import hashlib
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if sys.argv[1:] == ['--offline-stdio-fixture']:
    sys.path.insert(0, str(ROOT))
from errors import DriverError


def write_workpiece(path):
    """A synthetic box using the observed version 2 native mesh layout."""
    vertices = [(0, 0, 0), (2, 0, 0), (2, 3, 0), (0, 3, 0),
                (0, 0, 4), (2, 0, 4), (2, 3, 4), (0, 3, 4)]
    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
             (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    header = struct.pack('<II4dI', 2, 1, 2., 3., 4., 0., len(vertices))
    coordinates = b''.join(struct.pack('<3d', *vertex) for vertex in vertices)
    polygons = b''.join(struct.pack('<5I', len(face), *face) for face in faces)
    path.write_bytes(header + coordinates + struct.pack('<I', len(faces)) + polygons)
    return path


class FileInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sscnc-files-')
        self.tmp = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('file_inspection'),
                             'Offline program/workpiece file inspection is not implemented')
        return importlib.import_module('file_inspection')

    def write_program(self, text, encoding='utf-8'):
        path = self.tmp / '中文程序.cnc'
        path.write_bytes(text.encode(encoding))
        return path

    def test_program_pages_original_text_and_excludes_comment_commands(self):
        path = self.write_program('%\nO0001\n(中文 G10L2P1X999 T99)\nN10G90G54\nN20G10L2P1X12.Y23.Z34.\nN30T3M6\nN40G43H03Z15.\nN50G41D05\nN60M30\n%\n')
        result = self.module().inspect_program(str(path), start_line=3, max_lines=3)
        self.assertTrue(result['ok'])
        self.assertEqual(result['source']['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual([line['line'] for line in result['lines']], [3, 4, 5])
        self.assertIn('中文', result['lines'][0]['text'])
        self.assertTrue(result['pagination']['has_more'])
        self.assertEqual(result['pagination']['next_start_line'], 6)
        summary = result['summary']
        self.assertEqual(summary['o_numbers'], [1])
        self.assertEqual(summary['tool_numbers'], [3])
        self.assertEqual(summary['h_registers'], [3])
        self.assertEqual(summary['d_registers'], [5])
        self.assertEqual(summary['g10_blocks'][0]['words']['X'], [12.0])
        self.assertEqual(summary['g10_blocks'][0]['target_coordinate_system'], 'G54')
        self.assertFalse(summary['dynamic_semantics_evaluated'])

    def test_gb18030_and_utf16_chinese_are_read_without_replacement(self):
        for encoding in ['gb18030', 'utf-16']:
            with self.subTest(encoding=encoding):
                path = self.write_program('O0012\n(中文刀具说明)\nM30\n', encoding)
                result = self.module().inspect_program(str(path))
                self.assertIn('中文刀具说明', result['lines'][1]['text'])
                self.assertNotIn('\ufffd', result['lines'][1]['text'])

    def test_macros_subprograms_and_block_skip_remain_explicitly_unresolved(self):
        path = self.write_program('O0001\n#100=2\n/G10L2P1X#100\nM98P1234\nG65P9000A1\nM30\n')
        result = self.module().inspect_program(str(path))
        self.assertTrue(result['summary']['has_macro_expressions'])
        self.assertTrue(result['summary']['has_subprogram_calls'])
        self.assertTrue(result['summary']['has_block_skip'])
        self.assertFalse(result['summary']['dynamic_semantics_evaluated'])
        self.assertTrue(result['summary']['g10_blocks'][0]['has_dynamic_expression'])

    def test_macro_keywords_do_not_become_tool_or_o_number_references(self):
        path = self.write_program('IF[#1GT10]GOTO20\nM30\n')
        summary = self.module().inspect_program(str(path))['summary']
        self.assertEqual(summary['tool_numbers'], [])
        self.assertEqual(summary['o_numbers'], [])
        self.assertTrue(summary['has_macro_expressions'])

    def test_relative_input_bad_paging_and_binary_program_are_rejected(self):
        module = self.module()
        path = self.write_program('O1\nM30')
        for args, code in [(('relative.cnc',), 'invalid_path'), ((str(path), 0, 10), 'invalid_argument'),
                           ((str(path), 1, 0), 'invalid_argument'), ((str(path), 1, 501), 'invalid_argument')]:
            with self.subTest(args=args), self.assertRaises(DriverError) as caught:
                module.inspect_program(*args)
            self.assertEqual(caught.exception.code, code)
        path.write_bytes(b'O1\x00\x01M30')
        with self.assertRaises(DriverError) as caught:
            module.inspect_program(str(path))
        self.assertEqual(caught.exception.code, 'unsupported_text_encoding')

    def test_oversize_input_is_rejected_before_program_decoding(self):
        module = self.module()
        path = self.tmp / 'oversize.cnc'
        with path.open('wb') as stream:
            stream.truncate(module.MAX_PROGRAM_BYTES + 1)
        with self.assertRaises(DriverError) as caught:
            module.inspect_program(str(path))
        self.assertEqual(caught.exception.code, 'file_too_large')

    def test_inspects_wp_without_external_validator(self):
        filename = write_workpiece(self.tmp / 'box.wp')
        original_hash = hashlib.sha256(filename.read_bytes()).hexdigest()
        module = self.module()
        with patch.dict(os.environ):
            os.environ.pop('SSCNC_VALIDATOR_SCRIPT', None)
            result = module.inspect_workpiece(str(filename))
        self.assertTrue(result['ok'])
        self.assertEqual(result['source']['sha256'], original_hash)
        self.assertEqual(result['mesh']['size_mm'], [2., 3., 4.])
        self.assertEqual(result['native_format']['header_stock_size_mm'], [2., 3., 4.])
        self.assertEqual(result['mesh']['vertices'], 8)
        self.assertEqual(result['mesh']['faces'], 6)
        self.assertEqual(result['mesh']['horizontal_face_z_levels_mm'], [0., 4.])
        self.assertFalse(result['fresh_simulation_verified'])
        self.assertEqual(hashlib.sha256(filename.read_bytes()).hexdigest(), original_hash)

    def test_workpiece_inspection_finishes_over_stdio(self):
        filename = write_workpiece(self.tmp / 'box.wp')

        async def check():
            from mcp.client.session import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client
            params = StdioServerParameters(command=sys.executable,
                args=['-u', str(Path(__file__).resolve()), '--offline-stdio-fixture'], cwd=str(ROOT),
                env={'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    result = await session.call_tool('inspect_workpiece', {'filename': str(filename)})
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(part.text for part in result.content if part.type == 'text'))
                    self.assertTrue(data.get('ok'), data)
                    self.assertEqual(data['source']['sha256'], hashlib.sha256(filename.read_bytes()).hexdigest())
                    self.assertEqual(data['mesh']['size_mm'], [2., 3., 4.])
        asyncio.run(asyncio.wait_for(check(), timeout=20))

    def test_corrupt_wp_has_structured_failure(self):
        broken = self.tmp / 'broken.wp'
        sample = write_workpiece(self.tmp / 'box.wp')
        broken.write_bytes(sample.read_bytes()[:80])
        with self.assertRaises(DriverError) as caught:
            self.module().inspect_workpiece(str(broken))
        self.assertEqual(caught.exception.code, 'invalid_workpiece')

    def test_unbounded_vertex_count_is_rejected(self):
        sample = write_workpiece(self.tmp / 'box.wp')
        raw = bytearray(sample.read_bytes()[:80])
        struct.pack_into('<I', raw, 40, 0xffffffff)
        broken = self.tmp / 'malformed.wp'
        broken.write_bytes(raw)
        with self.assertRaises(DriverError) as caught:
            self.module().inspect_workpiece(str(broken))
        self.assertIn(caught.exception.code, ['invalid_workpiece', 'workpiece_too_complex'])

    def test_validation_uses_configured_script_to_write_a_report(self):
        filename = write_workpiece(self.tmp / 'box.wp')
        output = self.tmp / 'validation'
        script = self.tmp / 'validator.py'
        script.write_text(
            'import hashlib\n'
            'import json\n'
            'def analyze(path, kind, description, tolerances):\n'
            '    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()\n'
            '    return {"source": {"sha256": sha256}, "passed": True, "limitations": []}, {}\n'
            'def write_validation(output, report, evidence, plot):\n'
            '    (output / "validation.json").write_text(json.dumps(report), encoding="utf-8")\n',
            encoding='utf-8',
        )
        module = self.module()
        with patch.object(module, 'WORKSPACE', self.tmp), patch.dict(os.environ, {
            'SSCNC_WORKSPACE': str(self.tmp),
            'SSCNC_VALIDATOR_SCRIPT': str(script),
            'SSCNC_VALIDATOR_PYTHON': sys.executable,
        }):
            result = module.validate_pocket(str(filename), str(output))
        self.assertTrue(result['ok'])
        self.assertTrue(result['passed'])
        self.assertFalse(result['fresh_simulation_verified'])
        report = json.loads((output / 'validation.json').read_text('utf-8'))
        self.assertEqual(report['source']['sha256'], hashlib.sha256(filename.read_bytes()).hexdigest())
        self.assertEqual([Path(item['path']).name for item in result['artifacts']], ['validation.json'])

    def test_validation_never_overwrites_existing_directory(self):
        output = self.tmp / 'existing'
        output.mkdir()
        marker = output / 'keep.txt'
        marker.write_text('unchanged')
        with self.assertRaises(DriverError) as caught:
            self.module().validate_pocket(str(self.tmp / 'box.wp'), str(output))
        self.assertEqual(caught.exception.code, 'output_exists')
        self.assertEqual(marker.read_text(), 'unchanged')


if __name__ == '__main__':
    if sys.argv[1:] == ['--offline-stdio-fixture']:
        sys.path.insert(0, str(ROOT))
        from file_inspection import inspect_workpiece
        from server import create_server
        class OfflineController:
            inspect_workpiece = staticmethod(inspect_workpiece)
        create_server(OfflineController()).run(transport='stdio')
    else:
        unittest.main()
