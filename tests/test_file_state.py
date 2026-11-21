"""Offline tests for document flags; no process or simulator access."""
import importlib.util
import itertools
import struct
import unittest

from errors import DriverError

if importlib.util.find_spec("file_state"):
    from file_state import read_document_flags
else:
    read_document_flags = None


class FlagMemory:
    def __init__(self, **values):
        self.base = 0x810000
        # Independent literal layout from the pinned binary, eight DWORDs.
        fields = ["tool_table_file_flag", "tool_table_dirty",
                  "workpiece_file_flag", "workpiece_dirty",
                  "fixture_file_flag", "fixture_dirty", "nc_file_flag", "nc_dirty"]
        self.data = struct.pack("<8I", *(values.get(key, 0) for key in fields))
        self.calls = []

    def read(self, address, size):
        self.calls.append((address, size))
        if (address, size) != (self.base + 0x5D1830, 32):
            raise AssertionError("Read outside the verified document flag block")
        return self.data


class DocumentFlagTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(read_document_flags), "document decoder not implemented")

    def test_clean_snapshot_is_repeated_and_has_evidence(self):
        memory = FlagMemory()
        result = read_document_flags(memory.read, memory.base)
        self.assertIs(result["nc_dirty"], False)
        self.assertIs(result["workpiece_dirty"], False)
        self.assertIs(result["file_open_will_prompt"], False)
        self.assertEqual(result["file_open_prompt_reasons"], [])
        self.assertIs(result["coherent"], True)
        self.assertEqual(memory.calls, [(memory.base + 0x5D1830, 32)] * 2)
        self.assertIn("0x5d184c", result["sources"]["nc_dirty"])
        self.assertIn("SAVECURFILE", result["sources"]["file_open_will_prompt"])

    def test_nc_dirty_is_separate_from_the_native_file_open_prompt(self):
        memory = FlagMemory(nc_dirty=1)
        result = read_document_flags(memory.read, memory.base)
        self.assertIs(result["nc_dirty"], True)
        self.assertIs(result["file_open_will_prompt"], False)
        self.assertEqual(result["raw"]["nc_dirty"], 1)

    def test_native_prompt_truth_table_includes_workpiece_without_file_flag(self):
        names = ["workpiece_dirty", "workpiece_file_flag", "fixture_dirty",
                 "fixture_file_flag", "tool_table_dirty", "tool_table_file_flag"]
        for bits in itertools.product((0, 1), repeat=6):
            values = dict(zip(names, bits))
            with self.subTest(**values):
                memory = FlagMemory(**values)
                result = read_document_flags(memory.read, memory.base)
                wp, _, fx, fx_file, ct, ct_file = bits
                expected = bool(wp or (fx and fx_file) or (ct and ct_file))
                self.assertIs(result["file_open_will_prompt"], expected)
                self.assertEqual(result["file_open_prompt_reasons"],
                                 (["workpiece_dirty"] if wp else [])
                                 + (["fixture_dirty_with_file_flag"] if fx and fx_file else [])
                                 + (["tool_table_dirty_with_file_flag"] if ct and ct_file else []))

    def test_changed_snapshot_is_explicitly_incoherent(self):
        memory = FlagMemory()
        count = 0

        def changing(address, size):
            nonlocal count
            count += 1
            data = memory.read(address, size)
            return data if count == 1 else data[:-4] + struct.pack("<I", 1)

        result = read_document_flags(changing, memory.base)
        self.assertIs(result["coherent"], False)
        self.assertIs(result["nc_dirty"], False)

    def test_all_out_of_range_flags_rejected_on_either_sample(self):
        names = ["tool_table_file_flag", "tool_table_dirty", "workpiece_file_flag",
                 "workpiece_dirty", "fixture_file_flag", "fixture_dirty",
                 "nc_file_flag", "nc_dirty"]
        for name in names:
            for bad in (2, 0xFFFFFFFF):
                for sample in (1, 2):
                    with self.subTest(field=name, value=bad, sample=sample):
                        memory = FlagMemory()
                        corrupt = FlagMemory(**{name: bad})
                        count = 0

                        def read(address, size):
                            nonlocal count
                            count += 1
                            return (corrupt if count == sample else memory).read(address, size)

                        with self.assertRaises(DriverError) as raised:
                            read_document_flags(read, memory.base)
                        self.assertEqual(raised.exception.code, "invalid_document_flag")
                        self.assertEqual(raised.exception.details["field"], name)

    def test_bad_reader_or_base_does_not_read(self):
        memory = FlagMemory()
        for base in (True, 0, 0xFFFF0000, 0x100000000, "0x810000", -1):
            with self.subTest(base=base), self.assertRaises(DriverError) as raised:
                read_document_flags(memory.read, base)
            self.assertEqual(raised.exception.code, "invalid_document_reader")
        self.assertEqual(memory.calls, [])
        with self.assertRaises(DriverError):
            read_document_flags(None, memory.base)

    def test_read_failure_or_incomplete_data_is_not_clean(self):
        for fail_on in (1, 2):
            for bad in (None, b"", b"\0" * 31, b"\0" * 33, "\0" * 32, OSError("denied")):
                count = 0
                memory = FlagMemory()

                def read(address, size):
                    nonlocal count
                    count += 1
                    if count != fail_on:
                        return memory.read(address, size)
                    if isinstance(bad, BaseException):
                        raise bad
                    return bad

                with self.subTest(fail_on=fail_on, bad=repr(bad)), self.assertRaises(DriverError) as raised:
                    read_document_flags(read, memory.base)
                self.assertEqual(raised.exception.code, "memory_read_failed")

    def test_bytearray_input_is_copied(self):
        memory = FlagMemory(nc_dirty=1, workpiece_dirty=1)
        result = read_document_flags(lambda a, s: bytearray(memory.read(a, s)), memory.base)
        self.assertTrue(result["nc_dirty"] and result["workpiece_dirty"])
        self.assertTrue(result["coherent"])


if __name__ == "__main__":
    unittest.main()
