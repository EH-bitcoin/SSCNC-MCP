"""Offline byte fixtures for the pinned MF layouts; never attach to SSCNC."""
import hashlib
import importlib.util
import math
import struct
import unittest

from errors import DriverError

if importlib.util.find_spec("read_setup"):
    from read_setup import read_program_source, read_setup
else:
    read_program_source = read_setup = None


class ByteMemory:
    def __init__(self):
        self.cells = {}
        self.reads = []

    def put(self, address, data):
        self.cells.update((address + i, value) for i, value in enumerate(data))

    def scalar(self, address, fmt, value):
        self.put(address, struct.pack("<" + fmt, value))

    def cstring(self, object_address, data_address, text):
        raw = text.encode("gbk")
        self.scalar(object_address, "I", data_address)
        self.put(data_address - 12, struct.pack("<iii", len(raw), len(raw), 1))
        self.put(data_address, raw + b"\0")

    def read(self, address, size):
        self.reads.append((address, size))
        try:
            return bytes(self.cells[address + i] for i in range(size))
        except KeyError as exc:
            raise OSError("fixture does not map this address") from exc


def setup_fixture():
    # Literal, independently derived from mf_static evidence; relocated image.
    base, doc = 0x810000, 0x20000000
    table, records = 0x21000000, 0x21000100
    tool1, tool12, tool13 = 0x22000000, 0x22001000, 0x22002000
    mem = ByteMemory()
    for rva, fmt, value in [
        (0x5CB178, "I", doc), (0x5C6924, "I", 0),
        (0x5CC83E, "B", 0), (0x5CB418, "I", 1),
        (0x5CCA90, "I", 2), (0x5CB184, "I", table),
        (0x5CB464, "i", 5), (0x5CCB24, "i", 24),
        (0x5CC9F8, "d", -450), (0x5CCA00, "d", -250),
        (0x5CCA08, "d", -145),
    ]:
        mem.scalar(base + rva, fmt, value)
    mem.cstring(base + 0x5D1814, 0x23000100, "C:\\Programs\\示例.cnc")
    mem.cstring(base + 0x5D181C, 0x23001100, "C:\\Workpieces\\example.wp")
    for index in range(6):
        for axis, value in enumerate([-450 + index * 20, -250 + index * 30,
                                       -145 + index * 40]):
            mem.scalar(base + 0x5CD758 + index * 0x30 + axis * 8, "d", value)
    for axis, value in enumerate([1, 2, 3]):
        mem.scalar(base + 0x5CD728 + axis * 8, "d", value)
    for rva, entries in [(0x5CE198, {3: -7.5, 5: -17}),
                         (0x5CEE20, {3: 0.25}),
                         (0x5CFAA8, {400: 8}), (0x5D0730, {400: 0.2})]:
        mem.put(base + rva, struct.pack("<401d", *(entries.get(i, 0) for i in range(401))))
    mem.scalar(base + 0x5D1458, "i", 1)
    for offset, value in [
        (0x730, 800), (0x738, 400), (0x740, 350),
        (0x3D0, 0), (0x3D8, 0), (0x3E0, 0),
        (0x5C8, 35), (0x5D0, 7), (0x5D8, 5),
        # WP serializes +58, +48, +68 in X/Y/Z order.
        (0x3FA0, 120), (0x3F90, 80), (0x3FB0, 40),
    ]:
        mem.scalar(doc + offset, "d", value)
    mem.scalar(doc + 0x3F80, "i", 0)
    mem.scalar(doc + 0x368, "I", tool13)
    for i in range(24):
        mem.scalar(doc + 0x36C + 4 * i, "I", {0: tool1, 2: tool12}.get(i, 0))
    mem.scalar(table + 8, "I", records)
    mem.scalar(table + 12, "i", 25)
    for i in range(25):
        number, identifier, label = {
            0: (1, 1, b"001"), 2: (3, 12, b"012"),
            24: (5, 13, b"013"),
        }.get(i, (i + 1, -1, b""))
        mem.put(records + i * 72, struct.pack("<ii64s", number, identifier, label))
    for pointer, identifier, kind, diameter, length in [
        (tool1, 1, 11, 8, 65), (tool12, 12, 0, 8, 112.5),
        (tool13, 13, 0, 2, 103),
    ]:
        for offset, fmt, value in [
            (8, "i", kind), (0x18, "d", diameter), (0x20, "d", length),
            (0x88, "B", 0), (0x98, "d", 55), (0xD8, "i", identifier),
        ]:
            mem.scalar(pointer + offset, fmt, value)
    return mem, base, doc, table, records, tool12, tool13


class SetupReadTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(read_setup), "read_setup public decoder is missing")

    def test_relocated_setup_decodes_independent_tool_mapping_and_stock_axes(self):
        mem, base, doc, *_ = setup_fixture()
        result = read_setup(mem.read, base)
        self.assertEqual(result["document"], doc)
        self.assertEqual(result["files"]["program"], "C:\\Programs\\示例.cnc")
        self.assertEqual(result["files"]["workpiece"], "C:\\Workpieces\\example.wp")
        self.assertEqual(result["offsets"]["active_work_xyz_mm"], [-450, -250, -145])
        self.assertEqual(result["offsets"]["g54_xyz_mm"], [-450, -250, -145])
        self.assertEqual(result["stock"]["size_xyz_mm"], [120, 80, 40])
        self.assertEqual(result["machine"]["travel_xyz_mm"], [800, 400, 350])
        self.assertEqual(result["fixture"]["base_height_mm"], 42)
        self.assertEqual(result["fixture"]["shift_x_mm"], 5)
        tools = result["tools"]
        self.assertEqual(tools["current_number_raw"], 5)
        self.assertEqual(tools["mapping"][24]["t_number"], 5)
        self.assertEqual(tools["mapping"][24]["library_id"], 13)
        self.assertEqual(tools["slots"][2]["tool"]["library_id"], 12)
        self.assertEqual(tools["spindle"]["effective_length_mm"], 103)
        self.assertFalse(tools["spindle"]["damaged"])
        self.assertTrue(result["coherent"])
        self.assertTrue(result["sources"])
        self.assertIn("fixture.full_dimensions_mm", {u["field"] for u in result["unknowns"]})

    def test_persistent_work_coordinates_are_not_copied_from_active_offsets(self):
        mem, base, *_ = setup_fixture()
        for axis, value in enumerate([-123, -456, -789]):
            mem.scalar(base + 0x5CC9F8 + axis * 8, "d", value)
        offsets = read_setup(mem.read, base)["offsets"]
        self.assertEqual(offsets["active_work_xyz_mm"], [-123, -456, -789])
        self.assertEqual(offsets["g54_xyz_mm"], [-450, -250, -145])
        self.assertEqual(offsets["work_systems_xyz_mm"]["G59"], [-350, -100, 55])
        self.assertEqual(offsets["common_work_xyz_mm"], [1, 2, 3])

    def test_full_offset_tables_expose_nonzero_rows_and_independent_h_d_indices(self):
        mem, base, *_ = setup_fixture()
        table = read_setup(mem.read, base)["offsets"]["tool_length_compensation"]
        self.assertEqual(table["row_count"], 401)
        self.assertTrue(table["zero_rows_omitted"])
        self.assertEqual([row["index"] for row in table["entries"]], [3, 5, 400])
        self.assertEqual(table["entries"][0]["h0_mm"], -7.5)
        self.assertEqual(table["entries"][0]["h1_mm"], 0.25)
        self.assertEqual(table["entries"][0]["h_sum_mm"], -7.25)
        self.assertAlmostEqual(table["entries"][2]["d_sum_mm"], 8.2)
        self.assertAlmostEqual(table["entries"][2]["cutter_radius_mm"], 4.1)

    def test_radius_option_does_not_divide_a_radius_table_again(self):
        mem, base, *_ = setup_fixture()
        mem.scalar(base + 0x5D1458, "i", 0)
        table = read_setup(mem.read, base)["offsets"]["tool_length_compensation"]
        self.assertFalse(table["diameter_mode"])
        self.assertAlmostEqual(table["entries"][-1]["cutter_radius_mm"], 8.2)

    def test_nonfinite_offset_table_is_unknown_instead_of_zero_filled(self):
        mem, base, *_ = setup_fixture()
        mem.scalar(base + 0x5CE198 + 399 * 8, "d", math.nan)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["offsets"]["tool_length_compensation"])
        self.assertIn("offsets.tool_length_compensation", {u["field"] for u in result["unknowns"]})

    def test_collision_damaged_flat_uses_remaining_length_instead_of_nominal(self):
        mem, base, _, _, _, tool12, _ = setup_fixture()
        mem.scalar(tool12 + 0x88, "B", 1)
        result = read_setup(mem.read, base)["tools"]["slots"][2]["tool"]
        self.assertTrue(result["damaged"])
        self.assertEqual(result["nominal_length_mm"], 112.5)
        self.assertEqual(result["effective_length_mm"], 60)

    def test_damaged_flat_piecewise_boundaries_do_not_overstate_tool_reach(self):
        # Independent outcomes from native branches; catches reversed FPU comparisons.
        for remaining, expected in [(100, 50), (95, 95), (90, 90),
                                    (85, 90), (80, 85), (70, 75), (10, 40)]:
            with self.subTest(remaining=remaining):
                mem, base, _, _, _, tool12, _ = setup_fixture()
                mem.scalar(tool12 + 0x20, "d", 100)
                mem.scalar(tool12 + 0x88, "B", 1)
                mem.scalar(tool12 + 0x98, "d", remaining)
                result = read_setup(mem.read, base)["tools"]["slots"][2]["tool"]
                self.assertEqual(result["effective_length_mm"], expected)

    def test_unimplemented_damaged_tool_type_is_unknown_not_nominal(self):
        mem, base, _, _, _, _, tool13 = setup_fixture()
        mem.scalar(tool13 + 8, "i", 19)
        mem.scalar(tool13 + 0x88, "B", 1)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["tools"]["spindle"]["effective_length_mm"])
        self.assertTrue(any("effective_length_mm" in u["field"] for u in result["unknowns"]))

    def test_null_document_does_not_dereference_document_fields(self):
        mem, base, *_ = setup_fixture()
        mem.scalar(base + 0x5CB178, "I", 0)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["stock"]["size_xyz_mm"])
        self.assertIsNone(result["tools"]["spindle"])
        self.assertTrue(all(address >= 0x10000 for address, _ in mem.reads))

    def test_invalid_document_fails_before_dereferencing_it(self):
        mem, base, *_ = setup_fixture()
        mem.scalar(base + 0x5CB178, "I", 4)
        with self.assertRaises(DriverError) as caught:
            read_setup(mem.read, base)
        self.assertEqual(caught.exception.code, "invalid_document")
        self.assertFalse(any(address < 0x10000 for address, _ in mem.reads))

    def test_invalid_tool_pointer_is_reported_without_unsafe_read(self):
        mem, base, doc, *_ = setup_fixture()
        mem.scalar(doc + 0x368, "I", 4)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["tools"]["spindle"])
        self.assertIn("tools.spindle", {u["field"] for u in result["unknowns"]})
        self.assertFalse(any(address < 0x10000 for address, _ in mem.reads))

    def test_corrupt_ct_count_is_bounded_before_reading_record_storage(self):
        mem, base, _, table, records, *_ = setup_fixture()
        mem.scalar(table + 12, "i", 2_000_000_000)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["tools"]["mapping"])
        self.assertFalse(any(records <= address < records + 1800 for address, _ in mem.reads))

    def test_invalid_cstring_length_does_not_scan_unbounded_memory(self):
        mem, base, *_ = setup_fixture()
        mem.scalar(0x23000100 - 12, "i", 1_000_000_000)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["files"]["program"])
        self.assertFalse(any(address == 0x23000100 for address, _ in mem.reads))

    def test_short_optional_read_is_unknown_not_an_empty_value(self):
        mem, base, *_ = setup_fixture()
        def short_read(address, size):
            data = mem.read(address, size)
            return data[:-1] if address == base + 0x5D181C else data
        result = read_setup(short_read, base)
        self.assertIsNone(result["files"]["workpiece"])
        self.assertIn("files.workpiece", {u["field"] for u in result["unknowns"]})

    def test_nonfinite_dimensions_do_not_escape_as_json_nan(self):
        mem, base, doc, _, _, _, tool13 = setup_fixture()
        mem.scalar(doc + 0x3FA0, "d", math.nan)
        mem.scalar(tool13 + 0x20, "d", math.inf)
        result = read_setup(mem.read, base)
        self.assertIsNone(result["stock"]["size_xyz_mm"])
        self.assertIsNone(result["tools"]["spindle"]["nominal_length_mm"])
        self.assertIsNone(result["tools"]["spindle"]["effective_length_mm"])

    def test_in_place_tool_change_marks_snapshot_incoherent(self):
        mem, base, _, _, _, _, tool13 = setup_fixture()
        calls = 0
        def changing(address, size):
            nonlocal calls
            if address == tool13 + 0x88:
                calls += 1
                if calls > 1:
                    return b"\x01"
            return mem.read(address, size)
        self.assertFalse(read_setup(changing, base)["coherent"])


class ProgramSourceTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(read_program_source), "read_program_source is missing")

    def fixture(self, raw=b"%\r\nO1234\r\nG1 Z-10.\r\nM30\r\n%"):
        mem, base, *_ = setup_fixture()
        pointer = 0x24000000
        mem.scalar(base + 0x5D1868, "I", 4096)
        mem.scalar(base + 0x5D186C, "I", pointer)
        mem.put(pointer, (raw + b"\0").ljust(4096, b"\0"))
        return mem, base, pointer, raw

    def test_reads_actual_source_buffer_and_hashes_original_bytes(self):
        mem, base, _, raw = self.fixture()
        result = read_program_source(mem.read, base)
        self.assertEqual(result["text"], raw.decode("ascii"))
        self.assertEqual(result["byte_length"], len(raw))
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(result["coherent"])

    def test_unterminated_buffer_is_rejected_instead_of_reported_complete(self):
        mem, base, pointer, _ = self.fixture()
        mem.put(pointer, b"X" * 4096)
        with self.assertRaises(DriverError) as caught:
            read_program_source(mem.read, base)
        self.assertEqual(caught.exception.code, "invalid_program_source")

    def test_raw_buffer_does_not_require_a_cstring_header(self):
        mem, base, pointer, _ = self.fixture(b"M30")
        self.assertEqual(read_program_source(mem.read, base)["text"], "M30")
        self.assertFalse(any(pointer - 16 <= address < pointer for address, _ in mem.reads))

    def test_cp936_character_split_across_chunks_is_preserved(self):
        raw = b"A" * 4095 + "(凹槽)\r\nM30".encode("gbk")
        mem, base, pointer, _ = self.fixture(raw)
        mem.scalar(base + 0x5D1868, "I", 8192)
        mem.put(pointer, (raw + b"\0").ljust(8192, b"\0"))
        result = read_program_source(mem.read, base)
        self.assertEqual(result["text"], raw.decode("gbk"))
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())

    def test_large_allocation_still_stops_at_one_megabyte_read_limit(self):
        mem, base, pointer, _ = self.fixture()
        mem.scalar(base + 0x5D1868, "I", 2 * 1024 * 1024)
        source_reads = []
        def unterminated(address, size):
            if pointer <= address < pointer + 2 * 1024 * 1024:
                source_reads.append((address, size))
                return b"X" * size
            return mem.read(address, size)
        with self.assertRaises(DriverError) as caught:
            read_program_source(unterminated, base)
        self.assertEqual(caught.exception.code, "invalid_program_source")
        self.assertEqual(sum(size for _, size in source_reads), 1024 * 1024)
        self.assertLessEqual(max(address + size for address, size in source_reads),
                             pointer + 1024 * 1024)

    def test_same_pointer_content_change_marks_source_incoherent(self):
        mem, base, pointer, _ = self.fixture()
        calls = 0
        def changed(address, size):
            nonlocal calls
            data = mem.read(address, size)
            if address == pointer:
                calls += 1
                if calls > 1:
                    return b"!" + data[1:]
            return data
        self.assertFalse(read_program_source(changed, base)["coherent"])

    def test_null_or_wrapping_source_pointer_is_rejected_before_content_read(self):
        for value in (0, 4, 0xFFFFFFF0):
            with self.subTest(pointer=value):
                mem, base, _, _ = self.fixture()
                mem.scalar(base + 0x5D186C, "I", value)
                with self.assertRaises(DriverError):
                    read_program_source(mem.read, base)
                self.assertFalse(any(address == value for address, _ in mem.reads))


if __name__ == "__main__":
    unittest.main()
