import unittest

import win32_backend
from constants import TARGET_PATH
from errors import DriverError


class ModuleInventoryTests(unittest.TestCase):
    def select(self, candidates):
        selector = getattr(win32_backend, "select_module_base", None)
        self.assertIsNotNone(selector, "Deduplication of identical WOW64 module records is not implemented")
        return selector(candidates)

    def test_wow64_duplicate_records_are_one_physical_module(self):
        entries = [dict(path=TARGET_PATH, base=0x590000), dict(path=TARGET_PATH, base=0x590000)]
        self.assertEqual(self.select(entries), 0x590000)

    def test_different_module_bases_are_still_ambiguous(self):
        with self.assertRaises(DriverError) as error:
            self.select([dict(path=TARGET_PATH, base=0x590000), dict(path=TARGET_PATH, base=0x880000)])
        self.assertEqual(error.exception.code, "module_ambiguous")
