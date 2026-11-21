"""Bounded, read-only decoders for the SHA-pinned FANUC0iMF 7.2.5.2.

The caller verifies the process/image identity and supplies read(address, size).
No process attachment, native calls, file access, or writes occur here. Missing
evidence and failed optional reads remain explicit unknowns, never guessed zeros.
Each field records its observed source and unknown values remain explicit.
"""
from __future__ import annotations

import hashlib
import math
import struct

from errors import DriverError


MAX_PATH_BYTES = 4096
MAX_PROGRAM_SOURCE_BYTES = 1024 * 1024
MAX_MAGAZINE_SLOTS = 24
TOOL_OFFSET_ROW_COUNT = 401
ANSI_ENCODING = "gbk"  # CP936: this installation's Simplified Chinese ANSI text.

SOURCES = {
    "files.program": "CStringA global RVA 0x5d1814; mf_save_as_selection.asm.txt VA 0x6e4cd3..0x6e4d58 (NC source save/load)",
    "files.workpiece": "CStringA global RVA 0x5d181c; mf_addresses.json and mf_save_as_selection.asm.txt VA 0x6e4ab9",
    "offsets.active_work_xyz_mm": "globals RVA 0x5cc9f8/0x5cca00/0x5cca08; mf_run_loop.asm.txt VA 0x6ee540..0x6ee55d copies descriptor+0x60/+0x68/+0x70; these are active execution offsets, not a G54 table",
    "offsets.work_systems_xyz_mm": "persistent G54 XYZ globals RVA 0x5cd758/0x5cd760/0x5cd768; G55..G59 stride 0x30; native G54..59 switch RVA 0x8b143..0x8b4cf; register.lib loader RVA 0x238780 at inline state RVA 0x5cb370+0x23b0",
    "offsets.common_work_xyz_mm": "globals RVA 0x5cd728/0x5cd730/0x5cd738 added separately by coordinate getter RVA 0x8b090; G54 table is not assumed to include these or document shifts",
    "offsets.tool_length_compensation": "401-row double arrays RVA 0x5ce198/0x5cee20/0x5cfaa8/0x5d0730; offset.dat loader RVA 0x238a80; export RVA 0x27bf40 labels OFFSET_H[0]/[1], OFFSET_D[0]/[1]; G43 sum RVA 0x8b560, G41/G42 radius RVA 0x8b700, DiameterOffset flag RVA 0x5d1458",
    "machine": "kind RVA 0x5cb418; travel doubles doc+0x730/+0x738/+0x740; coordinate_transform_findings.json and mf_machine_spec_end.asm.txt",
    "stock": "inline object doc+0x3f48; type +0x38; X/Y/Z doubles +0x58/+0x48/+0x68; mf_writer_stream.asm.txt VA 0x66c75e..0x66c794 serializes native WP header; shift/rotation doc+0x3d0/+0x3d8/+0x3e0",
    "fixture": "kind RVA 0x5cca90; mf_cut_setup.asm.txt RVA 0x26c390 and mf_fixture_height_getter.asm.txt RVA 0x325a50; kind1 height doc+0x5f0, kind2 +0x5c8 +0x5d0, kind6 +0x5d0, kind5 +0x620; kind2 X shift +0x5d8",
    "tools.mapping": "ct_format_findings.json/ct_import_ui_findings.json: global pointer RVA 0x5cb184; object+8 records, +12 count; 72-byte records <ii64s> = T number/library ID/ANSI label",
    "tools.current_number_raw": "global RVA 0x5cb464; magazine/current-number state used by tool selection and cutting; do not infer the NC T word solely from this value",
    "tools.slots": "doc+0x36c pointer array; capacity global RVA 0x5ccb24, pinned ordinary MF maximum 24; spindle pointer doc+0x368; ct_format_findings.json",
    "tools.geometry": "tool object type+8, diameter+0x18, nominal length+0x20, library ID+0xd8; flat_tool_findings.json and mf_tool_effective_length.asm.txt RVA 0xe6db0",
    "tools.damaged": "tool+0x88 byte; damaged_tool_findings.json, continuous error branch RVA 0x2ee769 and normal CT import recovery RVA 0x31af59; not a custom-geometry setting",
    "tools.effective_length_mm": "normal native getter RVA 0xe6db0; damaged FLAT getter RVA 0xe6ca0 type0 branch RVA 0xe6d00, remaining parameter tool+0x98; unsupported types stay unknown",
}


def _valid_range(address, size=1):
    return (isinstance(address, int) and not isinstance(address, bool)
            and 0x10000 <= address < 0xFFFF0000 and size >= 0
            and address + size <= 0xFFFF0000)


class _Snapshot:
    def __init__(self, read, base):
        if not callable(read) or not _valid_range(base, 0x5D1870):
            raise DriverError("invalid_setup_reader", "Expected an x86 image base and a read-only byte reader")
        self.read = read
        self.samples = []
        self.unknowns = []

    def raw(self, address, size):
        if not _valid_range(address, size):
            raise DriverError("invalid_memory_pointer", "Read is outside the pinned x86 user address range")
        try:
            data = self.read(address, size)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError("memory_read_failed", "Unable to read setup memory") from exc
        if not isinstance(data, (bytes, bytearray)) or len(data) != size:
            raise DriverError("memory_read_failed", "Incomplete read-only setup snapshot")
        data = bytes(data)
        self.samples.append((address, data))
        return data

    def scalar(self, address, fmt):
        value = struct.unpack("<" + fmt, self.raw(address, struct.calcsize("<" + fmt)))[0]
        if fmt in ("d", "f") and not math.isfinite(value):
            raise DriverError("invalid_setup_value", "Setup contains a non-finite number")
        return value

    def unknown(self, field, reason, code="not_verified"):
        self.unknowns.append({"field": field, "reason": reason, "code": code})
        return None

    def optional(self, field, operation):
        try:
            return operation()
        except DriverError as exc:
            return self.unknown(field, str(exc), exc.code)

    def pointer(self, address, size=1):
        pointer = self.scalar(address, "I")
        if pointer and not _valid_range(pointer, size):
            raise DriverError("invalid_memory_pointer", "Object pointer is outside the pinned x86 user address range")
        return pointer

    def cstring(self, address):
        pointer = self.pointer(address)
        if not pointer:
            raise DriverError("unavailable_string", "CStringA has not been initialized")
        length, allocated = struct.unpack("<ii", self.raw(pointer - 12, 8))
        if not (0 <= length <= MAX_PATH_BYTES and length <= allocated <= 1024 * 1024):
            raise DriverError("invalid_cstring", "CStringA length/allocation is outside the supported bound")
        raw = self.raw(pointer, length + 1)
        if raw[-1:] != b"\0" or b"\0" in raw[:-1]:
            raise DriverError("invalid_cstring", "CStringA termination disagrees with its recorded length")
        try:
            return raw[:-1].decode(ANSI_ENCODING)
        except UnicodeError as exc:
            raise DriverError("invalid_text_encoding", "CStringA is not valid CP936 text") from exc

    def coherent(self):
        # This is a repeated-read check, not an atomic snapshot or process lock.
        for address, expected in self.samples:
            try:
                observed = self.read(address, len(expected))
            except Exception:
                return False
            if observed != expected:
                return False
        return True


def _vector(snapshot, address, offsets, *, positive=False):
    values = [snapshot.scalar(address + offset, "d") for offset in offsets]
    if positive and any(value <= 0 for value in values):
        raise DriverError("invalid_setup_value", "Dimensions must be positive")
    return values


def _tool(snapshot, pointer, field):
    if not pointer:
        return None
    if not _valid_range(pointer, 0xDC):
        raise DriverError("invalid_memory_pointer", "Tool pointer is outside the pinned x86 user address range")
    result = {"address": pointer}
    for key, offset, fmt in (("type_id", 8, "i"), ("library_id", 0xD8, "i"),
                             ("diameter_mm", 0x18, "d"), ("nominal_length_mm", 0x20, "d")):
        result[key] = snapshot.optional(field + "." + key,
                                        lambda o=offset, f=fmt: snapshot.scalar(pointer + o, f))
    damage = snapshot.optional(field + ".damaged", lambda: snapshot.scalar(pointer + 0x88, "B"))
    result["damaged"] = bool(damage) if damage in (0, 1) else None
    if damage not in (0, 1, None):
        snapshot.unknown(field + ".damaged", "Damage flag is outside the observed 0/1 range", "invalid_setup_value")
    kind, length = result["type_id"], result["nominal_length_mm"]
    effective = None
    if length is not None and length > 0 and damage == 0:
        # Only branches with verified direct length semantics are exposed.
        if kind in {0, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20}:
            effective = length
        elif kind == 1 and result["diameter_mm"] is not None:
            effective = length - result["diameter_mm"] / 2
    elif length is not None and length > 0 and damage == 1 and kind == 0:
        remaining = snapshot.optional(field + ".remaining_length_parameter_mm",
                                      lambda: snapshot.scalar(pointer + 0x98, "d"))
        if remaining is not None and remaining >= 0:
            if remaining + 5 > length:
                effective = length / 2
            elif remaining + 15 > length:
                effective = remaining
            elif remaining + 30 > length:
                effective = remaining + 5
            else:
                effective = max(remaining + 5, length * 0.4)
    if effective is None or effective <= 0:
        effective = snapshot.unknown(field + ".effective_length_mm",
                                     "Effective length is not verified for this tool type/state or its dimensions are unavailable")
    result["effective_length_mm"] = effective
    return result


def _mapping(snapshot, base):
    table = snapshot.pointer(base + 0x5CB184, 16)
    if not table:
        raise DriverError("unavailable_tool_table", "No CT mapping object is loaded")
    count = snapshot.scalar(table + 12, "i")
    if not 0 <= count <= MAX_MAGAZINE_SLOTS + 1:
        raise DriverError("invalid_tool_table", "CT record count exceeds the pinned MF layout")
    if not count:
        return []
    records = snapshot.pointer(table + 8, count * 72)
    if not records:
        raise DriverError("invalid_tool_table", "Nonempty CT mapping has no record storage")
    raw = snapshot.raw(records, count * 72)
    rows = []
    for i in range(count):
        t_number, library_id, label = struct.unpack_from("<ii64s", raw, i * 72)
        label = label.split(b"\0", 1)[0]
        try:
            name = label.decode(ANSI_ENCODING)
        except UnicodeError:
            name = snapshot.unknown(f"tools.mapping[{i}].label", "CT label is not valid CP936 text", "invalid_text_encoding")
        rows.append({"record_index": i, "t_number": t_number, "library_id": library_id,
                     "label": name, "empty": library_id == -1})
    return rows


def _offset_table(snapshot, base):
    columns = []
    for rva in (0x5CE198, 0x5CEE20, 0x5CFAA8, 0x5D0730):
        values = struct.unpack("<401d", snapshot.raw(base + rva, TOOL_OFFSET_ROW_COUNT * 8))
        if any(not math.isfinite(value) for value in values):
            raise DriverError("invalid_setup_value", "Tool offset table contains a non-finite number")
        columns.append(values)
    diameter_raw = snapshot.scalar(base + 0x5D1458, "i")
    diameter_mode = diameter_raw != 0
    entries = []
    for index, (h0, h1, d0, d1) in enumerate(zip(*columns)):
        if not any((h0, h1, d0, d1)):
            continue
        h_sum, d_sum = h0 + h1, d0 + d1
        if not math.isfinite(h_sum) or not math.isfinite(d_sum):
            raise DriverError("invalid_setup_value", "Tool offset sum exceeds the finite range")
        entries.append({"index": index, "h0_mm": h0, "h1_mm": h1,
                        "h_sum_mm": h_sum, "d0_mm": d0, "d1_mm": d1,
                        "d_sum_mm": d_sum,
                        "cutter_radius_mm": d_sum / 2 if diameter_mode else d_sum})
    return {"row_count": TOOL_OFFSET_ROW_COUNT, "first_index": 0, "last_index": 400,
            "entries": entries, "zero_rows_omitted": True,
            "diameter_mode": diameter_mode, "diameter_mode_raw": diameter_raw,
            "columns": {"h0_mm": "OFFSET_H[0]", "h1_mm": "OFFSET_H[1]",
                        "d0_mm": "OFFSET_D[0]", "d1_mm": "OFFSET_D[1]"},
            "semantics": "All 401 rows are read; omitted rows have four zero values. These are table indices, not magazine/T numbers or the currently applied H/D selection."}


def read_setup(read, base: int) -> dict:
    """Read proven setup fields. Optional failures appear in ``unknowns``.

    An invalid document pointer or unreadable root state raises DriverError.
    ``coherent`` only means sampled bytes were identical at the second read.
    No missing G54 or H/D offset table is reconstructed from the NC text.
    """
    snapshot = _Snapshot(read, base)
    doc = snapshot.scalar(base + 0x5CB178, "I")
    if doc and not _valid_range(doc, 0x3FB8):
        raise DriverError("invalid_document", "Document pointer is outside the pinned x86 user address range")
    cycle = snapshot.scalar(base + 0x5C6924, "I")
    worker = snapshot.scalar(base + 0x5CC83E, "B")
    optional = snapshot.optional

    def doc_value(field, operation):
        return optional(field, operation) if doc else snapshot.unknown(field, "No document is loaded", "no_document")

    files = {name: optional("files." + name, lambda rva=rva: snapshot.cstring(base + rva))
             for name, rva in (("program", 0x5D1814), ("workpiece", 0x5D181C))}
    work_systems = {f"G{54 + index}": optional(f"offsets.work_systems_xyz_mm.G{54 + index}",
                    lambda i=index: _vector(snapshot, base + 0x5CD758 + i * 0x30, (0, 8, 16)))
                    for index in range(6)}
    offsets = {
        "active_work_xyz_mm": optional("offsets.active_work_xyz_mm", lambda: _vector(snapshot, base, (0x5CC9F8, 0x5CCA00, 0x5CCA08))),
        "g54_xyz_mm": work_systems["G54"],
        "work_systems_xyz_mm": work_systems,
        "common_work_xyz_mm": optional("offsets.common_work_xyz_mm", lambda: _vector(snapshot, base, (0x5CD728, 0x5CD730, 0x5CD738))),
        "tool_length_compensation": optional("offsets.tool_length_compensation", lambda: _offset_table(snapshot, base)),
    }
    snapshot.unknown("offsets.tool_column_ui_names", "Internal H[0]/H[1]/D[0]/D[1] names and sums are established; mapping to the UI geometry/wear labels is not yet independently verified")
    snapshot.unknown("offsets.applied_h_d_indices", "Current H/D selection is not inferred from a T number or nonzero table entries")
    machine = {
        "kind": optional("machine.kind", lambda: snapshot.scalar(base + 0x5CB418, "i")),
        "travel_xyz_mm": doc_value("machine.travel_xyz_mm", lambda: _vector(snapshot, doc, (0x730, 0x738, 0x740), positive=True)),
    }
    stock = {
        "type_raw": doc_value("stock.type_raw", lambda: snapshot.scalar(doc + 0x3F80, "i")),
        "size_xyz_mm": doc_value("stock.size_xyz_mm", lambda: _vector(snapshot, doc, (0x3FA0, 0x3F90, 0x3FB0), positive=True)),
        "shift_xy_mm": doc_value("stock.shift_xy_mm", lambda: _vector(snapshot, doc, (0x3D0, 0x3D8))),
        "rotation_degrees": doc_value("stock.rotation_degrees", lambda: snapshot.scalar(doc + 0x3E0, "d")),
    }
    fixture_kind = optional("fixture.kind", lambda: snapshot.scalar(base + 0x5CCA90, "i"))
    fixture = {"kind": fixture_kind, "base_height_mm": None, "shift_x_mm": None}
    if fixture_kind in (1, 2, 5, 6):
        fields = {1: (0x5F0,), 2: (0x5C8, 0x5D0), 5: (0x620,), 6: (0x5D0,)}[fixture_kind]
        fixture["base_height_mm"] = doc_value("fixture.base_height_mm", lambda: sum(_vector(snapshot, doc, fields)))
        fixture["shift_x_mm"] = (doc_value("fixture.shift_x_mm", lambda: snapshot.scalar(doc + 0x5D8, "d"))
                                  if fixture_kind == 2 else 0.0)
    else:
        snapshot.unknown("fixture.base_height_mm", "Fixture height conversion is not verified for this kind")
        snapshot.unknown("fixture.shift_x_mm", "Fixture X conversion is not verified for this kind")
    snapshot.unknown("fixture.full_dimensions_mm", "Full vise/jaw dimensions have not been established; only the placement height/shift is decoded")

    capacity = optional("tools.magazine_slot_count", lambda: snapshot.scalar(base + 0x5CCB24, "i"))
    tools = {
        "current_number_raw": optional("tools.current_number_raw", lambda: snapshot.scalar(base + 0x5CB464, "i")),
        "magazine_slot_count": capacity,
        "mapping": optional("tools.mapping", lambda: _mapping(snapshot, base)),
        "spindle": doc_value("tools.spindle", lambda: _tool(snapshot, snapshot.pointer(doc + 0x368, 0xDC), "tools.spindle")),
        "slots": None,
    }
    if doc and capacity is not None and 0 <= capacity <= MAX_MAGAZINE_SLOTS:
        tools["slots"] = []
        for index in range(capacity):
            field = f"tools.slots[{index}].tool"
            tool = optional(field, lambda i=index, f=field: _tool(snapshot, snapshot.pointer(doc + 0x36C + i * 4, 0xDC), f))
            tools["slots"].append({"slot_index": index, "tool": tool})
    else:
        snapshot.unknown("tools.slots", "Document or bounded magazine slot count is unavailable", "invalid_tool_table" if doc else "no_document")
    return {"schema_version": 1, "document": doc, "cycle": cycle, "worker": worker,
            "files": files, "offsets": offsets, "machine": machine, "stock": stock,
            "fixture": fixture, "tools": tools, "coherent": snapshot.coherent(),
            "sources": dict(SOURCES), "unknowns": snapshot.unknowns,
            "limitations": ["Caller must verify the pinned image SHA256 before reading.",
                            "Repeated reads are not an atomic snapshot; they cannot exclude changes between reads.",
                            "ANSI strings use CP936 for this Simplified Chinese installation.",
                            "CT record IDs, resolved slot tools, and the spindle tool are reported separately; their agreement is not assumed."]}


def read_program_source(read, base: int) -> dict:
    """Read SSCNC's actual NUL-terminated NC editor buffer, not the disk file.

    Raw source has no CString header. Its allocation capacity is object+0x18,
    pointer object+0x1c, where the inline object is base+0x5d1850. Never expose
    bytes following NUL; reject a source without a terminator inside the bound.
    """
    snapshot = _Snapshot(read, base)
    path = snapshot.optional("path", lambda: snapshot.cstring(base + 0x5D1814))
    capacity = snapshot.scalar(base + 0x5D1868, "I")
    if not 1 <= capacity <= 64 * 1024 * 1024:
        raise DriverError("invalid_program_source", "NC source allocation is outside the supported bound")
    limit = min(capacity, MAX_PROGRAM_SOURCE_BYTES)
    pointer = snapshot.pointer(base + 0x5D186C, limit)
    if not pointer:
        raise DriverError("invalid_program_source", "NC source buffer is unavailable")
    chunks = []
    for offset in range(0, limit, 4096):
        chunk = snapshot.raw(pointer + offset, min(4096, limit - offset))
        end = chunk.find(b"\0")
        chunks.append(chunk if end < 0 else chunk[:end])
        if end >= 0:
            break
    else:
        raise DriverError("invalid_program_source", "NC source has no NUL terminator inside its allocation/read limit")
    raw = b"".join(chunks)
    try:
        text = raw.decode(ANSI_ENCODING)
    except UnicodeError as exc:
        raise DriverError("invalid_program_source", "NC source is not valid CP936 text") from exc
    return {"text": text, "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "path": path, "encoding": "cp936", "capacity_bytes": capacity,
            "read_limit_bytes": MAX_PROGRAM_SOURCE_BYTES, "coherent": snapshot.coherent(),
            "source": "live_editor_buffer", "unknowns": snapshot.unknowns,
            "evidence": "MF raw NC object RVA 0x5d1850: capacity +0x18 / char* +0x1c; constructor RVA 0x305270, strlen getter RVA 0x305330, native NC save RVA 0x2e4d0c..0x2e4d24"}
