"""Capture contract tests; the native desktop and simulator are never operated."""
import importlib.util
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch


class ModuleAvailable(unittest.TestCase):
    def test_capture_and_pending_program_contract_exists(self):
        self.assertIsNotNone(importlib.util.find_spec("view_extensions"),
                             "The verified screen capture helper is not implemented")


if importlib.util.find_spec("view_extensions"):
    import view_extensions as views
    from constants import CONTROLLER_FINGERPRINT, MAIN_CLASS, TARGET_PATH, TARGET_SHA256
    from errors import DriverError

    def window(hwnd=10, pid=101, rect=(0, 0, 4, 3), owner=0, **changes):
        row = dict(hwnd=hwnd, pid=pid, cls=MAIN_CLASS if hwnd == 10 else "#32770",
                   root=hwnd, owner=owner, parent=owner, visible=True, iconic=False,
                   cloaked=False, rect=list(rect), exstyle=0)
        row.update(changes)
        return row

    class FakeBackend:
        def __init__(self):
            self.rows = [window()]
            self.fail_identity = False
            self.digest = TARGET_SHA256
        def _verify(self, process):
            if self.fail_identity:
                raise DriverError("process_changed", "Identity changed")
        def sha256(self, path): return self.digest
        def windows(self, process): return [dict(row) for row in self.rows]

    class FakeScreen:
        """Only the external OS boundary is substituted; PNG and guards stay real."""
        def __init__(self, rows=None, pixels=None):
            self.rows = rows or [window()]
            self.after = None
            self.pixels = pixels
            self.captures = 0
            self.scenes = 0
        def scene(self):
            self.scenes += 1
            rows = self.after if self.after is not None and self.scenes > 1 else self.rows
            return dict(windows=[dict(row) for row in rows], monitors=[[0, 0, 20, 20]])
        def grab(self, rect):
            self.captures += 1
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            if self.pixels is not None: return self.pixels
            # B,G,R,X; distinct original pixels make color/row mistakes observable.
            return b"".join(bytes((i % 256, (i * 3) % 256, (i * 7) % 256, 0))
                            for i in range(width * height))

    class CaptureTests(unittest.TestCase):
        def setUp(self):
            self.temp = tempfile.TemporaryDirectory()
            self.addCleanup(self.temp.cleanup)
            self.root = Path(self.temp.name)
            self.output = self.root / "capture.png"
            self.backend = FakeBackend()
            self.process = dict(pid=101, path=TARGET_PATH, creation_time=123)
            self.screen = FakeScreen()
            self.patch = patch.object(views, "WORKSPACE", str(self.root))
            self.patch.start()
            self.addCleanup(self.patch.stop)
        def capture(self, **kwargs):
            return views.capture_screenshot(self.backend, self.process, 10,
                                            str(self.output), screen=self.screen, **kwargs)
        def rejected(self, code, **kwargs):
            with self.assertRaises(DriverError) as error: self.capture(**kwargs)
            self.assertEqual(error.exception.code, code)
            self.assertFalse(self.output.exists())
        def test_exact_original_pixels_are_encoded_without_stitching_or_drawing(self):
            result = self.capture()
            raw = self.output.read_bytes()
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", raw[16:24]), (4, 3))
            offset, compressed = 8, bytearray()
            while offset < len(raw):
                size = struct.unpack(">I", raw[offset:offset + 4])[0]
                if raw[offset + 4:offset + 8] == b"IDAT":
                    compressed.extend(raw[offset + 8:offset + 8 + size])
                offset += size + 12
            pixels = zlib.decompress(compressed)
            self.assertEqual(pixels[:13], b"\0\0\0\0\x07\x03\x01\x0e\x06\x02\x15\x09\x03")
            self.assertEqual(pixels[13:17], b"\0\x1c\x0c\x04")
            self.assertTrue(result["usable"])
            self.assertFalse(result["assembled_from_multiple_captures"])
        def test_foreign_foreground_occlusion_prevents_any_capture(self):
            self.screen.rows.insert(0, window(99, 999, (1, 1, 3, 3)))
            self.rejected("capture_occluded")
            self.assertEqual(self.screen.captures, 0)
        def test_foreign_window_behind_the_target_is_not_an_occlusion(self):
            self.screen.rows.append(window(99, 999, (0, 0, 4, 3)))
            self.assertTrue(self.capture()["usable"])
        def test_owned_controller_is_included_as_real_on_screen_pixels(self):
            self.screen.rows.insert(0, window(20, rect=(2, 0, 4, 3), owner=10))
            result = self.capture()
            self.assertEqual(result["window_hwnds"], [10, 20])
            self.assertEqual(self.screen.captures, 1)
        def test_unrelated_same_process_window_is_not_included(self):
            self.screen.rows.insert(0, window(20, rect=(2, 0, 4, 3)))
            self.rejected("capture_occluded")
        def test_disallowed_owned_overlay_is_reported(self):
            self.screen.rows.insert(0, window(20, rect=(2, 0, 4, 3), owner=10))
            self.rejected("capture_occluded", include_owned=False)
        def test_minimized_main_is_reported_without_restoring_it(self):
            self.screen.rows[0]["iconic"] = True
            self.rejected("window_minimized")
            self.assertEqual(self.screen.captures, 0)
        def test_hidden_main_is_reported(self):
            self.screen.rows[0]["visible"] = False
            self.rejected("window_hidden")
        def test_offscreen_target_is_rejected_instead_of_silent_cropping(self):
            self.screen.rows[0]["rect"] = [-1, 0, 4, 3]
            self.rejected("capture_offscreen")
        def maximized(self, **changes):
            row = window(rect=(-1, -8, 21, 18), zoomed=True,
                         monitor_rect=[0, 0, 20, 20], client_screen_rect=[0, 1, 20, 17],
                         max_frame_margin=[9, 9])
            row.update(changes)
            self.screen.rows = [row]
            return row
        def test_maximized_system_border_is_clipped_and_both_rectangles_reported(self):
            self.maximized()
            result = self.capture()
            self.assertEqual(result["raw_rect"], [-1, -8, 21, 18])
            self.assertEqual(result["capture_rect"], [0, 0, 20, 18])
            self.assertEqual(result["screen_rect"], [0, 0, 20, 18])
            self.assertTrue(result["window_rectangles"][0]["maximized_frame_clipped"])
            self.assertEqual(struct.unpack(">II", self.output.read_bytes()[16:24]), (20, 18))
        def test_maximized_metadata_without_iszoomed_does_not_authorize_crop(self):
            self.maximized(zoomed=False)
            self.rejected("capture_offscreen")
            self.assertEqual(self.screen.captures, 0)
        def test_maximized_client_pixels_outside_monitor_are_not_discarded(self):
            self.maximized(client_screen_rect=[-1, 1, 20, 17])
            self.rejected("capture_offscreen")
        def test_maximized_overflow_exceeding_system_frame_is_rejected(self):
            self.maximized(rect=[-50, -8, 21, 18])
            self.rejected("capture_offscreen")
        def test_maximized_without_monitor_or_frame_evidence_is_rejected(self):
            row = self.maximized()
            del row["max_frame_margin"]
            self.rejected("capture_offscreen")
        def test_maximized_raw_border_change_is_detected_even_if_capture_rect_matches(self):
            row = self.maximized()
            self.screen.after = [dict(row, rect=[-2, -8, 21, 18])]
            self.rejected("capture_scene_changed")
            self.assertEqual(self.screen.captures, 1)
        def test_desktop_gap_between_owned_windows_is_not_captured(self):
            self.screen.rows.insert(0, window(20, rect=(6, 0, 8, 3), owner=10))
            self.rejected("capture_background_gap")
        def test_changed_occlusion_after_capture_discards_pixels(self):
            self.screen.after = [window(99, 999, (0, 0, 2, 2)), window()]
            self.rejected("capture_occluded")
            self.assertEqual(self.screen.captures, 1)
        def test_window_moving_during_capture_discards_pixels(self):
            self.screen.after = [window(rect=(1, 0, 5, 3))]
            self.rejected("capture_scene_changed")
        def test_black_capture_is_saved_but_never_claimed_usable(self):
            self.screen.pixels = b"\0" * 48
            result = self.capture()
            self.assertTrue(self.output.exists())
            self.assertFalse(result["usable"])
            self.assertIn("black_frame", result["quality"]["issues"])
        def test_truncated_pixel_buffer_is_not_written(self):
            self.screen.pixels = b"\0" * 4
            self.rejected("capture_pixel_buffer")
        def test_existing_file_is_not_overwritten(self):
            self.output.write_bytes(b"existing")
            with self.assertRaises(DriverError) as error: self.capture()
            self.assertEqual(error.exception.code, "output_exists")
            self.assertEqual(self.output.read_bytes(), b"existing")
            self.assertEqual(self.screen.captures, 0)
        def test_path_outside_workspace_is_rejected(self):
            self.output = self.root.parent / (self.root.name + "_outside.png")
            self.rejected("path_outside_workspace")
        def test_process_reuse_is_rejected_before_screen_access(self):
            self.backend.fail_identity = True
            self.rejected("process_changed")
            self.assertEqual(self.screen.scenes, 0)
        def test_wrong_binary_is_not_captured(self):
            self.backend.digest = "0" * 64
            self.rejected("binary_hash_mismatch")
            self.assertEqual(self.screen.scenes, 0)

    class ProgramEvidenceTests(unittest.TestCase):
        def test_custom_gcode_view_is_reported_pending_not_as_program_text(self):
            backend = FakeBackend()
            backend.rows += [dict(hwnd=30, pid=101, cls="SysListView32", id=59648,
                                  parent=20, root=10, visible=True)]
            result = views.read_program_page(backend, dict(pid=101, path=TARGET_PATH,
                                                          creation_time=123), 10, 5, 20)
            self.assertFalse(result["supported"])
            self.assertEqual(result["outcome"], "pending")
            self.assertEqual(result["lines"], [])
            self.assertFalse(result["complete_program"])
            self.assertEqual(result["visible_candidate_controls"][0]["hwnd"], 30)
        def test_pagination_limits_are_validated_even_when_pending(self):
            for start, count in [(0, 10), (1, 0), (1, 501), (True, 10)]:
                with self.assertRaises(DriverError) as error:
                    views.read_program_page(FakeBackend(), {}, 10, start, count)
                self.assertEqual(error.exception.code, "invalid_pagination")
        def test_arbitrary_angles_and_unverified_reset_are_not_advertised_supported(self):
            result = views.view_capabilities()
            self.assertFalse(result["arbitrary_angles"]["supported"])
            self.assertEqual(result["pending"]["reset"]["runtime_verified"], False)

    class PageBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.rows += [dict(hwnd=12, pid=101, cls="MDIClient", id=59648,
                               parent=10, root=10, visible=True),
                          dict(hwnd=20, pid=101, cls="Afx:00590000:b:00010003", id=65280,
                               parent=12, root=10, visible=True),
                          dict(hwnd=30, pid=101, cls="SysListView32", id=59648,
                               parent=20, root=10, visible=True)]
            self.count, self.capacity, self.top = 100, 10, 0
            self.messages = []
            self.u = self
        def GetWindowLongW(self, hwnd, index): return 1  # LVS_REPORT
        def _check_window(self, process, hwnd, expected_class=None):
            row = next(row for row in self.rows if row["hwnd"] == hwnd)
            if expected_class and row["cls"] != expected_class:
                raise DriverError("window_changed", "Class changed")
            return row
        def _send(self, hwnd, message, wp=0, lp=0, timeout=500):
            if message == 0x1004: return self.count
            if message == 0x1028: return self.capacity
            if message == 0x1027: return self.top
            if message == 0x1013:
                self.messages.append((hwnd, message, wp, lp))
                if wp < self.top: self.top = wp
                elif wp >= self.top + self.capacity: self.top = wp - self.capacity + 1
                return 1
            raise AssertionError("Unexpected or pointer-bearing message")

    class ProgramPageTests(unittest.TestCase):
        def setUp(self):
            self.backend = PageBackend()
            self.process = dict(pid=101, path=TARGET_PATH, creation_time=123)
        def test_page_scroll_readback_reports_actual_display_range(self):
            result = views.show_program_page(self.backend, self.process, 10, 21, 10)
            self.assertEqual(result["actual_visible_range"], {"first": 21, "last": 30})
            self.assertFalse(result["program_selection_changed"])
            self.assertFalse(result["source_line_mapping_verified"])
            self.assertEqual(self.backend.messages, [(30, 0x1013, 20, 0), (30, 0x1013, 29, 0)])
        def test_last_page_reports_short_actual_range(self):
            result = views.show_program_page(self.backend, self.process, 10, 96, 10)
            self.assertEqual(result["actual_visible_range"], {"first": 91, "last": 100})
        def test_out_of_range_request_sends_no_scroll(self):
            with self.assertRaises(DriverError) as error:
                views.show_program_page(self.backend, self.process, 10, 101, 10)
            self.assertEqual(error.exception.code, "program_line_out_of_range")
            self.assertEqual(self.backend.messages, [])
        def test_folder_list_is_not_mistaken_for_gcode(self):
            self.backend.rows[2]["id"] = 99
            with self.assertRaises(DriverError) as error:
                views.show_program_page(self.backend, self.process, 10, 1, 10)
            self.assertEqual(error.exception.code, "program_view_unavailable")
            self.assertEqual(self.backend.messages, [])
        def test_ambiguous_gcode_views_are_rejected_before_scroll(self):
            self.backend.rows.append(dict(self.backend.rows[-1], hwnd=31))
            with self.assertRaises(DriverError) as error:
                views.show_program_page(self.backend, self.process, 10, 1, 10)
            self.assertEqual(error.exception.code, "program_view_ambiguous")
            self.assertEqual(self.backend.messages, [])

    class ControllerPageDecodeTests(unittest.TestCase):
        def setUp(self):
            self.base, self.crt = 0x590000, 0x200000
            self.control = self.crt+0x11638
            self.memory = {self.base+0x5CB180: self.crt, self.crt+0x20: 40,
                           self.crt+0x1D750: 1, self.crt+0x1263C: 0,
                           self.control+0x20: 50, self.control+0xD0: self.base+0x5D1850,
                           self.control+0x430: 20, self.control+0x434: 0,
                           self.control+0x438: -1, self.control+0x43C: 20,
                           self.control+0x440: 10, self.base+0x5D1854: 231}
        def read(self, address, size):
            self.assertEqual(size, 4)
            return (self.memory[address] & 0xFFFFFFFF).to_bytes(4, "little")
        def test_static_crt_offsets_read_real_model_cursor_and_top(self):
            result = views._decode_controller_program_state(self.read, self.base)
            self.assertEqual(result["top_row"], 20)
            self.assertEqual(result["cursor_row"], 20)
            self.assertEqual(result["active_row"], -1)
            self.assertEqual(result["row_count"], 231)
            self.assertTrue(result["coherent"])
        def test_changed_native_counter_is_reported_incoherent(self):
            reads = []
            def changing(address, size):
                reads.append(address)
                if address == self.control+0x43C and reads.count(address) == 2:
                    self.memory[address] = 21
                return self.read(address, size)
            self.assertFalse(views._decode_controller_program_state(changing, self.base)["coherent"])
        def test_non_nc_model_is_rejected(self):
            self.memory[self.control+0xD0] = self.base+0x5D1870
            with self.assertRaises(DriverError) as error:
                views._decode_controller_program_state(self.read, self.base)
            self.assertEqual(error.exception.code, "program_page_model_mismatch")
        def test_out_of_range_controller_pointer_is_not_dereferenced(self):
            self.memory[self.base+0x5CB180] = 1
            with self.assertRaises(DriverError) as error:
                views._decode_controller_program_state(self.read, self.base)
            self.assertEqual(error.exception.code, "controller_pointer_invalid")
        def test_truncated_memory_read_is_rejected(self):
            with self.assertRaises(DriverError) as error:
                views._decode_controller_program_state(lambda address, size: b"\0", self.base)
            self.assertEqual(error.exception.code, "program_page_unreadable")

    class ControllerBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.base, self.messages = 0x590000, []
            self.rows[0]["enabled"] = True
            self.rows.append(window(40, owner=10, root=40, enabled=True))
            fingerprints = set(CONTROLLER_FINGERPRINT) | {("Button", i) for i in (7031, 7042, 7043)}
            for cls, ident in sorted(fingerprints):
                self.rows.append(window(ident+10000, cls=cls, id=ident, parent=40, root=40,
                                        owner=0, enabled=True))
            self.rows.extend([window(45, id=0, parent=40, root=40, visible=False, enabled=True),
                              window(50, cls="Static", id=0, parent=45, root=40,
                                     visible=False, enabled=True)])
            self.raw = dict(document=0x50000, descriptor_array=0x60000, descriptor_count=241,
                            index=0, source_line=0, xyz=[0.0, 0.0, 0.0], mode=1, power=1,
                            worker=0, cycle=0, emergency=0, coherent=True, m30_count=0, end_count=0)
            self.page = dict(crt_pointer=0x200000, controller_hwnd=40, page=0, subpage=0,
                             text_control=0x211638, text_hwnd=50, model=self.base+0x5D1850,
                             row_count=231, top_row=0, cursor_row=0, cursor_column=0,
                             active_row=-1, page_capacity=10, coherent=True)
            self.digest_source = "a"*64
            self.change = None
        def module_base(self, process): return self.base
        def probe(self, hwnd): return True
        def read_state(self, process, base): return dict(self.raw, xyz=list(self.raw["xyz"]))
        def program_source(self, process, base):
            return dict(coherent=True, sha256=self.digest_source, byte_length=100, text="O1000\nM30\n")
        def read_text(self, process, hwnd, limit=32768):
            return {17031: "PROG", 17042: "PAGEUP", 17043: "PAGEDOWN"}.get(hwnd, "")
        def _check_window(self, process, hwnd, expected_class=None):
            self._verify(process)
            row = next(row for row in self.rows if row["hwnd"] == hwnd)
            if expected_class and row["cls"] != expected_class:
                raise DriverError("window_changed", "Unexpected class")
            if not row["visible"] or not row["enabled"]:
                raise DriverError("control_disabled", "Hidden or disabled")
            return dict(row)
        def _send(self, hwnd, message, wp=0, lp=0, timeout=500):
            self.messages.append((hwnd, message, wp, lp))
            if (hwnd, message, lp) != (40, 0x111, wp+10000): raise AssertionError("Unexpected native operation")
            if wp == 7031:
                self.page.update(page=1, subpage=0)
                for row in self.rows:
                    if row["hwnd"] in (45, 50): row["visible"] = True
            elif wp == 7042:
                self.page["top_row"] = max(0, self.page["top_row"] - 10)
                self.page["cursor_row"] = self.page["top_row"]
            elif wp == 7043:
                self.page["top_row"] += 10
                self.page["cursor_row"] = self.page["top_row"]
            else: raise AssertionError("Unrecognized command")
            if self.change: self.change(self)
            return 0

    class ControllerPageTests(unittest.TestCase):
        def setUp(self):
            self.backend = ControllerBackend()
            self.process = dict(pid=101, path=TARGET_PATH, creation_time=123)
        def page(self, action):
            helper = getattr(views, "controller_program_page", None)
            self.assertIsNotNone(helper, "Guarded controller paging is missing")
            with patch.object(views, "_read_controller_program_state",
                              side_effect=lambda *a: dict(self.backend.page)):
                return helper(self.backend, self.process, 10, action)
        def open_page(self):
            result = self.page("open")
            self.backend.messages.clear()
            return result
        def test_open_uses_native_prog_and_reports_verified_page(self):
            result = self.page("open")
            self.assertEqual(self.backend.messages, [(40, 0x111, 7031, 17031)])
            self.assertEqual(result["after"]["page"], 1)
            self.assertTrue(result["execution_state_unchanged"])
            self.assertTrue(result["source_content_unchanged"])
            self.assertFalse(result["exact_visible_range_verified"])
        def test_next_previous_read_back_actual_cursor_without_changing_run_position(self):
            self.open_page()
            result = self.page("next")
            self.assertEqual(result["after"]["top_row"], 10)
            self.assertEqual(result["after"]["cursor_row"], 10)
            self.assertTrue(result["selection_may_change"])
            self.assertEqual(self.page("previous")["after"]["top_row"], 0)
            self.assertEqual([item[2] for item in self.backend.messages], [7043, 7042])
        def test_already_open_does_not_reset_page_or_cursor(self):
            self.open_page()
            self.backend.page.update(top_row=20, cursor_row=20)
            result = self.page("open")
            self.assertEqual(result["outcome"], "already_open")
            self.assertEqual(result["after"]["top_row"], 20)
            self.assertEqual(self.backend.messages, [])
        def test_wrong_mode_running_emergency_and_modal_send_no_command(self):
            for condition in ("mode", "worker", "emergency", "modal"):
                with self.subTest(condition=condition):
                    self.backend = ControllerBackend()
                    if condition == "mode": self.backend.raw["mode"] = 2
                    elif condition == "modal": self.backend.rows.insert(0, window(80, owner=10, enabled=True))
                    else: self.backend.raw[condition] = 1
                    with self.assertRaises(DriverError): self.page("open")
                    self.assertEqual(self.backend.messages, [])
        def test_paging_without_visible_default_program_page_sends_no_command(self):
            with self.assertRaises(DriverError): self.page("next")
            self.assertEqual(self.backend.messages, [])
            self.open_page()
            self.backend.page["subpage"] = 4
            with self.assertRaises(DriverError): self.page("next")
            self.assertEqual(self.backend.messages, [])
        def test_running_position_change_during_button_processing_is_never_success(self):
            self.open_page()
            self.backend.change = lambda b: b.raw.update(index=1)
            with self.assertRaises(DriverError) as error: self.page("next")
            self.assertEqual(error.exception.code, "execution_state_changed")
            self.assertEqual(len(self.backend.messages), 1)
        def test_source_change_is_reported_after_one_command_without_automatic_retry(self):
            self.open_page()
            self.backend.change = lambda b: setattr(b, "digest_source", "b"*64)
            with self.assertRaises(DriverError) as error: self.page("next")
            self.assertEqual(error.exception.code, "program_content_changed")
            self.assertEqual(len(self.backend.messages), 1)
        def test_wrong_text_model_and_controller_pointer_are_rejected_before_input(self):
            for key, value in (("model", 0x40000), ("controller_hwnd", 80)):
                with self.subTest(key=key):
                    self.backend = ControllerBackend()
                    self.backend.page[key] = value
                    with self.assertRaises(DriverError): self.page("open")
                    self.assertEqual(self.backend.messages, [])
        def test_hidden_duplicate_native_buttons_do_not_ambigify_visible_key(self):
            original = next(row for row in self.backend.rows if row.get("id") == 7031)
            self.backend.rows.append(dict(original, hwnd=88031, visible=False))
            result = self.page("open")
            self.assertTrue(result["command_sent"])
            self.assertEqual(self.backend.messages, [(40, 0x111, 7031, 17031)])

        def test_bad_action_is_rejected_before_native_queries(self):
            with self.assertRaises(DriverError) as error: self.page("start")
            self.assertEqual(error.exception.code, "invalid_program_page_action")
            self.assertEqual(self.backend.messages, [])

    class RotationBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.rows.append(dict(hwnd=30, pid=101, cls="AfxFrameOrView100s", id=59648,
                                  parent=20, root=10, visible=True))
            self.view = dict(view_pointer=0x200000, hwnd=30, interaction_mode=0,
                             left_drag=0, right_drag=0, alternate_view=0, special_layout=0,
                             overlay_enabled=0, coherent=True)
            self.state = dict(coherent=True, worker=0, cycle=0, emergency=0)
            self.messages, self.move_failure = [], False
        def module_base(self, process): return 0x590000
        def read_state(self, process, base): return self.state
        def _check_window(self, process, hwnd, expected_class=None):
            row = next(row for row in self.rows if row["hwnd"] == hwnd)
            if expected_class and row["cls"] != expected_class:
                raise DriverError("window_changed", "Class changed")
            return row
        def _send(self, hwnd, msg, wp=0, lp=0, timeout=500):
            self.messages.append((hwnd, msg, wp, lp))
            if msg == 0x111: self.view["interaction_mode"] = 32777
            elif msg == 0x201: self.view["left_drag"] = 1
            elif msg == 0x202: self.view["left_drag"] = 0
            elif msg == 0x200 and self.move_failure:
                raise DriverError("message_timeout", "Move timed out")
            return 0

    class RotationTests(unittest.TestCase):
        def setUp(self):
            self.backend = RotationBackend()
            self.process = dict(pid=101, path=TARGET_PATH, creation_time=123)
        def rotate(self, dx=0.1, dy=-0.1):
            function = getattr(views, "rotate_view_relative", None)
            self.assertIsNotNone(function, "Bounded relative rotation helper is missing")
            with patch.object(views, "_read_view_state", side_effect=lambda *a: dict(self.backend.view)), \
                    patch.object(views, "_client_rect", return_value=(0, 0, 400, 300)):
                return function(self.backend, self.process, 10, dx, dy)
        def test_relative_drag_uses_view_client_coordinates_and_releases_button(self):
            result = self.rotate()
            self.assertEqual(result["start_client"], [200, 150])
            self.assertEqual(result["end_client"], [240, 120])
            self.assertFalse(result["absolute_angle_verified"])
            self.assertEqual(self.backend.messages[0], (10, 0x111, 32777, 0))
            self.assertEqual(self.backend.messages[-1], (30, 0x202, 0, (120 << 16) | 240))
            self.assertEqual(self.backend.view["left_drag"], 0)
        def test_already_rotate_mode_is_not_toggled_off(self):
            self.backend.view["interaction_mode"] = 32777
            self.rotate()
            self.assertTrue(all(msg[1] != 0x111 for msg in self.backend.messages))
        def test_running_simulation_rejects_drag_before_any_message(self):
            self.backend.state["worker"] = 1
            with self.assertRaises(DriverError) as error: self.rotate()
            self.assertEqual(error.exception.code, "busy")
            self.assertEqual(self.backend.messages, [])
        def test_existing_user_drag_is_not_interrupted(self):
            self.backend.view["right_drag"] = 1
            with self.assertRaises(DriverError) as error: self.rotate()
            self.assertEqual(error.exception.code, "view_interaction_busy")
            self.assertEqual(self.backend.messages, [])
        def test_special_layout_is_rejected_instead_of_dragging_an_unknown_surface(self):
            self.backend.view["alternate_view"] = 1
            with self.assertRaises(DriverError) as error: self.rotate()
            self.assertEqual(error.exception.code, "view_context_unsupported")
            self.assertEqual(self.backend.messages, [])
        def test_invalid_displacements_never_send_input(self):
            for dx, dy in [(float("nan"), 0), (0.9, 0), (0, 0), (True, 0.1)]:
                with self.assertRaises(DriverError) as error: self.rotate(dx, dy)
                self.assertEqual(error.exception.code, "invalid_rotation")
            self.assertEqual(self.backend.messages, [])
        def test_failed_move_still_releases_original_target_once(self):
            self.backend.move_failure = True
            with self.assertRaises(DriverError) as error: self.rotate()
            self.assertEqual(error.exception.code, "message_timeout")
            self.assertEqual(sum(message[1] == 0x202 for message in self.backend.messages), 1)
            self.assertEqual(self.backend.view["left_drag"], 0)


if __name__ == "__main__":
    unittest.main()
