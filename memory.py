"""Read-only snapshot decoder. No process attachment or writes in this module."""
import math
import struct
import time

from constants import DOC_FIELDS, GLOBALS
from errors import DriverError


def decode_snapshot(read, base: int) -> dict:
    def scalar(address, fmt):
        size = struct.calcsize("<" + fmt)
        raw = read(address, size)
        if len(raw) != size:
            raise DriverError("memory_read_failed", "Incomplete read-only process snapshot")
        return struct.unpack("<" + fmt, raw)[0]

    result = {name: scalar(base + offset, fmt) for name, (offset, fmt) in GLOBALS.items()}
    doc = result["document"]
    if doc and not 0x10000 <= doc < 0xFFFF0000:
        raise DriverError("invalid_document", "Document pointer is outside the pinned x86 user address range")
    result.update({name: scalar(doc + offset, fmt) if doc else None
                   for name, (offset, fmt) in DOC_FIELDS.items()})
    result["xyz"] = [result.pop("x"), result.pop("y"), result.pop("z")]
    if not all(math.isfinite(number) for number in result["xyz"]):
        raise DriverError("invalid_telemetry", "Coordinate snapshot contains non-finite values")
    if doc and not (0 <= result["descriptor_count"] <= 5_000_000 and
                    -1 <= result["index"] <= result["descriptor_count"] + 1):
        raise DriverError("invalid_program_state", "Program index/count is outside plausible bounds")
    # The simulator is not suspended. Reject commands if bookend reads changed.
    result["coherent"] = all(
        result[key] == scalar(base + GLOBALS[key][0], GLOBALS[key][1])
        for key in ("document", "cycle", "worker", "mode", "emergency")
    )
    if doc:
        result["coherent"] &= all(result[name] == scalar(doc + DOC_FIELDS[name][0], DOC_FIELDS[name][1])
                                  for name in ("index", "descriptor_array", "descriptor_count"))
    result["timestamp"] = time.time()
    return result
