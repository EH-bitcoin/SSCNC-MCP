"""Read-only document flags for the SHA-pinned SSCNC FANUC0iMF image.

The caller must first verify the target's identity and image SHA256:
9e27c17d3d1f75af7db51f03829254f3744f2dc6514e25dcca84f9be50c28f10.
This decoder never opens a process, sends a command, or changes a flag. Its two
reads detect observed changes, not an atomic snapshot; callers must reject an
incoherent result and still guard against UI races after this check.

Evidence is local PE/objdump analysis, not a claimed REA Evidence record.
"""
from __future__ import annotations

import struct

from errors import DriverError


FLAG_BLOCK_RVA = 0x5D1830
FLAG_BLOCK_BYTES = 32
_FIELDS = (
    "tool_table_file_flag", "tool_table_dirty",
    "workpiece_file_flag", "workpiece_dirty",
    "fixture_file_flag", "fixture_dirty", "nc_file_flag", "nc_dirty",
)
SOURCES = {
    "nc_dirty": (
        "DWORD RVA 0x5d184c; editor insert sets 1 at RVA 0x3055bb; new NC "
        "sets 1 at 0x2feb24; NC save reads source 0x5d1850/0x5d186c and "
        "clears dirty at 0x2e3be0. Distinct from the FileOpen prompt predicate."
    ),
    "workpiece_dirty": (
        "DWORD RVA 0x5d183c; .wp extension dispatch RVA 0x2fff06..0x2fffb0 "
        "uses path CStringA 0x5d181c; WP save-as RVA 0x2e4ab9..0x2e4afe "
        "writes the WP and clears this dirty flag."
    ),
    "fixture_dirty": (
        "DWORD RVA 0x5d1844; .fx extension dispatch RVA 0x2fffbd..0x30000d "
        "uses path CStringA 0x5d1820; fixture save-as clears dirty at RVA 0x2e4be4."
    ),
    "tool_table_dirty": (
        "DWORD RVA 0x5d1834; .ct extension dispatch RVA 0x2ffdf6..0x2ffe59 "
        "uses path CStringA 0x5d1824; paired dirty check at RVA 0x2ffbb0."
    ),
    "file_flags": (
        "Paired DWORD RVAs CT 0x5d1830, WP 0x5d1838, FX 0x5d1840, "
        "NC 0x5d1848. Reported as native file flags, not inferred 'saved' status."
    ),
    "file_open_will_prompt": (
        "WM_COMMAND 57601 message map VA 0x9227a8 points to FileOpen RVA "
        "0x2ff630; SAVECURFILE key RVA 0x522734, common/lang/lang_cn.ini "
        "line 74. Predicate RVA 0x2ffb86..0x2ffbc4 is WP_dirty OR "
        "(FX_dirty AND FX_file_flag) OR (CT_dirty AND CT_file_flag). "
        "The final test reuses EAX=WP_dirty, so WP_file_flag does not gate it. "
        "RVA 0x2ffbc8 pushes 0x24 (Yes/No + question); RVA 0x2ffbd0 "
        "checks IDYES=6; both branches continue to the file picker. "
        "NC_dirty is absent from this native predicate."
    ),
}


def _sample(read, address):
    try:
        data = read(address, FLAG_BLOCK_BYTES)
    except Exception as exc:
        raise DriverError("memory_read_failed", "Cannot read document modification flags") from exc
    if not isinstance(data, (bytes, bytearray)) or len(data) != FLAG_BLOCK_BYTES:
        raise DriverError("memory_read_failed", "Document modification flags were not read completely")
    values = dict(zip(_FIELDS, struct.unpack("<8I", bytes(data))))
    for name, value in values.items():
        if value not in (0, 1):
            raise DriverError("invalid_document_flag", "Document flag is outside the verified 0/1 range",
                              field=name, value=value)
    return values


def read_document_flags(read, base) -> dict:
    """Return NC dirty and the exact native FileOpen prompt predicate.

    Raises DriverError for unreadable/invalid data. ``coherent=False`` means
    the caller must refuse actions that depend on this snapshot. A clean native
    prompt predicate alone does not mean the NC editor has no unsaved changes.
    """
    if (not callable(read) or not isinstance(base, int) or isinstance(base, bool)
            or base < 0x10000 or base + FLAG_BLOCK_RVA + FLAG_BLOCK_BYTES > 0xFFFF0000):
        raise DriverError("invalid_document_reader", "Expected a pinned x86 image base and read-only byte reader")
    first = _sample(read, base + FLAG_BLOCK_RVA)
    second = _sample(read, base + FLAG_BLOCK_RVA)
    reasons = []
    if first["workpiece_dirty"]:
        reasons.append("workpiece_dirty")
    if first["fixture_dirty"] and first["fixture_file_flag"]:
        reasons.append("fixture_dirty_with_file_flag")
    if first["tool_table_dirty"] and first["tool_table_file_flag"]:
        reasons.append("tool_table_dirty_with_file_flag")
    return {
        **{name: bool(value) for name, value in first.items()},
        "file_open_will_prompt": bool(reasons),
        "file_open_prompt_reasons": reasons,
        "coherent": first == second,
        "raw": first,
        "sources": dict(SOURCES),
    }
