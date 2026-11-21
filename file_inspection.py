"""Bounded offline NC and workpiece inspection with an optional geometry validator."""
from __future__ import annotations

from array import array
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys

from constants import WORKSPACE as WORKSPACE_PATH
from errors import DriverError

WORKSPACE = Path(WORKSPACE_PATH)
MAX_PROGRAM_BYTES = 5 * 1024 * 1024
MAX_WORKPIECE_BYTES = 64 * 1024 * 1024
MAX_VERTICES = 1_000_000
MAX_FACES = 50_000
MAX_EVENTS = 200
MAX_PAGE_CHARACTERS = 40_000
WORKER_TIMEOUT_SECONDS = 45

PROGRAM_LIMITS = [
    'Static literal-word summary only; no program execution, macro evaluation, subprogram expansion or control-flow interpretation.',
    'G10 entries and G54-G59 selections are textual occurrences; they do not establish the machine work offsets or effective compensation.',
    'Tool/H/D numbers are literal references, not proof of installed tool geometry, tool health or actual register values.',
    'Parenthesis and semicolon comments are excluded from the summary; original page text remains untrusted file content.',
    'Returned event lists are bounded; consult event_counts and events_truncated. No collision or machining-safety verdict is produced.',
]


def _path(value, must_exist=True):
    if not isinstance(value, (str, Path)) or not str(value):
        raise DriverError('invalid_path', 'An absolute local filesystem path is required.')
    text = str(value)
    if any(ord(c) < 32 for c in text) or text.startswith(('\\\\', '//')):
        raise DriverError('invalid_path', 'UNC, device and control-character paths are not supported.')
    path = Path(text)
    if not path.is_absolute() or (os.name == 'nt' and ':' in text[2:]):
        raise DriverError('invalid_path', 'An absolute local path without an alternate data stream is required.')
    try:
        resolved = path.resolve(strict=must_exist)
        if os.name == 'nt' and str(resolved).startswith('\\\\'):
            raise DriverError('invalid_path', 'Resolved path must be local.')
        return resolved
    except (OSError, ValueError) as exc:
        raise DriverError('file_unavailable', 'Cannot resolve the supplied local path.', path=text, reason=str(exc)) from exc


def _stable_read(filename, limit):
    path = _path(filename)
    try:
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise DriverError('invalid_path', 'Input must be a regular file.', path=str(path))
            if before.st_size > limit:
                raise DriverError('file_too_large', 'Input exceeds the bounded parser limit.', bytes=before.st_size, maximum_bytes=limit)
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        latest = path.stat()
    except OSError as exc:
        raise DriverError('file_unavailable', 'Cannot read the input file.', path=str(path), reason=str(exc)) from exc
    if len(raw) > limit:
        raise DriverError('file_too_large', 'Input grew beyond the parser limit.', maximum_bytes=limit)
    identity = lambda s: (s.st_size, s.st_mtime_ns, s.st_ino)
    if identity(before) != identity(after) or identity(after) != identity(latest) or len(raw) != after.st_size:
        raise DriverError('input_changed', 'Input changed during the bounded read; retry after exporting is complete.')
    return path, raw, {
        'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
        'mtime_utc': datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat(),
        'fresh_run_verified': False,
    }


def _decode_program(raw):
    if raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        encodings = ['utf-32']
    elif raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ['utf-16']
    else:
        encodings = ['utf-8-sig', 'gb18030']
    for encoding in encodings:
        try:
            text = raw.decode(encoding, errors='strict')
        except UnicodeError:
            continue
        if any(ord(c) < 32 and c not in '\r\n\t\f' for c in text):
            continue
        return text, encoding
    raise DriverError('unsupported_text_encoding', 'Program is binary or cannot be decoded strictly as UTF-8, GB18030 or BOM-marked UTF-16/32.')


def _code_lines(lines):
    depth = 0
    for number, line in enumerate(lines, 1):
        code = []
        for character in line:
            if character == '(':
                depth += 1
            elif character == ')' and depth:
                depth -= 1
            elif character == ';' and not depth:
                break
            elif not depth:
                code.append(character)
        yield number, ''.join(code).upper(), depth


WORD = re.compile(r'(?<![A-Z_])([A-Z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))')


def _program_summary(lines):
    sets = {name: set() for name in ['o_numbers', 'tool_numbers', 'h_registers', 'd_registers', 'g_codes', 'm_codes']}
    events = {name: [] for name in ['g10_blocks', 'coordinate_selections', 'tool_change_blocks', 'length_compensation_blocks', 'cutter_compensation_blocks', 'subprogram_call_blocks']}
    counts = defaultdict(int)
    macro = calls = skipped = False
    open_comment = 0
    def event(name, entry):
        counts[name] += 1
        if len(events[name]) < MAX_EVENTS:
            events[name].append(entry)
    for number, code, open_comment in _code_lines(lines):
        if len(code) > 65536:
            raise DriverError('program_too_complex', 'A code line exceeds the static parser bound.', line=number, maximum_characters=65536)
        words = defaultdict(list)
        for index, match in enumerate(WORD.finditer(code)):
            if index >= 4096:
                raise DriverError('program_too_complex', 'Too many words on one program line.', line=number)
            value = float(match.group(2))
            if not math.isfinite(value):
                raise DriverError('invalid_program', 'Non-finite numeric literal.', line=number)
            words[match.group(1)].append(value)
        gs, ms = words.get('G', []), words.get('M', [])
        for letter, name in [('O', 'o_numbers'), ('T', 'tool_numbers'), ('H', 'h_registers'), ('D', 'd_registers')]:
            sets[name].update(int(v) for v in words.get(letter, []) if v.is_integer())
        sets['g_codes'].update(gs)
        sets['m_codes'].update(ms)
        dynamic = any(c in code for c in '#[]') or bool(re.search(r'\b(?:IF|WHILE|GOTO|DO|END)\b', code))
        macro |= dynamic
        skipped |= bool(re.match(r'^\s*/', code))
        entry = {'line': number, 'words': dict(words), 'has_dynamic_expression': dynamic,
                 'block_skip': bool(re.match(r'^\s*/', code))}
        if 10 in gs:
            target = None
            if words.get('L') == [2.0] and len(words.get('P', [])) == 1:
                p = words['P'][0]
                if p.is_integer() and 1 <= p <= 6:
                    target = f'G{53 + int(p)}'
            event('g10_blocks', {**entry, 'target_coordinate_system': target,
                                'interpretation': 'literal G10 words; unresolved expressions and execution state are not evaluated'})
        coordinate_codes = [g for g in gs if g in (54, 55, 56, 57, 58, 59, 54.1)]
        if coordinate_codes:
            event('coordinate_selections', {**entry, 'g_codes': coordinate_codes})
        if 6 in ms:
            event('tool_change_blocks', entry)
        if any(g in (43, 44, 49) for g in gs) or 'H' in words:
            event('length_compensation_blocks', entry)
        if any(g in (40, 41, 42) for g in gs) or 'D' in words:
            event('cutter_compensation_blocks', entry)
        if any(m in (98, 198) for m in ms) or any(g in (65, 66, 66.1) for g in gs):
            calls = True
            event('subprogram_call_blocks', entry)
    return {**{name: sorted(values)[:MAX_EVENTS] for name, values in sets.items()}, **events,
            'unique_value_counts': {name: len(values) for name, values in sets.items()},
            'unique_values_truncated': {name: len(values) > MAX_EVENTS for name, values in sets.items()},
            'event_counts': {name: counts[name] for name in events},
            'events_truncated': {name: counts[name] > MAX_EVENTS for name in events},
            'has_macro_expressions': macro, 'has_subprogram_calls': calls,
            'has_block_skip': skipped, 'unterminated_parenthesis_comment': bool(open_comment),
            'dynamic_semantics_evaluated': False, 'machine_configuration_verified': False}


def inspect_program(filename, start_line=1, max_lines=100):
    """Return a hash, bounded original-text page and explicitly static NC summary."""
    if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
        raise DriverError('invalid_argument', 'start_line must be a positive integer.')
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) or not 1 <= max_lines <= 500:
        raise DriverError('invalid_argument', 'max_lines must be an integer from 1 to 500.')
    _, raw, source = _stable_read(filename, MAX_PROGRAM_BYTES)
    text, encoding = _decode_program(raw)
    lines = text.splitlines()
    summary = _program_summary(lines)
    page, characters = [], 0
    for number in range(start_line, min(len(lines), start_line + max_lines - 1) + 1):
        original = lines[number - 1]
        displayed = original[:4000]
        if characters + len(displayed) > MAX_PAGE_CHARACTERS and page:
            break
        page.append({'line': number, 'text': displayed, 'truncated': len(displayed) < len(original)})
        characters += len(displayed)
    next_line = page[-1]['line'] + 1 if page else start_line
    more = next_line <= len(lines)
    return {'ok': True, 'operation': 'inspect_program', 'source': source,
            'encoding': encoding, 'encoding_strategy': 'strict BOM-aware UTF decoding, then GB18030 fallback; no replacement characters',
            'lines': page, 'pagination': {'start_line': start_line, 'returned_lines': len(page),
                                         'total_lines': len(lines), 'has_more': more,
                                         'next_start_line': next_line if more else None},
            'summary': summary, 'content_is_untrusted': True, 'limitations': PROGRAM_LIMITS}


def _inspect_mesh(raw):
    if len(raw) < 48:
        raise DriverError('invalid_workpiece', 'Native .wp header is truncated.')
    version, stock_type, *stock_size = struct.unpack_from('<II3d', raw)
    if version != 2:
        raise DriverError('invalid_workpiece', 'Only observed native .wp version 2 is supported.', version=version)
    if not all(math.isfinite(value) and value > 0 for value in stock_size):
        raise DriverError('invalid_workpiece', 'Stock dimensions must be finite and positive.')

    nv = struct.unpack_from('<I', raw, 40)[0]
    if nv > MAX_VERTICES:
        raise DriverError('workpiece_too_complex', 'Vertex count exceeds offline parser limit.', vertices=nv, maximum_vertices=MAX_VERTICES)
    vertices_end = 44 + nv * 24
    if nv < 4 or vertices_end + 4 > len(raw):
        raise DriverError('invalid_workpiece', 'Native vertex data is truncated or invalid.')
    nf = struct.unpack_from('<I', raw, vertices_end)[0]
    pos = vertices_end + 4
    if nf > MAX_FACES:
        raise DriverError('workpiece_too_complex', 'Face count exceeds offline parser limit.', faces=nf, maximum_faces=MAX_FACES)
    if nf < 4:
        raise DriverError('invalid_workpiece', 'Native face count is invalid.')

    view = memoryview(raw)
    lower = [math.inf] * 3
    upper = [-math.inf] * 3
    heights = array('d')
    for vertex in struct.iter_unpack('<3d', view[44:vertices_end]):
        for axis, value in enumerate(vertex):
            if not math.isfinite(value):
                raise DriverError('invalid_workpiece', 'Native coordinates must be finite.')
            lower[axis] = min(lower[axis], value)
            upper[axis] = max(upper[axis], value)
        heights.append(vertex[2])
    size = [high - low for low, high in zip(lower, upper)]
    if not all(map(math.isfinite, size)):
        raise DriverError('invalid_workpiece', 'Native mesh dimensions are out of range.')

    levels = set()
    for _ in range(nf):
        if pos + 4 > len(raw):
            raise DriverError('invalid_workpiece', 'Native face record is truncated.')
        count = struct.unpack_from('<I', raw, pos)[0]
        pos += 4
        next_face = pos + count * 4
        if count < 3 or next_face > len(raw):
            raise DriverError('invalid_workpiece', 'Native face index data is truncated or invalid.')
        low_z, high_z = math.inf, -math.inf
        for (index,) in struct.iter_unpack('<I', view[pos:next_face]):
            if index >= nv:
                raise DriverError('invalid_workpiece', 'A face references a vertex outside the mesh.')
            height = heights[index]
            low_z, high_z = min(low_z, height), max(high_z, height)
        if high_z - low_z <= 1e-5:
            levels.add(heights[struct.unpack_from('<I', raw, pos)[0]])
        pos = next_face

    native = {'version': version, 'stock_type': stock_type, 'header_stock_size_mm': stock_size,
              'mesh_end_offset': pos, 'uninterpreted_trailing_bytes': len(raw) - pos}
    mesh = {'vertices': nv, 'faces': nf, 'minimum_xyz_mm': lower, 'maximum_xyz_mm': upper, 'size_mm': size,
            'horizontal_face_z_levels_mm': sorted(levels)[:64], 'horizontal_levels_truncated': len(levels) > 64}
    return native, mesh


def _workpiece_source(filename):
    path, raw, source = _stable_read(filename, MAX_WORKPIECE_BYTES)
    if path.suffix.lower() != '.wp':
        raise DriverError('invalid_workpiece', 'Supply a native .wp file; .wpi and other containers are not automatically converted.')
    native, mesh = _inspect_mesh(raw)
    return path, source, native, mesh


def _output_path(value):
    path = _path(value, must_exist=False)
    if path.exists():
        raise DriverError('output_exists', 'Validation requires a brand-new output directory; no existing directory is overwritten.', path=str(path))
    if not path.is_relative_to(WORKSPACE.resolve()):
        raise DriverError('invalid_output', 'Keep validation output inside SSCNC_WORKSPACE.')
    if not path.parent.is_dir():
        raise DriverError('invalid_output', 'Output parent directory must already exist.', parent=str(path.parent))
    return path


def _validator_script():
    configured = os.environ.get('SSCNC_VALIDATOR_SCRIPT')
    if not configured:
        raise DriverError('analysis_environment_unavailable', 'Set SSCNC_VALIDATOR_SCRIPT to enable validate_pocket.')
    script = Path(configured).expanduser()
    if not script.is_absolute() or not script.is_file() or script.suffix.lower() != '.py':
        raise DriverError('analysis_environment_unavailable', 'SSCNC_VALIDATOR_SCRIPT must be an existing absolute .py path.')
    return script.resolve()


def _run_geometry(filename, expected_hash, output):
    _validator_script()
    python = Path(os.environ.get('SSCNC_VALIDATOR_PYTHON', sys.executable)).expanduser()
    if not python.is_absolute() or not python.is_file():
        raise DriverError('analysis_environment_unavailable', 'SSCNC_VALIDATOR_PYTHON must be an existing absolute executable path.')
    request = {'filename': str(filename), 'expected_sha256': expected_hash, 'output': str(output)}
    try:
        result = subprocess.run([str(python), '-X', 'utf8', str(Path(__file__).resolve()),
                                 '--geometry-worker', json.dumps(request)],
                                # Do not pass MCP's live protocol input pipe to this offline
                                # worker: the tested Windows stdio transport stalls if inherited.
                                # The complete worker request is already carried in argv.
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=WORKER_TIMEOUT_SECONDS,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    except subprocess.TimeoutExpired as exc:
        raise DriverError('analysis_timeout', 'Offline geometry analysis exceeded its bounded time budget.', seconds=WORKER_TIMEOUT_SECONDS,
                          output_directory=str(output)) from exc
    except OSError as exc:
        raise DriverError('analysis_environment_unavailable', 'Cannot start the existing numerical Python.', reason=str(exc)) from exc
    if len(result.stdout) > 2 * 1024 * 1024:
        raise DriverError('analysis_response_invalid', 'Numerical worker response exceeds its output limit.')
    try:
        response = json.loads(result.stdout.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise DriverError('analysis_response_invalid', 'Numerical worker did not return a valid JSON result.', exit_code=result.returncode,
                          stderr=result.stderr.decode('utf-8', 'replace')[-2000:]) from exc
    if not response.get('ok'):
        error = response.get('error', {})
        raise DriverError(error.get('code', 'geometry_analysis_failed'), error.get('message', 'Geometry analysis failed.'), **error.get('details', {}))
    if result.returncode != 0 or response.get('source', {}).get('sha256') != expected_hash:
        raise DriverError('input_changed', 'Geometry worker output did not match the originally inspected input.')
    return response


def inspect_workpiece(filename):
    """Summarize a saved native mesh using the observed version 2 layout."""
    _, source, native, mesh = _workpiece_source(filename)
    return {'ok': True, 'operation': 'inspect_workpiece', 'source': source,
            'native_format': native, 'mesh': mesh,
            'parser': {'path': str(Path(__file__).resolve()), 'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            'fresh_simulation_verified': False, 'pocket_geometry_validated': False,
            'limitations': ['Reads only the saved mesh.',
                            'Horizontal levels describe mesh surfaces. Pocket depth requires a separate check.',
                            'The parser covers the observed SSCNC version 2 mesh layout.']}


def validate_pocket(filename, output_directory):
    """Apply a configured geometry validator and write to a new directory."""
    output = _output_path(output_directory)
    path, source, _, _ = _workpiece_source(filename)
    return _run_geometry(path, source['sha256'], output)


def _load_geometry(script):
    spec = importlib.util.spec_from_file_location('sscnc_pocket_validation', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _geometry_worker(request):
    path, source, _, _ = _workpiece_source(request['filename'])
    if source['sha256'] != request['expected_sha256']:
        raise DriverError('input_changed', 'Input changed between the host check and numerical worker.')
    output = _output_path(request['output'])
    script = _validator_script()
    module = _load_geometry(script)
    tolerances = {'stock_mm': 1e-6, 'depth_mm': .01, 'contour_mm': .05, 'radius_mm': .05, 'corner_mm': .10}
    report, evidence = module.analyze(path, 'unspecified', 'Offline SSCNC MCP file validation; no new machining performed.', tolerances)
    if report['source']['sha256'] != source['sha256']:
        raise DriverError('input_changed', 'Geometry validation observed a changed input file.')
    # mkdir is exclusive: a competing creator cannot turn this into an overwrite.
    try:
        output.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise DriverError('output_exists', 'Output was created by another operation; nothing was overwritten.', path=str(output)) from exc
    module.write_validation(output, report, evidence, plot=True)
    artifacts = []
    for artifact in sorted(output.iterdir()):
        if artifact.is_file():
            raw = artifact.read_bytes()
            artifacts.append({'path': str(artifact), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    return {'ok': True, 'operation': 'validate_pocket', 'source': source,
            'passed': report['passed'], 'validation': report, 'output_directory': str(output),
            'artifacts': artifacts, 'fresh_simulation_verified': False,
            'validation_method': {'path': str(script), 'sha256': hashlib.sha256(script.read_bytes()).hexdigest()},
            'limitations': report['limitations']}


def _worker_main():
    try:
        request = json.loads(sys.argv[2])
        result = _geometry_worker(request)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except DriverError as exc:
        error = exc
    except (ValueError, struct.error, IndexError, ArithmeticError) as exc:
        error = DriverError('invalid_workpiece', 'Native workpiece could not be parsed or measured.', reason=str(exc))
    except ImportError as exc:
        error = DriverError('analysis_environment_unavailable', 'Existing geometry environment is missing a required dependency.', reason=str(exc))
    except OSError as exc:
        error = DriverError('analysis_io_error', 'Offline analysis could not read or write an artifact.', reason=str(exc))
    except Exception as exc:
        error = DriverError('geometry_analysis_failed', 'Offline geometry analysis failed.', reason=str(exc))
    print(json.dumps({'ok': False, 'error': error.as_dict()}, ensure_ascii=False))
    return 1


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--geometry-worker':
        raise SystemExit(_worker_main())
    raise SystemExit('This module exposes inspect_program, inspect_workpiece and validate_pocket for SSCNC MCP.')
