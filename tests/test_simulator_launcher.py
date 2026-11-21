"""Offline launcher contract tests. No native process or UI is started."""
import importlib.util
from pathlib import Path
import threading
import unittest
from contextlib import contextmanager

from constants import INSTALL_DIRECTORY, MAIN_CLASS, TARGET_PATH
from errors import DriverError

if importlib.util.find_spec("simulator_launcher"):
    from simulator_launcher import (LAUNCHER_PATH, LAUNCHER_SHA256, INSTALL_DIRECTORY,
                                    WindowsLauncherSystem, launch_simulator)
else:
    LAUNCHER_PATH = str(Path(INSTALL_DIRECTORY) / "CNCLogin.exe")
    LAUNCHER_SHA256 = "2dbc2285b40934141a58ead14516afcb391461ca2e108206e54ad13803161d3d"
    WindowsLauncherSystem = launch_simulator = None


def process(path, pid=101, created=123):
    return {"pid": pid, "path": path, "creation_time": created}


class FakeSystem:
    def __init__(self, processes=()):
        self.processes = list(processes)
        self.start_calls = []
        self.file_calls = []
        self.inventory_calls = 0
        self.on_inventory = None
        self.file = {"exists": True, "path": LAUNCHER_PATH, "sha256": LAUNCHER_SHA256}
        self.lock = threading.RLock()
        self.foreground_calls = []
        self.foreground_result = {"foreground_verified": True, "foreground_hwnd": 10,
                                  "restore_requested": False}

    def exclusive(self):
        return self.lock

    def list_processes(self):
        self.inventory_calls += 1
        if self.on_inventory:
            self.on_inventory(self.inventory_calls)
        return [dict(p) for p in self.processes]

    def file_identity(self, filename):
        self.file_calls.append(filename)
        return dict(self.file)

    def start_launcher(self, filename, cwd):
        self.start_calls.append((filename, cwd))
        created = process(filename, 303, 999)
        self.processes.append(created)
        return created

    def bring_to_front(self, instance, main_hwnd, maximize=False):
        self.foreground_calls.append((dict(instance), main_hwnd, maximize))
        return dict(self.foreground_result, maximize_requested=maximize,
                    maximized_verified=True if maximize else None)


class FakeDriver:
    def __init__(self, system):
        self._lock = threading.RLock()
        self.launcher_system = system
        self.status_calls = 0
        self.observed_status = {"ok": True, "state": "stopped_unknown",
                                "target": dict(process(TARGET_PATH), main_hwnd=10)}

    def status(self):
        self.status_calls += 1
        return dict(self.observed_status)


class SimulatorLauncherTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(launch_simulator), "Launcher workflow is not implemented")

    def rejected(self, code, system):
        with self.assertRaises(DriverError) as caught:
            launch_simulator(FakeDriver(system))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(system.start_calls, [])

    def test_foreground_option_false_does_not_operate_existing_window(self):
        system = FakeSystem([process(TARGET_PATH)])
        result = launch_simulator(FakeDriver(system), bring_to_front=False)
        self.assertFalse(result["foreground_requested"])
        self.assertIsNone(result["foreground_verified"])
        self.assertEqual(system.foreground_calls, [])

    def test_foreground_failure_is_reported_without_claiming_visibility(self):
        system = FakeSystem([process(TARGET_PATH)])
        system.foreground_result["foreground_verified"] = False
        system.foreground_result["foreground_hwnd"] = 999
        result = launch_simulator(FakeDriver(system))
        self.assertTrue(result["ok"])
        self.assertFalse(result["foreground_verified"])
        self.assertEqual(system.start_calls, [])

    def test_foreground_flag_requires_a_boolean(self):
        system = FakeSystem()
        with self.assertRaises(DriverError) as caught:
            launch_simulator(FakeDriver(system), bring_to_front="yes")
        self.assertEqual(caught.exception.code, "invalid_foreground_option")
        self.assertEqual(system.start_calls, [])

    def test_existing_pinned_simulator_returns_real_status_without_relaunch(self):
        system = FakeSystem([process(LAUNCHER_PATH, 99), process(TARGET_PATH)])
        driver = FakeDriver(system)
        result = launch_simulator(driver)
        self.assertEqual(result["outcome"], "already_running")
        self.assertEqual(result["status"], driver.observed_status)
        self.assertEqual(driver.status_calls, 1)
        self.assertFalse(result["launcher_started"])
        self.assertEqual(system.start_calls, [])
        self.assertTrue(result["foreground_verified"])
        self.assertEqual(system.foreground_calls, [(process(TARGET_PATH), 10, False)])

    def test_maximize_option_is_forwarded_only_to_verified_simulator(self):
        system = FakeSystem([process(TARGET_PATH)])
        result = launch_simulator(FakeDriver(system), maximize=True)
        self.assertEqual(system.foreground_calls, [(process(TARGET_PATH), 10, True)])
        self.assertTrue(result["maximize_requested"])
        self.assertTrue(result["maximized_verified"])
        self.assertEqual(system.start_calls, [])

    def test_maximize_requires_a_boolean_and_foreground_request(self):
        for options in ({"maximize": "yes"}, {"maximize": 1},
                        {"maximize": True, "bring_to_front": False}):
            system = FakeSystem([process(TARGET_PATH)])
            with self.subTest(options=options), self.assertRaises(DriverError) as caught:
                launch_simulator(FakeDriver(system), **options)
            self.assertEqual(caught.exception.code, "invalid_maximize_option")
            self.assertEqual(system.foreground_calls, [])
            self.assertEqual(system.start_calls, [])

    def test_maximize_does_not_operate_existing_or_new_login_window(self):
        for processes in ([], [process(LAUNCHER_PATH, 99)]):
            system = FakeSystem(processes)
            with self.subTest(processes=processes):
                result = launch_simulator(FakeDriver(system), maximize=True)
                self.assertEqual(result["outcome"], "login_required")
                self.assertFalse(result["maximize_requested"])
                self.assertIsNone(result["maximized_verified"])
                self.assertEqual(system.foreground_calls, [])

    def test_failed_maximize_is_propagated_independently_of_foreground(self):
        system = FakeSystem([process(TARGET_PATH)])

        def rejected(instance, main_hwnd, maximize=False):
            return {"foreground_verified": True, "maximized_verified": False}

        system.bring_to_front = rejected
        result = launch_simulator(FakeDriver(system), maximize=True)
        self.assertTrue(result["foreground_verified"])
        self.assertFalse(result["maximized_verified"])

    def test_existing_launcher_requires_normal_login_without_buttons(self):
        system = FakeSystem([process(LAUNCHER_PATH, 99)])
        driver = FakeDriver(system)
        result = launch_simulator(driver)
        self.assertEqual(result["outcome"], "login_required")
        self.assertEqual(result["launcher"]["pid"], 99)
        self.assertFalse(result["launcher_started"])
        self.assertFalse(result["login_page_verified"])
        self.assertEqual(system.start_calls, [])
        self.assertEqual(driver.status_calls, 0)
        self.assertEqual(system.file_calls, [LAUNCHER_PATH])

    def test_empty_inventory_starts_only_fixed_login_executable_with_fixed_cwd(self):
        system = FakeSystem()
        result = launch_simulator(FakeDriver(system))
        self.assertEqual(system.start_calls, [(LAUNCHER_PATH, INSTALL_DIRECTORY)])
        self.assertEqual(result["outcome"], "login_required")
        self.assertTrue(result["launcher_started"])
        self.assertEqual(result["launcher"]["pid"], 303)
        self.assertFalse(result["login_page_verified"])
        self.assertFalse(result["machining_started"])

    def test_missing_launcher_is_not_replaced_by_child_executable(self):
        system = FakeSystem()
        system.file["exists"] = False
        self.rejected("launcher_missing", system)

    def test_hash_mismatch_is_rejected(self):
        system = FakeSystem()
        system.file["sha256"] = "0" * 64
        self.rejected("launcher_hash_mismatch", system)

    def test_redirected_launcher_path_is_rejected_even_with_matching_hash(self):
        system = FakeSystem()
        system.file["path"] = r"C:\other\CNCLogin.exe"
        self.rejected("launcher_path_mismatch", system)

    def test_wrong_same_named_simulator_or_launcher_blocks_new_instance(self):
        for filename in (r"C:\other\sscnc.exe", r"C:\other\CNCLogin.exe"):
            with self.subTest(filename=filename):
                self.rejected("conflicting_instance", FakeSystem([process(filename)]))

    def test_duplicate_instances_are_not_silently_selected(self):
        for filename in (TARGET_PATH, LAUNCHER_PATH):
            with self.subTest(filename=filename):
                self.rejected("ambiguous_process", FakeSystem([process(filename, 101), process(filename, 102)]))

    def test_launcher_appearing_during_file_check_is_reused(self):
        system = FakeSystem()
        def appear(call):
            if call == 2:
                system.processes = [process(LAUNCHER_PATH, 202)]
        system.on_inventory = appear
        result = launch_simulator(FakeDriver(system))
        self.assertEqual(result["outcome"], "login_required")
        self.assertFalse(result["launcher_started"])
        self.assertEqual(system.start_calls, [])

    def test_simulator_appearing_during_file_check_is_not_restarted(self):
        system = FakeSystem()
        def appear(call):
            if call == 2:
                system.processes = [process(TARGET_PATH)]
        system.on_inventory = appear
        result = launch_simulator(FakeDriver(system))
        self.assertEqual(result["outcome"], "already_running")
        self.assertEqual(system.start_calls, [])

    def test_unverified_existing_target_never_causes_fallback_launch(self):
        system = FakeSystem([process(TARGET_PATH)])
        driver = FakeDriver(system)
        driver.observed_status = {"ok": False, "state": "binary_hash_mismatch"}
        result = launch_simulator(driver)
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "existing_instance_unverified")
        self.assertEqual(system.start_calls, [])

    def test_target_identity_change_during_status_is_rejected(self):
        system = FakeSystem([process(TARGET_PATH)])
        driver = FakeDriver(system)
        driver.observed_status["target"] = process(TARGET_PATH, 102)
        with self.assertRaises(DriverError) as caught:
            launch_simulator(driver)
        self.assertEqual(caught.exception.code, "process_changed")
        self.assertEqual(system.start_calls, [])

    def test_process_inventory_failure_never_launches(self):
        system = FakeSystem()
        def fail(_):
            raise DriverError("process_inventory", "not readable")
        system.on_inventory = fail
        self.rejected("process_inventory", system)

    def test_two_callers_share_serialization_and_create_only_one_launcher(self):
        system = FakeSystem()
        drivers = [FakeDriver(system), FakeDriver(system)]
        results, failures = [], []
        def call(driver):
            try:
                results.append(launch_simulator(driver))
            except Exception as exc:
                failures.append(exc)
        threads = [threading.Thread(target=call, args=(driver,)) for driver in drivers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(system.start_calls), 1)
        self.assertEqual(sum(result["launcher_started"] for result in results), 1)

    def test_native_launch_boundary_uses_argument_list_and_no_shell(self):
        calls = []
        class Child:
            pid = 303
            def poll(self):
                return None
        class Backend:
            @contextmanager
            def _handle(self, pid):
                self.pid = pid
                yield object()
            def _process_identity(self, handle, pid):
                return process(LAUNCHER_PATH, pid, 999)
        def popen(*args, **kwargs):
            calls.append((args, kwargs))
            return Child()
        native = WindowsLauncherSystem(Backend(), popen=popen)
        observed = native.start_launcher(LAUNCHER_PATH, INSTALL_DIRECTORY)
        self.assertEqual(observed["pid"], 303)
        self.assertEqual(calls, [(([LAUNCHER_PATH],),
                                 {"cwd": INSTALL_DIRECTORY, "shell": False, "close_fds": True})])


class APIFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class ForegroundBackend:
    def __init__(self, iconic=False, allowed=True, foreground=999, zoomed=False):
        self.now = 0
        self.iconic = iconic
        self.zoomed = zoomed
        self.allowed = allowed
        self.foreground = foreground
        self.calls = []
        self.rows = {
            10: {"hwnd": 10, "pid": 101, "cls": MAIN_CLASS, "root": 10, "owner": 0},
            20: {"hwnd": 20, "pid": 101, "cls": "#32770", "root": 20, "owner": 10},
            999: {"hwnd": 999, "pid": 500, "cls": "Browser", "root": 999, "owner": 0},
        }
        self.u = type("API", (), {})()
        self.u.IsWindow = APIFunction(lambda hwnd: hwnd in self.rows)
        self.u.IsIconic = APIFunction(lambda hwnd: self.iconic)
        self.u.IsZoomed = APIFunction(lambda hwnd: self.zoomed)
        self.u.IsWindowVisible = APIFunction(lambda hwnd: True)
        self.u.ShowWindowAsync = APIFunction(self.restore)
        self.u.SetForegroundWindow = APIFunction(self.foreground_request)
        self.u.GetForegroundWindow = APIFunction(lambda: self.foreground)
        self.u.GetWindow = APIFunction(lambda hwnd, flag: self.rows[hwnd]["owner"])
        self.u.GetAncestor = APIFunction(lambda hwnd, flag: self.rows[hwnd]["root"])

    def restore(self, hwnd, command):
        self.calls.append(("ShowWindowAsync", hwnd, command))
        self.iconic = False
        self.zoomed = command == 3
        return True

    def foreground_request(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        if self.allowed:
            self.foreground = hwnd
        return self.allowed

    def _verify(self, instance):
        if instance != process(TARGET_PATH):
            raise DriverError("process_changed", "identity changed")

    def _window(self, hwnd):
        return dict(self.rows[hwnd])

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class NativeForegroundTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(WindowsLauncherSystem), "Windows launcher boundary is missing")

    def test_only_minimized_main_receives_restore_then_foreground(self):
        backend = ForegroundBackend(iconic=True)
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertEqual(backend.calls, [("ShowWindowAsync", 10, 9), ("SetForegroundWindow", 10)])
        self.assertTrue(result["foreground_verified"])

    def test_normal_window_is_not_resized_or_restored(self):
        backend = ForegroundBackend()
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertEqual(backend.calls, [("SetForegroundWindow", 10)])
        self.assertTrue(result["foreground_verified"])

    def test_explicit_maximize_uses_normal_window_command_and_verifies_zoomed(self):
        for iconic in (False, True):
            backend = ForegroundBackend(iconic=iconic)
            with self.subTest(iconic=iconic):
                result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10, maximize=True)
                self.assertEqual(backend.calls, [("ShowWindowAsync", 10, 3), ("SetForegroundWindow", 10)])
                self.assertTrue(result["foreground_verified"])
                self.assertTrue(result["maximized_verified"])
                self.assertTrue(result["maximize_request_accepted"])
                self.assertFalse(result["restore_requested"])

    def test_already_maximized_window_is_verified_without_redundant_show(self):
        backend = ForegroundBackend(zoomed=True)
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10, maximize=True)
        self.assertEqual(backend.calls, [("SetForegroundWindow", 10)])
        self.assertTrue(result["maximized_verified"])
        self.assertIsNone(result["maximize_request_accepted"])

    def test_default_does_not_maximize_and_does_not_claim_maximize_verification(self):
        backend = ForegroundBackend(zoomed=True)
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertEqual(backend.calls, [("SetForegroundWindow", 10)])
        self.assertFalse(result["maximize_requested"])
        self.assertIsNone(result["maximized_verified"])

    def test_accepted_show_request_without_zoomed_readback_is_not_success(self):
        backend = ForegroundBackend()

        def ignored(hwnd, command):
            backend.calls.append(("ShowWindowAsync", hwnd, command))
            return True

        backend.u.ShowWindowAsync = APIFunction(ignored)
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10, maximize=True)
        self.assertTrue(result["foreground_verified"])
        self.assertFalse(result["maximized_verified"])
        self.assertTrue(result["maximize_request_accepted"])
        self.assertGreaterEqual(backend.now, 1)
        self.assertLessEqual(backend.now, 1.1)

    def test_identity_change_after_maximize_prevents_foreground_request(self):
        backend = ForegroundBackend()

        def replaced(hwnd, command):
            result = backend.restore(hwnd, command)
            backend.rows[10]["pid"] = 501
            return result

        backend.u.ShowWindowAsync = APIFunction(replaced)
        with self.assertRaises(DriverError) as caught:
            WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10, maximize=True)
        self.assertEqual(caught.exception.code, "window_changed")
        self.assertEqual(backend.calls, [("ShowWindowAsync", 10, 3)])

    def test_owned_dialog_counts_as_verified_foreground(self):
        backend = ForegroundBackend(allowed=False, foreground=20)
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertTrue(result["foreground_verified"])
        self.assertEqual(result["foreground_hwnd"], 20)

    def test_unowned_same_process_window_does_not_prove_main_is_foreground(self):
        backend = ForegroundBackend(allowed=False, foreground=20)
        backend.rows[20]["owner"] = 0
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertFalse(result["foreground_verified"])

    def test_identity_change_after_restore_blocks_foreground_request(self):
        backend = ForegroundBackend(iconic=True)
        def replaced(hwnd, command):
            result = backend.restore(hwnd, command)
            backend.rows[10]["pid"] = 501
            return result
        backend.u.ShowWindowAsync = APIFunction(replaced)
        with self.assertRaises(DriverError) as caught:
            WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertEqual(caught.exception.code, "window_changed")
        self.assertEqual(backend.calls, [("ShowWindowAsync", 10, 9)])

    def test_foreground_policy_denial_returns_false_after_bounded_readback(self):
        backend = ForegroundBackend(allowed=False)
        result = WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertFalse(result["foreground_verified"])
        self.assertEqual(backend.calls, [("SetForegroundWindow", 10)])
        self.assertLessEqual(backend.now, 1.1)

    def test_wrong_pid_at_main_handle_prevents_any_window_mutation(self):
        backend = ForegroundBackend(iconic=True)
        backend.rows[10]["pid"] = 501
        with self.assertRaises(DriverError) as caught:
            WindowsLauncherSystem(backend).bring_to_front(process(TARGET_PATH), 10)
        self.assertEqual(caught.exception.code, "window_changed")
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
