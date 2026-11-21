"""MFC lazy menu update contract; no real windows are used."""
from types import SimpleNamespace
import unittest
from win32_backend import WindowsBackend
from errors import DriverError


class Function:
    def __init__(self, call): self.call = call
    def __call__(self, *args): return self.call(*args)


class MenuBackend(WindowsBackend):
    def __init__(self):
        self.actual = True
        self.cached = False
        self.commands = []
        self.checks = 0
        self.u = SimpleNamespace(
            GetMenu=Function(lambda hwnd: 100),
            GetMenuState=Function(lambda menu, command, flag: 8 if self.cached else 0),
            GetMenuItemCount=Function(lambda menu: 2 if menu == 100 else 1),
            GetMenuItemID=Function(lambda menu, index: 32798 if menu == 300 else 0xffffffff),
            GetSubMenu=Function(lambda menu, index: (200, 300)[index] if menu == 100 else 0))
    def _check_window(self, *args): self.checks += 1
    def menu_items(self, *args): return []
    def _send(self, *args):
        self.commands.append(args)
        self.cached = self.actual


class MenuStateTests(unittest.TestCase):
    def test_reads_refreshed_application_state_instead_of_stale_checkmark(self):
        backend = MenuBackend()
        self.assertTrue(backend.menu_checked({}, 10, 32798))
        self.assertEqual(backend.commands, [(10, 0x117, 300, 1)])
        backend.actual = False
        self.assertFalse(backend.menu_checked({}, 10, 32798))
        self.assertGreaterEqual(backend.checks, 6)
    def test_missing_item_does_not_dispatch_a_command(self):
        backend = MenuBackend()
        with self.assertRaises(DriverError): backend.menu_checked({}, 10, 32797)
        self.assertEqual(backend.commands, [])


if __name__ == '__main__': unittest.main()
