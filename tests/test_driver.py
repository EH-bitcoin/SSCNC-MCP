"""Contract tests: no running simulator is changed by this suite."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ModuleAvailable(unittest.TestCase):
    def test_driver_contract_is_implemented(self):
        self.assertIsNotNone(importlib.util.find_spec("driver"), "The guarded driver is not implemented")


if importlib.util.find_spec("driver"):
    from constants import TARGET_PATH, TARGET_SHA256
    from driver import Driver, DriverError

    def window(hwnd, cls, control_id=0, parent=0, root=None, enabled=True, visible=True):
        return dict(hwnd=hwnd, pid=101, cls=cls, id=control_id, parent=parent,
                    root=hwnd if root is None else root, owner=0, enabled=enabled, visible=visible)

    def snapshot(**changes):
        result = dict(document=0x220000, index=0, source_line=0, descriptor_count=40,
                      xyz=[0.0, 0.0, 0.0], cycle=0, worker=0, emergency=0,
                      mode=2, power=1, current_mcode=0, m30_count=0, end_count=0,
                      coherent=True, timestamp=1.0)
        result.update(changes)
        return result

    class FakeBackend:
        """OS boundary only: explicit state transitions and actual outgoing command log."""
        def __init__(self):
            self.processes = [dict(pid=101, path=TARGET_PATH, creation_time=123)]
            self.digest = TARGET_SHA256
            self.rows = [window(10, "ClassName_Swansoft_CNC_Simulation"),
                         window(20, "#32770", parent=10, root=10),
                         window(30, "Button", 9518, parent=20, root=10),
                         window(31, "Button", 9508, parent=20, root=10),
                         window(32, "Button", 9500, parent=20, root=10)]
            self.responsive = True
            self.states = [snapshot()]
            self.commands = []
            self.now = 0.0
            self.on_command = None
            self.selected = None
            self.file_text = ""
            self.dialog_phase = 0
            self.list_data = [7, 9, 1]

        def list_processes(self): return list(self.processes)
        def sha256(self, path): return self.digest
        def module_base(self, process): return 0x800000
        def windows(self, process): return [dict(w) for w in self.rows]
        def probe(self, hwnd): return self.responsive
        def read_state(self, process, base):
            value = dict(self.states[0])
            if len(self.states) > 1: self.states.pop(0)
            return value
        def monotonic(self): return self.now
        def sleep(self, seconds): self.now += seconds
        def post_command(self, process, hwnd, command, control=0):
            self.commands.append((hwnd, command, control))
            if self.on_command: self.on_command(hwnd, command, control)
        def select_workpiece(self, process, hwnd):
            matches = [i for i, data in enumerate(self.list_data) if data == 1]
            if len(matches) != 1: raise DriverError("ambiguous_workpiece_item", "item data 1 not unique")
            self.selected = matches[0]
            return dict(index=matches[0], item_data=1)
        def read_workpiece_selection(self, process, hwnd):
            if self.selected is None or self.list_data[self.selected] != 1:
                raise DriverError("selection_changed", "Workpiece is no longer selected")
            return dict(index=self.selected, item_data=1)
        def set_text_readback(self, process, hwnd, text):
            self.file_text = text
            return text

    class DriverTests(unittest.TestCase):
        def setUp(self):
            workspace_patch = patch('driver.WORKSPACE', str(Path(__file__).resolve().parents[1]))
            workspace_patch.start()
            self.addCleanup(workspace_patch.stop)
            self.backend = FakeBackend()
            self.driver = Driver(self.backend, wait_timeout=0.7)

        def assert_rejected(self, code, call):
            with self.assertRaises(DriverError) as context: call()
            self.assertEqual(context.exception.code, code)
            self.assertEqual(self.backend.commands, [], "A rejected request must send no mutation")

        def add_controller(self, hwnd=70):
            # Hand-checked MF keyboard/display direct-child fingerprint, 2026-11-17.
            top = window(hwnd, "#32770", parent=10)
            top["owner"] = 10
            self.backend.rows.append(top)
            for control_id in (7001, 7012, 7024, 7030, 7044, 7049, 7050, 7056):
                self.backend.rows.append(window(hwnd * 1000 + control_id, "Button", control_id, hwnd, hwnd))
            for control_id in (7101, 7105, 7110, 7113):
                self.backend.rows.append(window(hwnd * 1000 + control_id, "Static", control_id, hwnd, hwnd))

        def test_wrong_executable_is_never_controlled(self):
            self.backend.processes[0]["path"] = r"C:\other\sscnc.exe"
            self.assert_rejected("wrong_executable", self.driver.start)

        def test_wrong_binary_hash_is_never_controlled(self):
            self.backend.digest = "0" * 64
            self.assert_rejected("binary_hash_mismatch", self.driver.start)

        def test_multiple_matching_processes_are_ambiguous(self):
            self.backend.processes.append(dict(pid=102, path=TARGET_PATH, creation_time=124))
            self.assert_rejected("ambiguous_process", self.driver.start)

        def test_zero_flags_and_old_m30_do_not_prove_completion(self):
            self.backend.states = [snapshot(m30_count=8, end_count=8, current_mcode=30, index=39)]
            result = self.driver.status()
            self.assertEqual(result["state"], "stopped_unknown")
            self.assertFalse(result["completion"]["confirmed"])

        def test_unresponsive_is_reported_even_if_memory_is_readable(self):
            self.backend.responsive = False
            result = self.driver.status()
            self.assertEqual(result["state"], "unresponsive")
            self.assertEqual(result["telemetry"]["mode"], 2)
            self.assert_rejected("unresponsive", self.driver.start)

        def test_modal_blocks_mutation(self):
            self.backend.rows.append(window(40, "#32770"))
            self.assert_rejected("modal_dialog", self.driver.start)

        def test_enabled_child_with_disabled_top_parent_is_rejected(self):
            self.backend.rows[0]["enabled"] = False
            self.assert_rejected("main_disabled", self.driver.start)

        def test_disabled_panel_parent_blocks_button_notification(self):
            self.backend.rows[1]["enabled"] = False
            self.assert_rejected("control_disabled", self.driver.start)

        def test_emergency_blocks_mutations(self):
            for action in (self.driver.start, lambda: self.driver.set_view("top")):
                self.backend.states = [snapshot(emergency=1)]
                self.assert_rejected("emergency_active", action)

        def test_running_rejects_start_view_and_save(self):
            for action in (self.driver.start, lambda: self.driver.set_view("top"),
                           lambda: self.driver.save_workpiece(str(Path(__file__).parent / "unused.wp"))):
                self.backend.states = [snapshot(cycle=1, worker=1)]
                self.assert_rejected("busy", action)

        def test_start_rejects_non_mem_without_changing_mode(self):
            self.backend.states = [snapshot(mode=7)]
            self.assert_rejected("wrong_mode", self.driver.start)

        def test_stopped_pause_is_idempotent_without_message(self):
            result = self.driver.pause()
            self.assertEqual(result["outcome"], "already_stopped")
            self.assertEqual(self.backend.commands, [])

        def test_start_targets_discovered_parent_and_button(self):
            self.backend.on_command = lambda *args: setattr(self.backend, "states", [snapshot(worker=1, cycle=1, index=1)])
            result = self.driver.start()
            self.assertEqual(self.backend.commands, [(20, 9518, 30)])
            self.assertEqual(result["outcome"], "activity_observed")
            self.assertFalse(result["status"]["completion"]["confirmed"])

        def test_owned_top_level_standard_panel_is_not_a_modal_dialog(self):
            # Actual MF: standard panel is an owned top-level dialog, not WS_CHILD.
            for row in self.backend.rows:
                if row["hwnd"] in (20, 30, 31, 32): row["root"] = 20
            self.backend.rows[1]["owner"] = 10
            self.backend.on_command = lambda *args: setattr(self.backend, "states", [snapshot(worker=1, cycle=1, index=1)])
            result = self.driver.start()
            self.assertEqual(result["outcome"], "activity_observed")
            self.assertEqual(self.backend.commands, [(20, 9518, 30)])

        def test_identified_controller_keyboard_display_is_not_a_modal(self):
            self.add_controller()
            state = self.driver.status()
            self.assertEqual(state["modal_windows"], [])
            self.backend.on_command = lambda *args: setattr(self.backend, "states", [snapshot(worker=1, cycle=1, index=1)])
            self.assertEqual(self.driver.start()["outcome"], "activity_observed")
            self.assertEqual(self.backend.commands, [(20, 9518, 30)])

        def test_tool_dialog_stays_modal_alongside_controller(self):
            self.add_controller()
            tool = window(80, "#32770", parent=10)
            tool["owner"] = 10
            self.backend.rows += [tool, window(81, "SysListView32", 1067, 80, 80),
                                  window(82, "SysListView32", 1074, 80, 80)]
            self.assertEqual([row["hwnd"] for row in self.driver.status()["modal_windows"]], [80])
            self.assert_rejected("modal_dialog", self.driver.start)

        def test_incomplete_keyboard_signature_is_not_excluded(self):
            self.add_controller()
            self.backend.rows = [row for row in self.backend.rows if row["id"] != 7113]
            self.assertEqual([row["hwnd"] for row in self.driver.status()["modal_windows"]], [70])
            self.assert_rejected("modal_dialog", self.driver.start)

        def test_ambiguous_controllers_are_not_silently_excluded(self):
            self.add_controller(70)
            self.add_controller(80)
            self.assertEqual([row["hwnd"] for row in self.driver.status()["modal_windows"]], [70, 80])
            self.assert_rejected("modal_dialog", self.driver.start)

        def test_pause_records_stable_stop_then_resume_without_reset(self):
            self.backend.states = [snapshot(worker=1, cycle=1, index=4)]
            def transition(hwnd, command, control):
                if command == 9508:
                    self.backend.states = [snapshot(index=5, source_line=4, xyz=[1, 2, -3])]
                elif command == 9518:
                    self.backend.states = [snapshot(worker=1, cycle=1, index=6, xyz=[1, 2, -3])]
            self.backend.on_command = transition
            paused = self.driver.pause()
            self.assertEqual(paused["status"]["state"], "paused")
            self.assertEqual(paused["status"]["telemetry"]["index"], 5)
            resumed = self.driver.resume()
            self.assertEqual(resumed["outcome"], "activity_observed")
            self.assertEqual(self.backend.commands, [(20, 9508, 31), (20, 9518, 30)])

        def test_resume_requires_observed_pause(self):
            self.backend.states = [snapshot(index=4)]
            self.assert_rejected("pause_not_verified", self.driver.resume)

        def test_start_rechecks_position_before_sending(self):
            self.backend.states = [snapshot(index=0), snapshot(index=7, source_line=6)]
            self.assert_rejected("position_changed", self.driver.start)

        def test_m30_during_hold_does_not_become_paused(self):
            self.backend.states = [snapshot(worker=1, cycle=1, index=37)]
            self.backend.on_command = lambda *args: setattr(self.backend, "states", [snapshot(index=39, current_mcode=30, m30_count=1, end_count=1)])
            result = self.driver.pause()
            self.assertNotEqual(result["status"]["state"], "paused")
            self.assertFalse(result["status"]["completion"]["confirmed"])

        def test_only_observed_run_transition_can_be_possible_complete(self):
            self.backend.states = [snapshot(worker=1, cycle=1, index=1)]
            self.assertEqual(self.driver.status()["state"], "busy")
            self.backend.states = [snapshot(index=39, current_mcode=30, m30_count=1, end_count=1)]
            result = self.driver.status()
            self.assertEqual(result["state"], "possible_complete")
            self.assertFalse(result["completion"]["confirmed"])

        def test_arbitrary_view_commands_are_not_exposed(self):
            self.assert_rejected("invalid_view", lambda: self.driver.set_view("rotate"))

        def test_saved_file_must_be_new_absolute_wp_in_workspace(self):
            workspace = Path(__file__).resolve().parents[1]
            for filename in (str(workspace.parent / "outside.wp"), "relative.wp", str(workspace / "bad.cnc"),
                             str(workspace / ".." / "outside.wp"), str(workspace / "bad.wp:stream")):
                with self.subTest(filename=filename):
                    with self.assertRaises(DriverError): self.driver.save_workpiece(filename)
                    self.assertEqual(self.backend.commands, [])

        def test_existing_workpiece_is_never_overwritten(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "existing.wp"
                path.write_bytes(b"original")
                self.assert_rejected("output_exists", lambda: self.driver.save_workpiece(str(path)))
                self.assertEqual(path.read_bytes(), b"original")

        def test_save_selects_item_data_and_targets_enabled_dialog_not_disabled_main(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "fresh.wp"
                def transition(hwnd, command, control):
                    if command == 32827:
                        self.backend.rows[0]["enabled"] = False
                        dialog = window(40, "#32770", parent=10)
                        dialog["owner"] = 10
                        self.backend.rows += [dialog, window(41, "ListBox", 1067, 40, 40), window(42, "Button", 1, 40, 40)]
                    elif hwnd == 40 and command == 1:
                        self.backend.rows = [w for w in self.backend.rows if w["root"] != 40]
                        dialog = window(50, "#32770", parent=10)
                        dialog["owner"] = 10
                        self.backend.rows += [dialog, window(51, "Edit", 1148, 50, 50), window(52, "Button", 1, 50, 50)]
                    elif hwnd == 50 and command == 1:
                        path.write_bytes(b"new workpiece bytes")
                        self.backend.rows = [w for w in self.backend.rows if w["root"] != 50]
                        self.backend.rows[0]["enabled"] = True
                self.backend.on_command = transition
                result = self.driver.save_workpiece(str(path))
                self.assertEqual(self.backend.selected, 2)
                self.assertEqual(self.backend.file_text, str(path))
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42), (50, 1, 52)])
                self.assertEqual(result["outcome"], "file_created")
                self.assertGreater(result["bytes"], 0)

        def nested_save_flow(self, path, extra_dialog=False, change_selection=False, wrong_owner=False,
                             reuse_hidden_selection=False, reuse_hidden_filename=False,
                             selection_owner=10, filename_owner=10):
            def owned(hwnd):
                owner = selection_owner if hwnd == 40 else filename_owner
                row = window(hwnd, "#32770", parent=owner)
                row["owner"] = owner
                return row
            if reuse_hidden_selection:
                hidden = [owned(40), window(41, "ListBox", 1067, 40, 40), window(42, "Button", 1, 40, 40)]
                for row in hidden: row["visible"] = False
                self.backend.rows += hidden
            if reuse_hidden_filename:
                hidden = [owned(50), window(51, "Edit", 1148, 50, 50), window(52, "Button", 1, 50, 50)]
                for row in hidden: row["visible"] = False
                self.backend.rows += hidden
            def transition(hwnd, command, control):
                if command == 32827:
                    self.backend.rows[0]["enabled"] = False
                    if reuse_hidden_selection:
                        for row in self.backend.rows:
                            if row["root"] == 40: row["visible"] = True
                    else:
                        self.backend.rows += [owned(40), window(41, "ListBox", 1067, 40, 40), window(42, "Button", 1, 40, 40)]
                elif hwnd == 40 and command == 1:
                    common = owned(50)
                    if wrong_owner: common["owner"] = common["parent"] = 999
                    if reuse_hidden_filename:
                        for row in self.backend.rows:
                            if row["root"] == 50: row["visible"] = True
                    else:
                        self.backend.rows += [common, window(51, "Edit", 1148, 50, 50), window(52, "Button", 1, 50, 50)]
                    if extra_dialog: self.backend.rows.append(owned(60))
                    if change_selection: self.backend.selected = 0
                elif hwnd == 50 and command == 1:
                    path.write_bytes(b"nested save result")
                    self.backend.rows = [w for w in self.backend.rows if w["root"] not in (40, 50)]
                    self.backend.rows[0]["enabled"] = True
            self.backend.on_command = transition

        def test_scoped_selection_and_sibling_filename_dialog_can_coexist(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "nested.wp"
                self.nested_save_flow(path)
                result = self.driver.save_workpiece(str(path))
                self.assertEqual(result["outcome"], "file_created")
                self.assertEqual(path.read_bytes(), b"nested save result")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42), (50, 1, 52)])

        def test_nested_save_rejects_unrelated_third_dialog(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "blocked.wp"
                self.nested_save_flow(path, extra_dialog=True)
                with self.assertRaises(DriverError) as error: self.driver.save_workpiece(str(path))
                self.assertEqual(error.exception.code, "unexpected_dialog")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42)])
                self.assertFalse(path.exists())

        def test_nested_save_rechecks_workpiece_selection(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "blocked.wp"
                self.nested_save_flow(path, change_selection=True)
                with self.assertRaises(DriverError) as error: self.driver.save_workpiece(str(path))
                self.assertEqual(error.exception.code, "selection_changed")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42)])
                self.assertEqual(self.backend.file_text, "")

        def test_nested_save_rejects_filename_with_wrong_owner(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "blocked.wp"
                self.nested_save_flow(path, wrong_owner=True)
                with self.assertRaises(DriverError) as error: self.driver.save_workpiece(str(path))
                self.assertEqual(error.exception.code, "unexpected_dialog")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42)])

        def test_save_accepts_its_hidden_selection_becoming_visible(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "reused-selection.wp"
                self.nested_save_flow(path, reuse_hidden_selection=True)
                self.assertEqual(self.driver.save_workpiece(str(path))["outcome"], "file_created")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42), (50, 1, 52)])

        def test_save_accepts_its_hidden_filename_dialog_becoming_visible(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "reused-filename.wp"
                self.nested_save_flow(path, reuse_hidden_filename=True)
                self.assertEqual(self.driver.save_workpiece(str(path))["outcome"], "file_created")

        def test_save_never_adopts_already_visible_selection(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "existing-dialog.wp"
                self.nested_save_flow(path, reuse_hidden_selection=True)
                for row in self.backend.rows:
                    if row["root"] == 40: row["visible"] = True
                self.assert_rejected("modal_dialog", lambda: self.driver.save_workpiece(str(path)))

        def test_selection_may_be_owned_by_identified_controller(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "controller-owned.wp"
                self.add_controller(70)
                self.nested_save_flow(path, selection_owner=70, filename_owner=70)
                self.assertEqual(self.driver.save_workpiece(str(path))["outcome"], "file_created")

        def test_filename_may_return_to_main_from_controller_owned_selection(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "controller-main.wp"
                self.add_controller(70)
                self.nested_save_flow(path, selection_owner=70, filename_owner=10)
                self.assertEqual(self.driver.save_workpiece(str(path))["outcome"], "file_created")

        def test_unknown_window_cannot_own_this_save_flow(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "unknown-owner.wp"
                self.nested_save_flow(path, selection_owner=999)
                with self.assertRaises(DriverError) as error: self.driver.save_workpiece(str(path))
                self.assertEqual(error.exception.code, "unexpected_dialog")
                self.assertEqual(self.backend.commands, [(10, 32827, 0)])

        def save_close_race(self, path, persists=False):
            self.nested_save_flow(path)
            original_command = self.backend.on_command
            original_windows = self.backend.windows
            original_selection = self.backend.read_workpiece_selection
            stale, remaining = [], 0
            def command(hwnd, command_id, control):
                nonlocal stale, remaining
                if hwnd == 50 and command_id == 1:
                    stale = [dict(row) for row in self.backend.rows]
                    remaining = 2  # _refresh enumerates before and after module discovery.
                original_command(hwnd, command_id, control)
            def windows(process):
                nonlocal remaining
                if stale and (persists or remaining > 0):
                    remaining -= 1
                    return [dict(row) for row in stale]
                return original_windows(process)
            def selection(process, hwnd):
                if not any(row["hwnd"] == hwnd for row in self.backend.rows):
                    raise DriverError("window_changed", "Selection closed after window enumeration")
                return original_selection(process, hwnd)
            self.backend.on_command = command
            self.backend.windows = windows
            self.backend.read_workpiece_selection = selection

        def test_save_recovers_when_selection_closes_during_cleanup_read(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "closed-race.wp"
                self.save_close_race(path)
                result = self.driver.save_workpiece(str(path))
                self.assertEqual(result["outcome"], "file_created")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42), (50, 1, 52)])

        def test_save_closure_retry_is_bounded_and_never_reconfirms(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "stale-forever.wp"
                self.save_close_race(path, persists=True)
                with self.assertRaises(DriverError) as error: self.driver.save_workpiece(str(path))
                self.assertEqual(error.exception.code, "save_not_verified")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42), (50, 1, 52)])
                self.assertLess(self.backend.now, 1.2)

        def test_closed_save_completes_before_rechecking_old_dialog_owners(self):
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
                path = Path(folder) / "closed-owner.wp"
                self.add_controller(70)
                self.nested_save_flow(path, selection_owner=70, filename_owner=70)
                original = self.backend.on_command
                def command(hwnd, command_id, control):
                    original(hwnd, command_id, control)
                    if hwnd == 50 and command_id == 1:
                        self.backend.rows = [row for row in self.backend.rows if row["root"] != 70]
                self.backend.on_command = command
                result = self.driver.save_workpiece(str(path))
                self.assertEqual(result["outcome"], "file_created")
                self.assertEqual(self.backend.commands, [(10, 32827, 0), (40, 1, 42), (50, 1, 52)])

if __name__ == "__main__":
    unittest.main()
