"""Preparation identity/transition tests using only an in-memory UI boundary."""
import tempfile
import unittest
from pathlib import Path

from errors import DriverError
from test_driver import FakeBackend, window
from workflow import WorkflowDriver


PANEL_FIXTURES = {
    'stock': ('设置毛坯', (
        ('Edit', 1016, '20'), ('Edit', 1019, '30'), ('Edit', 1020, '40'), ('ComboBox', 1051, '08F 低碳钢'),
        ('Button', 1113, '添加'), ('Button', 1114, '修改'),
    )),
    'fixture': ('工件装夹', (
        ('Button', 1100, '直接装夹'), ('Button', 1101, '工艺板装夹'), ('Button', 1102, '平口钳装夹'),
    )),
    'placement': ('工件放置', (
        ('Edit', 841, '0'), ('Edit', 845, '0'), ('Edit', 847, '0'), ('Button', 4, '选择'),
    )),
    'tools': ('刀具库管理', (
        ('SysListView32', 1067, ''), ('SysListView32', 1074, ''), ('Button', 1073, '装入刀具信息文件'),
        ('Button', 862, '添加到刀库'), ('Button', 2, '取消'),
    )),
    'machine': ('参数设置', (
        ('Button', 1, '确定'), ('Button', 2, '取消'), ('Button', 1056, ''),
    )),
}


class PreparationBackend(FakeBackend):
    def __init__(self, panel='tools'):
        super().__init__()
        title, controls = PANEL_FIXTURES[panel]
        dialog = window(50, '#32770', parent=10)
        dialog['owner'] = 10
        self.dialog_rows = [dialog]
        self.texts = {50: title}
        for hwnd, (control_class, control_id, label) in enumerate(controls, start=1000):
            self.dialog_rows.append(window(hwnd, control_class, control_id, 50, 50))
            self.texts[hwnd] = label
        self.home_busy = 0
        self.field_writes = []
        self.chooser_delay = .45
        self.chooser_at = None
        self.chooser_owner = 50
        self.chooser_label = '打开(&O)'
        self.stock_at = None
        self.stock_delay = .35
        self.stock_owner = 50
        self.stock_fields_enabled = True
        self.stock_title = '设置毛坯'
        self.ct_duplicate_ids = False
        self.on_command = self.effect

    def effect(self, hwnd, command, control):
        if command in (32871, 32881, 32870, 32806, 32882):
            self.rows.extend(dict(row) for row in self.dialog_rows)
            self.rows[0]['enabled'] = False
        elif command == 1073:
            self.root()['enabled'] = False
            self.chooser_at = self.now + self.chooser_delay
        elif hwnd == 50 and command in (1113, 1114):
            self.root()['enabled'] = False
            self.stock_at = self.now + self.stock_delay
        elif hwnd == 90 and command in (1, 2):
            self.rows = [row for row in self.rows if row['root'] != 90]
            self.root()['enabled'] = True
        elif hwnd == 80 and command in (1, 2):
            self.rows = [row for row in self.rows if row['root'] != 80]
            self.root()['enabled'] = True
        elif hwnd == 50 and command == 2:
            self.rows = [row for row in self.rows if row['root'] != 50]
            self.rows[0]['enabled'] = True

    def root(self):
        return next(row for row in self.rows if row['hwnd'] == 50)

    def add_chooser(self):
        top = window(80, '#32770', parent=self.chooser_owner)
        top['owner'] = self.chooser_owner
        self.rows.extend([top, window(81, '#32770', 0, 80, 80),
                          window(82, 'Edit', 1148, 81, 80),
                          window(83, 'Button', 1, 80, 80), window(84, 'Button', 2, 80, 80)])
        self.texts.update({80: '打开', 81: '', 82: '', 83: self.chooser_label, 84: '取消'})
        if self.ct_duplicate_ids:
            self.rows.extend([window(85, 'ComboBox', 1148, 80, 80), window(86, 'SysListView32', 1, 80, 80)])
            self.texts.update({85: '', 86: 'List1'})

    def add_stock_editor(self):
        top = window(90, '#32770', parent=self.stock_owner)
        top['owner'] = self.stock_owner
        self.rows.append(top)
        self.texts[90] = self.stock_title
        for control_id in (1016, 1019, 1020):
            self.rows.append(window(9000 + control_id, 'Edit', control_id, 90, 90,
                                    enabled=self.stock_fields_enabled))
            self.texts[9000 + control_id] = '100'
        self.rows.append(window(10051, 'ComboBox', 1051, 90, 90))
        self.texts[10051] = '08F 低碳钢'
        for control_id, label in ((1, '确定'), (2, '取消'), (1087, '长方体'), (1088, '圆柱体')):
            self.rows.append(window(9000 + control_id, 'Button', control_id, 90, 90))
            self.texts[9000 + control_id] = label

    def sleep(self, seconds):
        super().sleep(seconds)
        if self.chooser_at is not None and self.now >= self.chooser_at:
            self.chooser_at = None
            self.add_chooser()
        if self.stock_at is not None and self.now >= self.stock_at:
            self.stock_at = None
            self.add_stock_editor()

    def read_text(self, process, hwnd, limit=32768):
        return self.texts.get(hwnd, '')

    def set_field(self, process, hwnd, value):
        self.field_writes.append((hwnd, value))
        self.texts[hwnd] = str(value)
        return str(value)

    def combo_options(self, process, hwnd):
        return ['08F 低碳钢']

    def _send(self, hwnd, message, wp=0, lp=0):
        return 1 if message == 0x1004 else 0


class PreparationTests(unittest.TestCase):
    def make(self, panel='tools'):
        backend = PreparationBackend(panel)
        driver = WorkflowDriver(backend, wait_timeout=.8)
        def guard(context):
            if backend.home_busy:
                raise DriverError('home_busy', 'Reference motion remains active')
        driver._require_home_idle = guard
        return driver, backend

    def assert_code(self, code, function):
        with self.assertRaises(DriverError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)

    def import_dialog(self):
        driver, backend = self.make()
        driver.open_setup('tools')
        result = driver.setup_action(button_id=1073)
        self.assertEqual(result['dialog']['hwnd'], 80)
        return driver, backend

    def test_all_five_recorded_panel_fingerprints_are_accepted(self):
        for panel in ('stock', 'fixture', 'placement', 'tools', 'machine'):
            with self.subTest(panel=panel):
                driver, _ = self.make(panel)
                self.assertEqual(driver.open_setup(panel)['outcome'], 'setup_dialog_ready')

    def test_unowned_or_wrong_panel_is_not_adopted(self):
        for failure in ('owner', 'title'):
            driver, backend = self.make()
            if failure == 'owner':
                for row in backend.dialog_rows:
                    if row['hwnd'] == 50:
                        row['parent'] = row['owner'] = 0
            else:
                backend.texts[50] = 'Unrelated dialog'
            self.assert_code('setup_dialog_unverified', lambda: driver.open_setup('tools'))
            self.assertEqual(len(backend.commands), 1)

    def test_changed_root_identity_blocks_actions(self):
        driver, backend = self.make()
        driver.open_setup('tools')
        backend.root()['owner'] = 0
        self.assert_code('setup_window_changed', lambda: driver.setup_action(button_id=1073))
        self.assertEqual(len(backend.commands), 1)

    def test_home_busy_blocks_open_and_subsequent_actions(self):
        driver, backend = self.make()
        backend.home_busy = 1
        self.assert_code('home_busy', lambda: driver.open_setup('tools'))
        self.assertEqual(backend.commands, [])
        backend.home_busy = 0
        driver.open_setup('tools')
        backend.home_busy = 1
        self.assert_code('home_busy', lambda: driver.setup_action(button_id=1073))
        self.assertEqual(len(backend.commands), 1)

    def test_delayed_ct_chooser_retains_origin_and_nested_filename_edit(self):
        driver, backend = self.import_dialog()
        self.assertIsNotNone(driver.preparation.origin)
        self.assertGreaterEqual(backend.now, .45)
        self.assertEqual(driver.preparation.root, 50)

    def test_transition_timeout_does_not_forget_live_disabled_root(self):
        driver, backend = self.make()
        backend.chooser_delay = 10
        driver.open_setup('tools')
        self.assert_code('setup_transition_timeout', lambda: driver.setup_action(button_id=1073))
        self.assertIsNotNone(driver.preparation.origin)
        backend.add_chooser()
        self.assertEqual(driver.inspect_setup_dialog()['dialog']['hwnd'], 80)

    def test_unrequested_or_wrong_ct_successor_is_rejected(self):
        driver, backend = self.make()
        driver.open_setup('tools')
        backend.root()['enabled'] = False
        backend.add_chooser()
        self.assert_code('unrelated_dialog', driver.inspect_setup_dialog)
        for field, value in [('chooser_owner', 10), ('chooser_label', '保存(&S)')]:
            driver, backend = self.make()
            setattr(backend, field, value)
            driver.open_setup('tools')
            self.assert_code('unrelated_dialog', lambda: driver.setup_action(button_id=1073))
            self.assertFalse(any(command == 1 for _, command, _ in backend.commands))

    def test_ct_path_must_be_existing_absolute_ct_file(self):
        driver, backend = self.import_dialog()
        with tempfile.TemporaryDirectory() as folder:
            wrong = Path(folder) / 'wrong.cnc'
            wrong.write_text('not a CT file', encoding='ascii')
            for path in ('relative.ct', str(Path(folder) / 'missing.ct'), str(wrong)):
                self.assert_code('invalid_ct_input', lambda path=path: driver.setup_action(control_id=1148, value=path))
            self.assertEqual(backend.field_writes, [])
        self.assert_code('ct_input_not_verified', lambda: driver.setup_action(button_id=1))

    def test_changed_ct_input_is_not_submitted(self):
        driver, backend = self.import_dialog()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.ct'
            path.write_bytes(b'first')
            driver.setup_action(control_id=1148, value=str(path))
            path.write_bytes(b'second')
            self.assert_code('ct_input_changed', lambda: driver.setup_action(button_id=1))
            self.assertFalse(any(hwnd == 80 and command == 1 for hwnd, command, _ in backend.commands))

    def test_valid_ct_import_returns_to_same_root_without_claiming_content_verification(self):
        driver, backend = self.import_dialog()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '中文刀具.ct'
            path.write_bytes(b'bounded native tool input')
            driver.setup_action(control_id=1148, value=str(path))
            result = driver.setup_action(button_id=1)
        self.assertEqual(result['dialog']['hwnd'], 50)
        self.assertIsNotNone(driver.preparation.origin)
        self.assertFalse(result['last_action']['import_effect_verified'])
        self.assertIn((80, 1, 83), backend.commands)

    def test_filename_change_during_final_context_check_blocks_open(self):
        driver, backend = self.import_dialog()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.ct'
            path.write_bytes(b'first')
            driver.setup_action(control_id=1148, value=str(path))
            checks = 0
            def external_change(context):
                nonlocal checks
                checks += 1
                if checks == 2:
                    backend.texts[82] = str(Path(folder) / 'unrequested.ct')
            driver._require_home_idle = external_change
            self.assert_code('ct_input_changed', lambda: driver.setup_action(button_id=1))
        self.assertFalse(any(hwnd == 80 and command == 1 for hwnd, command, _ in backend.commands))

    def test_confirmed_root_close_releases_flow_identity(self):
        driver, _ = self.make()
        driver.open_setup('tools')
        self.assertEqual(driver.setup_action(button_id=2)['outcome'], 'dialog_closed')
        self.assertIsNone(driver.preparation.origin)

    def test_stock_add_and_modify_wait_for_editor_then_return_to_original_root(self):
        for command in (1113, 1114):
            with self.subTest(command=command):
                driver, backend = self.make('stock')
                driver.open_setup('stock')
                result = driver.setup_action(button_id=command)
                self.assertEqual(result['dialog_kind'], 'stock_editor')
                self.assertEqual(result['dialog']['hwnd'], 90)
                self.assertGreaterEqual(backend.now, .35)
                driver.setup_action(control_id=1020, value='50')
                self.assertIn((10020, '50'), backend.field_writes)
                result = driver.setup_action(button_id=1)
                self.assertEqual(result['dialog']['hwnd'], 50)
                self.assertIsNotNone(driver.preparation.origin)
                self.assertFalse(result['last_action']['setup_effect_verified'])

    def test_stock_editor_requires_requested_transition_owner_and_enabled_dimensions(self):
        driver, backend = self.make('stock')
        driver.open_setup('stock')
        backend.root()['enabled'] = False
        backend.add_stock_editor()
        self.assert_code('unrelated_dialog', driver.inspect_setup_dialog)
        for field, value in (('stock_owner', 10), ('stock_title', 'Other dialog'), ('stock_fields_enabled', False)):
            with self.subTest(field=field):
                driver, backend = self.make('stock')
                setattr(backend, field, value)
                driver.open_setup('stock')
                self.assert_code('unrelated_dialog', lambda: driver.setup_action(button_id=1113))
                self.assertEqual(len(backend.commands), 2)

    def test_stock_transition_timeout_retains_scope_and_cancel_returns_to_root(self):
        driver, backend = self.make('stock')
        backend.stock_delay = 10
        driver.open_setup('stock')
        self.assert_code('setup_transition_timeout', lambda: driver.setup_action(button_id=1114))
        self.assertIsNotNone(driver.preparation.origin)
        backend.add_stock_editor()
        self.assertEqual(driver.inspect_setup_dialog()['dialog_kind'], 'stock_editor')
        self.assertEqual(driver.setup_action(button_id=2)['dialog']['hwnd'], 50)

    def test_stock_editor_replacement_is_rejected_before_field_write(self):
        driver, backend = self.make('stock')
        driver.open_setup('stock')
        driver.setup_action(button_id=1113)
        next(row for row in backend.rows if row['hwnd'] == 90)['owner'] = 10
        self.assert_code('unrelated_dialog', lambda: driver.setup_action(control_id=1020, value='50'))
        self.assertEqual(backend.field_writes, [])

    def test_unknown_tool_child_reports_pending_without_adopting_it(self):
        driver, backend = self.make('tools')
        driver.open_setup('tools')
        original = backend.on_command
        def show_unknown(hwnd, command, control):
            original(hwnd, command, control)
            if command == 862:
                backend.root()['enabled'] = False
                child = window(95, '#32770', parent=50)
                child['owner'] = 50
                backend.rows.append(child)
        backend.on_command = show_unknown
        with self.assertRaises(DriverError) as caught:
            driver.setup_action(button_id=862)
        self.assertEqual(caught.exception.code, 'unrelated_dialog')
        self.assertEqual(caught.exception.details['pending'], 'unverified_successor')
        self.assertEqual(caught.exception.details['trigger']['button_id'], 862)
        self.assertEqual(driver.preparation.root, 50)

    def test_ct_controls_with_duplicate_ids_are_selected_by_action_class(self):
        driver, backend = self.make()
        backend.ct_duplicate_ids = True
        driver.open_setup('tools')
        driver.setup_action(button_id=1073)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'duplicate.ct'
            path.write_bytes(b'CT input')
            driver.setup_action(control_id=1148, value=str(path))
            self.assertEqual(backend.field_writes[-1][0], 82)
            self.assertEqual(driver.setup_action(button_id=1)['dialog']['hwnd'], 50)
        self.assertIn((80, 1, 83), backend.commands)


if __name__ == '__main__':
    unittest.main()
