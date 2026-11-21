"""Synthetic observer events only: never import Frida or attach to SSCNC."""
import importlib.util
import json
import io
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch


class ModuleAvailable(unittest.TestCase):
    def test_optional_completion_monitor_is_implemented(self):
        self.assertIsNotNone(importlib.util.find_spec("completion_monitor"),
                             "Completion observer module is missing")


if importlib.util.find_spec("completion_monitor"):
    import completion_monitor as cm
    from constants import TARGET_PATH
    from errors import DriverError

    PROCESS = dict(pid=101, path=TARGET_PATH, creation_time=134393913908195106)

    def event(name, seq, **changes):
        result = dict(event=name, seq=seq, nonce="current", pid=101,
                      creation_time="134393913908195106", tid=77, worker_id=1,
                      state=dict(document="0x10000", descriptor_array="0x20000", count=20,
                                 worker=1, cycle=1, emergency=0, coherent=True),
                      mcode=30, caller_rva="0x2ede0d", call_id=4, block_address="0x30000")
        result.update(changes)
        if name == "ready":
            result.update(path=TARGET_PATH, base="0x590000")
            result["state"].update(worker=0, cycle=0)
        if name == "worker_exit": result["state"].update(worker=0, cycle=0)
        return result

    CHAIN = ["ready", "worker_enter", "loop_enter", "m_apply", "m30_branch",
             "m_apply_return", "normal_drain", "loop_leave", "worker_exit"]

    class ChainTests(unittest.TestCase):
        def collector(self, limit=64):
            return cm.CompletionEvidence(PROCESS, "current", 0x590000, max_events=limit)
        def feed(self, collector, changes=None):
            for seq, name in enumerate(CHAIN, 1):
                collector.accept(event(name, seq, **(changes or {}).get(name, {})))
        def test_only_complete_run_origin_chain_confirms_worker_end(self):
            collector = self.collector()
            for seq, name in enumerate(CHAIN[:-1], 1): collector.accept(event(name, seq))
            self.assertFalse(collector.snapshot()["completed"])
            collector.accept(event("worker_exit", 9))
            result = collector.snapshot()
            self.assertTrue(result["completed"])
            self.assertFalse(result["geometry_verified"])
            self.assertEqual(result["worker"]["tid"], 77)
            self.assertEqual(result["worker"]["proof"]["normal_drain"], 7)
        def test_preview_origin_m30_cannot_complete_a_worker(self):
            collector = self.collector()
            self.feed(collector, {"m_apply": {"caller_rva": "0x123456"}})
            self.assertFalse(collector.snapshot()["completed"])
        def test_old_counters_and_zero_flags_without_events_do_not_complete(self):
            collector = self.collector()
            ready = event("ready", 1)
            ready["state"].update(m30_count=900, end_count=900, current_mcode=30)
            collector.accept(ready)
            self.assertFalse(collector.snapshot()["completed"])
        def test_incoherent_ready_snapshot_does_not_arm_the_observer(self):
            collector = self.collector()
            ready = event("ready", 1)
            ready["state"]["coherent"] = False
            collector.accept(ready)
            self.assertFalse(collector.snapshot()["ready"])
            self.assertEqual(collector.snapshot()["fault"]["code"], "observer_not_idle")
        def test_other_thread_cannot_supply_missing_m30_branch(self):
            collector = self.collector()
            self.feed(collector, {"m30_branch": {"tid": 88}})
            self.assertFalse(collector.snapshot()["completed"])
        def test_old_pid_or_old_attach_nonce_never_supplies_evidence(self):
            for changed in ({"pid": 99}, {"nonce": "previous"}, {"creation_time": "1"}):
                collector = self.collector()
                self.feed(collector, {"m30_branch": changed})
                self.assertFalse(collector.snapshot()["completed"])
                self.assertGreater(collector.snapshot()["rejected_events"], 0)
        def test_out_of_order_or_missing_sequence_is_not_reconstructed(self):
            collector = self.collector()
            collector.accept(event("ready", 1))
            collector.accept(event("worker_enter", 3))
            collector.accept(event("loop_enter", 2))
            self.assertFalse(collector.snapshot()["completed"])
            self.assertEqual(collector.snapshot()["fault"]["code"], "event_order")
        def test_early_exit_and_changed_program_each_block_completion(self):
            collector = self.collector()
            for seq, name in enumerate(CHAIN[:6], 1): collector.accept(event(name, seq))
            collector.accept(event("early_exit", 7))
            collector.accept(event("loop_leave", 8))
            collector.accept(event("worker_exit", 9))
            self.assertFalse(collector.snapshot()["completed"])
            collector = self.collector()
            changed = dict(event("normal_drain", 7)["state"], descriptor_array="0x99900")
            self.feed(collector, {"normal_drain": {"state": changed}})
            self.assertFalse(collector.snapshot()["completed"])
        def test_event_limit_is_explicit_and_cannot_leave_stale_completed_true(self):
            collector = self.collector(9)
            self.feed(collector)
            self.assertTrue(collector.snapshot()["completed"])
            collector.accept(event("worker_enter", 10, worker_id=2))
            result = collector.snapshot()
            self.assertFalse(result["completed"])
            self.assertEqual(result["fault"]["code"], "event_limit")
            self.assertEqual(len(result["events"]), 9)

    class FakeBackend:
        def _verify(self, process): pass

    class FakeDriver:
        def __init__(self):
            self._lock = threading.RLock()
            self.backend = FakeBackend()
            self.context = dict(process=dict(PROCESS), base=0x590000, main=dict(hwnd=10))
            self.state = dict(telemetry=dict(worker=0, cycle=0), modal_windows=[])
            self.callback_phase = False
        def _discover(self):
            if self.callback_phase: raise AssertionError("Event handler called the blocking driver")
            return self.context
        def _capture(self): return self._discover(), self.state
        def _refresh(self, context): return self._capture()
        def _guard(self, context, state):
            if state["telemetry"]["worker"]: raise DriverError("busy", "Running")
            if state["modal_windows"]: raise DriverError("modal_dialog", "Modal")

    class FakeScript:
        def __init__(self, source):
            self.cfg = json.loads(source.split("const CFG = ", 1)[1].split(";\n", 1)[0])
            self.callback = None
            self.unloaded = False
            self.callbacks = {}
        def on(self, name, callback):
            self.callbacks[name] = callback
            if name == "message": self.callback = callback
        def set_log_handler(self, handler): self.log_handler = handler
        @property
        def is_destroyed(self): return self.unloaded
        def emit(self, payload):
            payload = dict(payload, nonce=self.cfg["nonce"])
            self.callback(dict(type="send", payload=payload), None)
        def load(self): self.emit(event("ready", 1))
        def unload(self):
            self.unloaded = True
            if "destroyed" in self.callbacks: self.callbacks["destroyed"]()

    class FakeSession:
        def __init__(self): self.script = None; self.detached = False
        @property
        def is_detached(self): return self.detached
        def on(self, name, callback): self.callback = callback
        def create_script(self, source):
            self.script = FakeScript(source)
            return self.script
        def detach(self):
            self.detached = True
            self.callback("application-requested", None)

    class FakeFrida:
        def __init__(self): self.session = FakeSession(); self.attachments = []
        def attach(self, pid):
            if self.attachments: self.session = FakeSession()
            self.attachments.append(pid)
            return self.session

    class LifecycleTests(unittest.TestCase):
        def test_constructor_does_not_import_sdk_and_missing_sdk_is_structured(self):
            driver = FakeDriver()
            with patch.object(cm.importlib, "import_module", side_effect=ModuleNotFoundError("frida")):
                monitor = cm.CompletionMonitor(driver)
                self.assertEqual(monitor.status()["state"], "unarmed")
                with self.assertRaises(DriverError) as error: monitor.arm()
            self.assertEqual(error.exception.code, "dependency_missing")
            self.assertFalse(monitor.close()["attached"])
        def test_active_simulation_or_modal_is_rejected_before_attach(self):
            for change in ("busy", "modal"):
                driver, provider = FakeDriver(), FakeFrida()
                if change == "busy": driver.state["telemetry"]["worker"] = 1
                else: driver.state["modal_windows"] = [1]
                with patch.object(cm.importlib, "import_module", return_value=provider):
                    with self.assertRaises(DriverError): cm.CompletionMonitor(driver).arm()
                self.assertEqual(provider.attachments, [])
        def test_arm_callbacks_and_close_use_no_simulator_command_or_driver_callback(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider):
                self.assertTrue(monitor.arm()["attached"])
            driver.callback_phase = True
            for seq, name in enumerate(CHAIN[1:], 2): provider.session.script.emit(event(name, seq))
            driver.callback_phase = False
            self.assertTrue(monitor.status()["completed"])
            self.assertFalse(monitor.close()["attached"])
            self.assertTrue(provider.session.script.unloaded)
            self.assertTrue(provider.session.detached)
            self.assertEqual(monitor.close()["outcome"], "already_closed")
        def test_pid_reuse_invalidates_current_completion_even_after_valid_chain(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider): monitor.arm()
            for seq, name in enumerate(CHAIN[1:], 2): provider.session.script.emit(event(name, seq))
            driver.context = dict(driver.context, process=dict(PROCESS, creation_time=1))
            result = monitor.status()
            self.assertFalse(result["completed"])
            self.assertEqual(result["fault"]["code"], "process_changed")
            monitor.close()
        def test_unexpected_detach_is_not_reported_as_attached_or_completed(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider): monitor.arm()
            for seq, name in enumerate(CHAIN[1:], 2): provider.session.script.emit(event(name, seq))
            provider.session.callback("process-terminated", None)
            result = monitor.status()
            self.assertFalse(result["attached"])
            self.assertFalse(result["completed"])
            self.assertEqual(result["fault"]["code"], "observer_detached")
            monitor.close()
        def test_ready_callback_on_another_thread_does_not_wait_for_driver_lock(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            def asynchronous_load(script):
                thread = threading.Thread(target=lambda: script.emit(event("ready", 1)), daemon=True)
                thread.start()
                thread.join(0.5)
                self.assertFalse(thread.is_alive(), "Ready callback blocked on Driver's lock")
            with patch.object(cm.importlib, "import_module", return_value=provider), \
                    patch.object(FakeScript, "load", asynchronous_load):
                self.assertTrue(monitor.arm()["attached"])
            monitor.close()
        def test_failed_detach_retains_cleanup_handle_and_cannot_claim_active_completion(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider): monitor.arm()
            for seq, name in enumerate(CHAIN[1:], 2): provider.session.script.emit(event(name, seq))
            original_detach = provider.session.detach
            with patch.object(provider.session, "detach", side_effect=RuntimeError("detach failed")):
                result = monitor.close()
                self.assertEqual(result["state"], "cleanup_pending")
                self.assertFalse(result["completed"])
                self.assertFalse(result["observer_active"])
                with self.assertRaises(DriverError) as error: monitor.arm()
                self.assertEqual(error.exception.code, "observer_cleanup_required")
            self.assertEqual(monitor.close()["outcome"], "closed")
            self.assertTrue(provider.session.detached)
        def test_script_load_failure_reports_failed_cleanup_and_remains_retryable(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider), \
                    patch.object(FakeScript, "load", side_effect=RuntimeError("load failed")), \
                    patch.object(provider.session, "detach", side_effect=RuntimeError("detach failed")):
                with self.assertRaises(DriverError) as error: monitor.arm()
            self.assertTrue(error.exception.details["cleanup_errors"])
            self.assertEqual(monitor.status()["state"], "cleanup_pending")
            self.assertEqual(monitor.close()["outcome"], "closed")
        def test_rearm_after_detach_uses_new_nonce_and_ignores_old_callbacks(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider):
                monitor.arm()
                old_script = provider.session.script
                provider.session.detached = True
                provider.session.callback("process-terminated", None)
                result = monitor.arm()
            self.assertEqual(result["outcome"], "armed")
            self.assertEqual(provider.attachments, [101, 101])
            old_script.emit(event("worker_enter", 2))
            self.assertEqual(monitor.status()["event_count"], 1)
            self.assertIsNone(monitor.status()["fault"])
            monitor.close()
        def test_destroyed_script_invalidates_observer_without_pretending_transport_detached(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider): monitor.arm()
            provider.session.script.unload()
            result = monitor.status()
            self.assertTrue(result["attached"])
            self.assertFalse(result["observer_active"])
            self.assertEqual(result["fault"]["code"], "observer_script_destroyed")
            monitor.close()
        def test_sdk_diagnostic_logs_never_pollute_mcp_stdout(self):
            driver, provider = FakeDriver(), FakeFrida()
            monitor = cm.CompletionMonitor(driver)
            with patch.object(cm.importlib, "import_module", return_value=provider): monitor.arm()
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                provider.session.script.log_handler("info", "diagnostic")
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("diagnostic", stderr.getvalue())
            monitor.close()


if __name__ == "__main__":
    unittest.main()
