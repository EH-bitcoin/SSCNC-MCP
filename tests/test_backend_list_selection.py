"""List selection boundary tests; no Windows backend or native UI is created."""
import unittest

from errors import DriverError
from win32_backend import WindowsBackend


COUNT = 0x1004
SELECTED = 0x100C
KEYDOWN = 0x100
KEYUP = 0x101
HOME = 0x24
DOWN = 0x28


class ListBackend(WindowsBackend):
    def __init__(self):
        self.row = dict(hwnd=51, pid=101, cls='SysListView32', id=1451,
                        parent=50, root=50, owner=0, visible=True, enabled=True)
        self.parent = dict(hwnd=50, pid=101, cls='#32770', id=0,
                           parent=10, root=50, owner=10, visible=True, enabled=True)
        self.messages = []
        self.checked = []
        self.change_after = None
        self.change_kind = 'reparent'
        self.fail_at = None
        self.selected = 0
        self.readback = None

    def _check_window(self, process, hwnd, expected_class=None):
        self.checked.append(hwnd)
        current = dict(self.row if hwnd == 51 else self.parent)
        if expected_class and current['cls'] != expected_class:
            raise DriverError('window_changed', 'Unexpected control class')
        return current

    def _send(self, hwnd, message, wp=0, lp=0):
        self.messages.append((hwnd, message, wp, lp))
        if self.fail_at == len(self.messages):
            raise DriverError('message_timeout', 'Fake bounded send failure')
        if message == KEYDOWN:
            self.selected = 0 if wp == HOME else self.selected + 1
        if self.change_after == len(self.messages):
            if self.change_kind == 'reparent':
                self.row.update(parent=90, root=90)
            elif self.change_kind == 'list_reused':
                self.row['id'] = 9999
            elif self.change_kind == 'parent_reused':
                self.parent['cls'] = 'Button'
            elif self.change_kind == 'parent_root':
                self.parent['root'] = 90
        if message == COUNT:
            return 4
        if message == SELECTED:
            return self.selected if self.readback is None else self.readback
        return 0  # A successful WM_KEYDOWN/UP need not return a nonzero value.


class ListSelectionTests(unittest.TestCase):
    def test_reparent_after_item_count_blocks_all_keys(self):
        backend = ListBackend()
        backend.change_after = 1
        with self.assertRaises(DriverError) as caught:
            backend.select_list_row({'pid': 101}, 51, 2)
        self.assertEqual(caught.exception.code, 'window_changed')
        self.assertEqual(backend.messages, [(51, COUNT, 0, 0)])

    def test_each_key_boundary_and_final_read_reject_identity_change(self):
        for change_after in range(2, 8):
            with self.subTest(change_after=change_after):
                backend = ListBackend()
                backend.change_after = change_after
                with self.assertRaises(DriverError) as caught:
                    backend.select_list_row({'pid': 101}, 51, 2)
                self.assertEqual(caught.exception.code, 'window_changed')
                self.assertEqual(len(backend.messages), change_after)
                self.assertNotIn(SELECTED, [message[1] for message in backend.messages])

    def test_reused_list_or_parent_handle_is_not_accepted(self):
        for change_kind in ('list_reused', 'parent_reused', 'parent_root'):
            with self.subTest(change_kind=change_kind):
                backend = ListBackend()
                backend.change_after = 1
                backend.change_kind = change_kind
                with self.assertRaises(DriverError) as caught:
                    backend.select_list_row({'pid': 101}, 51, 1)
                self.assertEqual(caught.exception.code, 'window_changed')
                self.assertEqual(backend.messages, [(51, COUNT, 0, 0)])

    def test_change_during_readback_does_not_report_success(self):
        backend = ListBackend()
        backend.change_after = 4  # Count, Home down/up, selection readback.
        with self.assertRaises(DriverError) as caught:
            backend.select_list_row({'pid': 101}, 51, 0)
        self.assertEqual(caught.exception.code, 'window_changed')
        self.assertEqual(len(backend.messages), 4)

    def test_normal_selection_accepts_zero_key_returns_and_reads_back(self):
        backend = ListBackend()
        backend.select_list_row({'pid': 101}, 51, 2)
        self.assertEqual(backend.messages, [
            (51, COUNT, 0, 0),
            (51, KEYDOWN, HOME, 0), (51, KEYUP, HOME, 0),
            (51, KEYDOWN, DOWN, 0), (51, KEYUP, DOWN, 0),
            (51, KEYDOWN, DOWN, 0), (51, KEYUP, DOWN, 0),
            (51, SELECTED, -1, 2),
        ])
        self.assertIn(50, backend.checked)
        self.assertEqual(backend.selected, 2)

    def test_send_failure_stops_remaining_keys_and_readback(self):
        backend = ListBackend()
        backend.fail_at = 3
        with self.assertRaises(DriverError) as caught:
            backend.select_list_row({'pid': 101}, 51, 2)
        self.assertEqual(caught.exception.code, 'message_timeout')
        self.assertEqual(len(backend.messages), 3)

    def test_invalid_index_never_sends_keys(self):
        for index in (-1, 4, '1'):
            with self.subTest(index=index):
                backend = ListBackend()
                with self.assertRaises(DriverError) as caught:
                    backend.select_list_row({'pid': 101}, 51, index)
                self.assertEqual(caught.exception.code, 'invalid_row')
                self.assertEqual(backend.messages, [(51, COUNT, 0, 0)])

    def test_unexpected_selected_row_is_not_success(self):
        backend = ListBackend()
        backend.readback = 0
        with self.assertRaises(DriverError) as caught:
            backend.select_list_row({'pid': 101}, 51, 2)
        self.assertEqual(caught.exception.code, 'selection_not_verified')
        self.assertEqual(caught.exception.details['observed'], 0)


if __name__ == '__main__':
    unittest.main()
