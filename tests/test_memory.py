import struct
import unittest

from memory import decode_snapshot
from errors import DriverError


class MemoryTests(unittest.TestCase):
    def fixture(self):
        # Addresses are hand-derived from the MF manifest, at a non-preferred base.
        base, doc = 0x810000, 0x12000000
        cells = {
            base + 0x5CB178: struct.pack("<I", doc),
            base + 0x5C6924: struct.pack("<I", 1),
            base + 0x5CC83E: b"\x01",  # This guard is one byte, not a DWORD.
            base + 0x5C691C: struct.pack("<I", 2),
            base + 0x5CB458: struct.pack("<I", 0),
            base + 0x5CC7F0: struct.pack("<I", 1),
            base + 0x5CB460: struct.pack("<i", 3),
            base + 0x5CC7C4: struct.pack("<I", 12),
            base + 0x5CE190: struct.pack("<I", 12),
            base + 0x5CC998: struct.pack("<d", 1.25),
            base + 0x5CC9A0: struct.pack("<d", -2.5),
            base + 0x5CC9A8: struct.pack("<d", -10.0),
            doc + 0xD8: struct.pack("<i", 6),
            doc + 0xE0: struct.pack("<i", 5),
            doc + 0x3F0C: struct.pack("<I", 0x15000000),
            doc + 0x3F10: struct.pack("<i", 40),
        }
        return base, doc, cells

    def test_relocated_layout_reads_document_position_and_one_byte_guard(self):
        base, doc, cells = self.fixture()
        def read(address, size):
            self.assertEqual(len(cells[address]), size)
            return cells[address]
        result = decode_snapshot(read, base)
        self.assertEqual(result["document"], doc)
        self.assertEqual(result["index"], 6)
        self.assertEqual(result["source_line"], 5)
        self.assertEqual(result["descriptor_count"], 40)
        self.assertEqual(result.get("descriptor_array"), 0x15000000)
        self.assertEqual(result["worker"], 1)
        self.assertEqual(result["xyz"], [1.25, -2.5, -10.0])

    def test_program_change_during_read_is_not_coherent(self):
        base, doc, cells = self.fixture()
        calls = 0
        def read(address, size):
            nonlocal calls
            if address == doc + 0xD8:
                calls += 1
                if calls > 1: return struct.pack("<i", 7)
            return cells[address]
        self.assertFalse(decode_snapshot(read, base)["coherent"])

    def test_invalid_document_is_not_dereferenced(self):
        base, _, cells = self.fixture()
        cells[base + 0x5CB178] = struct.pack("<I", 4)
        with self.assertRaises(DriverError) as error:
            decode_snapshot(lambda address, size: cells[address], base)
        self.assertEqual(error.exception.code, "invalid_document")

    def test_descriptor_storage_change_during_read_is_not_coherent(self):
        base, doc, cells = self.fixture()
        calls = 0
        def read(address, size):
            nonlocal calls
            if address == doc + 0x3F0C:
                calls += 1
                if calls > 1: return struct.pack("<I", 0x16000000)
            return cells[address]
        self.assertFalse(decode_snapshot(read, base)["coherent"])
