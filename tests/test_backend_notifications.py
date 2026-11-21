"""Combo notification tests without creating a Windows backend or sending UI messages."""
from types import SimpleNamespace
import unittest

from errors import DriverError
from win32_backend import WindowsBackend


class ComboBackend(WindowsBackend):
    def __init__(self):
        self.row = dict(hwnd=51, pid=101, cls='ComboBox', id=1051, parent=50, root=50, owner=0,
                        visible=True, enabled=True)
        self.checked = []
        self.messages = []
        self.notifications = []
        self.change_after_options = False
        self.change_after_selection = False
        self.accept_notification = True
        self.parent_class = '#32770'
        self.replace_parent_after_selection = False
        self.selected = 0
        self.u = SimpleNamespace(PostMessageW=self.post)

    def _check_window(self, process, hwnd, expected_class=None):
        self.checked.append(hwnd)
        if hwnd == 51:
            return dict(self.row)
        return dict(hwnd=hwnd, pid=101, cls=self.parent_class, parent=10, root=hwnd, owner=10,
                    id=0, visible=True, enabled=True)

    def combo_options(self, process, hwnd):
        if self.change_after_options:
            self.row['parent'] = 60
        return ['A', 'B']

    def _send(self, hwnd, message, wp=0, lp=0):
        self.messages.append((hwnd, message, wp, lp))
        if message == 0x14E:
            self.selected = wp
            if self.change_after_selection:
                self.row['parent'] = 60
            if self.replace_parent_after_selection:
                self.parent_class = 'Button'
        return self.selected

    def post(self, hwnd, message, wp, lp):
        self.notifications.append((hwnd, message, wp, lp))
        return self.accept_notification

    def _fail(self, code, message):
        raise DriverError(code, message)


class ComboNotificationTests(unittest.TestCase):
    def test_identity_change_before_selection_sends_no_mutation(self):
        backend = ComboBackend()
        backend.change_after_options = True
        with self.assertRaises(DriverError) as caught:
            backend.select_combo({'pid': 101}, 51, 'B')
        self.assertEqual(caught.exception.code, 'window_changed')
        self.assertEqual(backend.messages, [])
        self.assertEqual(backend.notifications, [])

    def test_identity_change_before_notification_blocks_parent_message(self):
        backend = ComboBackend()
        backend.change_after_selection = True
        with self.assertRaises(DriverError) as caught:
            backend.select_combo({'pid': 101}, 51, 'B')
        self.assertEqual(caught.exception.code, 'window_changed')
        self.assertEqual(backend.notifications, [])

    def test_rejected_notification_is_reported(self):
        backend = ComboBackend()
        backend.accept_notification = False
        with self.assertRaises(DriverError) as caught:
            backend.select_combo({'pid': 101}, 51, 'B')
        self.assertEqual(caught.exception.code, 'command_rejected')

    def test_reused_parent_handle_does_not_receive_notification(self):
        backend = ComboBackend()
        backend.replace_parent_after_selection = True
        with self.assertRaises(DriverError) as caught:
            backend.select_combo({'pid': 101}, 51, 'B')
        self.assertEqual(caught.exception.code, 'window_changed')
        self.assertEqual(backend.notifications, [])

    def test_confirmed_control_notifies_its_reverified_parent(self):
        backend = ComboBackend()
        backend.select_combo({'pid': 101}, 51, 'B')
        self.assertIn(50, backend.checked)
        self.assertEqual(backend.notifications, [(50, 0x111, 1051 | (1 << 16), 51)])


if __name__ == '__main__':
    unittest.main()
