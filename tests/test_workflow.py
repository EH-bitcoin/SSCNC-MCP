import tempfile
import unittest
from pathlib import Path

from test_driver import FakeBackend, snapshot, window
from errors import DriverError


class WorkflowTests(unittest.TestCase):
    def make_driver(self):
        from workflow import WorkflowDriver
        backend = FakeBackend()
        backend.read_with = lambda *args: dict(home_busy=0, coherent=True, nc_dirty=False, file_open_will_prompt=False)
        backend.rows.append(window(33, 'Button', 9510, parent=20, root=10))
        return WorkflowDriver(backend, wait_timeout=0.5), backend

    def test_mode_switch_observes_effect_without_start(self):
        driver, backend = self.make_driver()
        backend.on_command = lambda *args: setattr(backend, 'states', [snapshot(mode=1)])
        result = driver.set_mode('edit')
        self.assertEqual(result['outcome'], 'mode_verified')
        self.assertEqual(backend.commands, [(20, 9510, 33)])

    def test_emergency_release_is_idempotent_and_never_starts(self):
        driver, backend = self.make_driver()
        backend.rows.append(window(34, 'Static', 9651, parent=20, root=10))
        result = driver.set_emergency(False)
        self.assertEqual(result['outcome'], 'already_set')
        self.assertEqual(backend.commands, [])
        backend.states = [snapshot(emergency=1)]
        backend.on_command = lambda *args: setattr(backend, 'states', [snapshot(emergency=0)])
        result = driver.set_emergency(False)
        self.assertEqual(result['outcome'], 'emergency_state_verified')
        self.assertEqual(backend.commands, [(20, 9651, 34)])

    def test_mode_switch_refuses_running_and_unknown_mode(self):
        driver, backend = self.make_driver()
        backend.states = [snapshot(worker=1, cycle=1)]
        with self.assertRaises(DriverError) as caught:
            driver.set_mode('edit')
        self.assertEqual(caught.exception.code, 'busy')
        self.assertEqual(backend.commands, [])
        with self.assertRaises(DriverError):
            driver.set_mode('automatic')

    def test_mode_switch_does_not_claim_unobserved_change(self):
        driver, backend = self.make_driver()
        with self.assertRaises(DriverError) as caught:
            driver.set_mode('edit')
        self.assertEqual(caught.exception.code, 'mode_not_verified')

    def test_loading_missing_input_has_no_ui_effect(self):
        driver, backend = self.make_driver()
        with self.assertRaises(DriverError):
            driver.load_program(str(Path(tempfile.gettempdir()) / 'missing-sscnc-input.cnc'))
        self.assertEqual(backend.commands, [])

    def test_loading_changed_document_cancels_by_default(self):
        driver, backend = self.make_driver()
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'sample.cnc'
            filename.write_text('O1234\nM30\n', encoding='ascii')
            def posted(hwnd, command, control):
                if command == 57601:
                    dialog = window(50, '#32770', parent=10)
                    dialog['owner'] = 10
                    backend.rows.extend([dialog,
                                         window(51, 'Static', 65535, 50, 50),
                                         window(52, 'Button', 2, 50, 50)])
            backend.on_command = posted
            backend.read_text = lambda process, hwnd: '文件已被修改，是否保存当前文件' if hwnd == 51 else ''
            with self.assertRaises(DriverError) as caught:
                driver.load_program(str(filename))
            self.assertEqual(caught.exception.code, 'unsaved_document')
            self.assertEqual(backend.commands[-1], (50, 2, 52))
            self.assertFalse(any(command == 7 for _, command, _ in backend.commands))

    def test_mode_refuses_active_homing(self):
        driver, backend = self.make_driver()
        backend.read_with = lambda *args: dict(home_busy=1, coherent=True)
        with self.assertRaises(DriverError) as caught:
            driver.set_mode('edit')
        self.assertEqual(caught.exception.code, 'home_busy')
        self.assertEqual(backend.commands, [])

    def test_uncancellable_unsaved_prompt_never_chooses_yes_or_no(self):
        driver, backend = self.make_driver()
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'sample.cnc'
            filename.write_text('O1234\nM30\n', encoding='ascii')
            def posted(hwnd, command, control):
                if command == 57601:
                    dialog = window(50, '#32770', parent=10)
                    dialog['owner'] = 10
                    backend.rows.extend([dialog, window(51, 'Static', 65535, 50, 50),
                                         window(52, 'Button', 6, 50, 50), window(53, 'Button', 7, 50, 50)])
            backend.on_command = posted
            backend.read_text = lambda process, hwnd: '文件已被修改，是否保存当前文件' if hwnd == 51 else ''
            with self.assertRaises(DriverError) as caught:
                driver.load_program(str(filename))
            self.assertEqual(caught.exception.code, 'unsaved_document')
            self.assertTrue(caught.exception.details['native_prompt_left_open'])
            self.assertEqual(backend.commands, [(10, 57601, 0)])

    def test_dirty_workpiece_preflight_does_not_open_uncancellable_prompt(self):
        driver, backend = self.make_driver()
        backend.read_with = lambda *args: dict(home_busy=0, coherent=True, nc_dirty=False,
                                              file_open_will_prompt=True, workpiece_dirty=True)
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'sample.cnc'
            filename.write_text('O1234\nM30\n', encoding='ascii')
            with self.assertRaises(DriverError) as caught:
                driver.load_program(str(filename))
            self.assertEqual(caught.exception.code, 'unsaved_document')
            self.assertFalse(caught.exception.details['native_prompt_left_open'])
            self.assertEqual(backend.commands, [])

    def test_refused_reset_preserves_pause_bookmark(self):
        driver, backend = self.make_driver()
        from driver import _position
        context, state = driver._capture()
        bookmark = _position(context, state['telemetry'])
        driver._paused = bookmark
        with self.assertRaises(DriverError):
            driver.reset_program()
        self.assertIs(driver._paused, bookmark)

    def test_loader_refuses_unowned_file_dialog(self):
        driver, backend = self.make_driver()
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / 'sample.cnc'
            filename.write_text('O1234\nM30\n', encoding='ascii')
            def posted(hwnd, command, control):
                if command == 57601:
                    backend.rows.extend([window(50, '#32770'), window(51, 'Edit', 1148, 50, 50),
                                         window(52, 'Button', 1, 50, 50)])
            backend.on_command = posted
            backend.read_text = lambda process, hwnd: {50: '打开', 52: '打开(&O)'}.get(hwnd, '')
            with self.assertRaises(DriverError) as caught:
                driver.load_program(str(filename))
            self.assertEqual(caught.exception.code, 'unexpected_dialog')
            self.assertEqual(backend.commands, [(10, 57601, 0)])

    def test_controller_keys_remain_bound_to_original_process(self):
        from test_initialization import InitializationBackend
        from workflow import WorkflowDriver
        backend = InitializationBackend()
        backend.read_text = lambda process, hwnd: 'POS' if hwnd == 27030 else ''
        backend.on_command = lambda *args: setattr(backend, 'processes', [dict(backend.processes[0], creation_time=124)])
        driver = WorkflowDriver(backend, wait_timeout=.5)
        with self.assertRaises(DriverError):
            driver.controller_keys(['POS', 'POS'])
        self.assertEqual(len(backend.commands), 1)


if __name__ == '__main__':
    unittest.main()
