"""Offline OS-boundary tests; no simulator or actual homing is used."""
import importlib
import importlib.util
import struct
import unittest

from errors import DriverError
from test_driver import FakeBackend, snapshot, window
from workflow import WorkflowDriver


class InitializationBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.states = [snapshot(xyz=[-450., -250., -145.])]
        self.axis = 0
        self.home_busy = 0
        self.home_due = None
        self.home_axes_sent = []
        self.complete_home = True
        self.accept_home = True
        self.select_axis = True
        self.reset_text = 'RESET'
        self.reset_effect = False
        for control_id in (9540, 9582, 9592, 9602, 9586):
            self.rows.append(window(10000 + control_id, 'Button', control_id, 20, 10))
        self.rows.append(window(19651, 'Static', 9651, 20, 10))
        panel = window(70, '#32770', parent=10)
        panel['owner'] = 10
        self.rows.append(panel)
        for control_id in (7001, 7012, 7024, 7030, 7044, 7049, 7050, 7056, 7045):
            self.rows.append(window(20000 + control_id, 'Button', control_id, 70, 70))
        for control_id in (7101, 7105, 7110, 7113):
            self.rows.append(window(20000 + control_id, 'Static', control_id, 70, 70))
        self.on_command = self.command_effect

    def command_effect(self, hwnd, command, control):
        if command == 9651:
            self.states[0]['emergency'] = 1 - self.states[0]['emergency']
        elif command == 9540:
            self.states[0]['mode'] = 7
        elif command in (9582, 9592, 9602) and self.select_axis:
            self.axis = {9582: 0, 9592: 1, 9602: 2}[command]
        elif command == 9586:
            self.home_axes_sent.append(self.axis)
            if not self.accept_home:
                return
            self.home_busy = 1
            self.home_due = self.now + .2
        elif command == 7045 and self.reset_effect:
            self.states[0]['index'] = 0

    def sleep(self, seconds):
        super().sleep(seconds)
        if self.home_due is not None and self.complete_home and self.now >= self.home_due:
            self.states[0]['xyz'][self.axis] = 0.
            self.home_busy = 0
            self.home_due = None

    def read_with(self, process, base, decoder):
        def read(address, size):
            offset = address - base
            if offset == 0x5CC7FC and size == 4:
                return struct.pack('<I', self.home_busy)
            if offset == 0x5CC7D4 and size == 4:
                return struct.pack('<I', self.axis)
            if offset == 0x5CC998 and size == 24:
                return struct.pack('<3d', *self.states[0]['xyz'])
            raise AssertionError(('Unexpected memory read', hex(offset), size))
        return decoder(read, base)

    def read_text(self, process, hwnd, limit=32768):
        return self.reset_text if hwnd == 27045 else ''


class InitializationTests(unittest.TestCase):
    def make(self):
        self.assertIsNotNone(importlib.util.find_spec('initialization'), 'Initialization module is not implemented')
        module = importlib.import_module('initialization')
        backend = InitializationBackend()
        return module, WorkflowDriver(backend, wait_timeout=.5), backend

    def test_default_preparation_never_homes_or_starts(self):
        module, driver, backend = self.make()
        result = module.initialize_controller(driver)
        self.assertTrue(result['ok'])
        self.assertEqual(result['outcome'], 'preparation_verified_no_homing')
        self.assertFalse(result['homing']['requested'])
        self.assertEqual([command for _, command, _ in backend.commands], [9540])

    def restart_after_reference_read(self, backend, number, emergency):
        original = backend.read_with
        calls = 0

        def read_then_restart(process, base, decoder):
            nonlocal calls
            result = original(process, base, decoder)
            calls += 1
            if calls == number:
                backend.processes = [dict(backend.processes[0], creation_time=124)]
                backend.states[0]['emergency'] = emergency
            return result

        backend.read_with = read_then_restart

    def test_restart_before_emergency_release_sends_nothing_to_new_instance(self):
        module, driver, backend = self.make()
        self.restart_after_reference_read(backend, 1, emergency=1)
        with self.assertRaises(DriverError) as caught:
            module.initialize_controller(driver)
        self.assertEqual(caught.exception.code, 'process_changed')
        self.assertEqual(backend.commands, [])
        self.assertEqual(backend.states[0]['emergency'], 1)

    def test_restart_before_mode_change_sends_nothing_to_new_instance(self):
        module, driver, backend = self.make()
        self.restart_after_reference_read(backend, 2, emergency=0)
        with self.assertRaises(DriverError) as caught:
            module.initialize_controller(driver)
        self.assertEqual(caught.exception.code, 'process_changed')
        self.assertEqual(backend.commands, [])
        self.assertEqual(backend.states[0]['mode'], 2)

    def test_power_off_or_unknown_is_not_solved_by_guessing_button(self):
        for power in (0, None):
            with self.subTest(power=power):
                module, driver, backend = self.make()
                backend.states[0]['power'] = power
                with self.assertRaises(DriverError) as caught:
                    module.initialize_controller(driver, True)
                self.assertIn(caught.exception.code, ['power_off', 'power_unknown'])
                self.assertEqual(backend.commands, [])

    def test_existing_home_activity_blocks_all_mutations(self):
        module, driver, backend = self.make()
        backend.home_busy = 1
        with self.assertRaises(DriverError) as caught:
            module.initialize_controller(driver, True)
        self.assertEqual(caught.exception.code, 'home_busy')
        self.assertEqual(backend.commands, [])

    def test_homing_observes_each_axis_before_sending_next(self):
        module, driver, backend = self.make()
        result = module.initialize_controller(driver, True)
        self.assertEqual(backend.home_axes_sent, [2, 0, 1])
        self.assertTrue(result['homing']['stable_zero_observed'])
        self.assertEqual(result['homing']['final']['xyz_mm'], [0., 0., 0.])
        self.assertFalse(result['homing']['independent_reference_flags_verified'])
        self.assertNotIn(9518, [command for _, command, _ in backend.commands])

    def test_unconfirmed_axis_selection_never_sends_positive_motion(self):
        module, driver, backend = self.make()
        backend.select_axis = False
        with self.assertRaises(DriverError) as caught:
            module.initialize_controller(driver, True)
        self.assertEqual(caught.exception.code, 'axis_selection_not_verified')
        self.assertEqual(backend.home_axes_sent, [])

    def test_homing_timeout_never_advances_to_next_axis(self):
        module, driver, backend = self.make()
        backend.complete_home = False
        with self.assertRaises(DriverError) as caught:
            module.initialize_controller(driver, True)
        self.assertEqual(caught.exception.code, 'home_timeout')
        self.assertEqual(backend.home_axes_sent, [2])
        self.assertLessEqual(backend.now, 55.2)

    def test_already_zero_without_observed_home_effect_does_not_advance(self):
        module, driver, backend = self.make()
        backend.states[0]['xyz'] = [0., 0., 0.]
        backend.accept_home = False
        with self.assertRaises(DriverError) as caught:
            module.initialize_controller(driver, True)
        self.assertEqual(caught.exception.code, 'home_effect_unconfirmed')
        self.assertEqual(backend.home_axes_sent, [2])

    def test_reset_only_sends_verified_controller_button_and_reports_unknown_effect(self):
        module, driver, backend = self.make()
        backend.states[0]['index'] = 12
        result = module.reset_program(driver)
        self.assertEqual(backend.commands, [(70, 7045, 27045)])
        self.assertEqual(result['outcome'], 'command_sent_effect_unconfirmed')
        self.assertFalse(result['reset']['full_controller_reset_verified'])
        self.assertFalse(result['reset']['index_rewind_observed'])

    def test_reset_rejects_wrong_label_without_command(self):
        module, driver, backend = self.make()
        backend.reset_text = 'START'
        with self.assertRaises(DriverError) as caught:
            module.reset_program(driver)
        self.assertEqual(caught.exception.code, 'reset_control_unverified')
        self.assertEqual(backend.commands, [])

    def test_index_rewind_does_not_claim_full_controller_reset(self):
        module, driver, backend = self.make()
        backend.states[0]['index'] = 12
        backend.reset_effect = True
        result = module.reset_program(driver)
        self.assertEqual(result['outcome'], 'program_index_rewind_observed')
        self.assertTrue(result['reset']['index_rewind_observed'])
        self.assertFalse(result['reset']['full_controller_reset_verified'])


if __name__ == '__main__':
    unittest.main()
