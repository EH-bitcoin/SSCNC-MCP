"""Scoped preparation dialogs, based on recorded live identities.

Tools/1073 and stock/1113/1114 have verified successor dialogs. Disabled parents
remain live flow roots during bounded transitions instead of appearing closed.
"""
import hashlib
import math
from pathlib import Path
import re

from errors import DriverError

PANELS = {'stock': 32871, 'fixture': 32881, 'placement': 32870, 'tools': 32806, 'machine': 32882}
# Window fingerprints for the supported MF build's preparation dialogs.
PANEL_SIGNATURES = {
    'stock': ('设置毛坯', {('Edit', 1016), ('Edit', 1019), ('Edit', 1020), ('ComboBox', 1051),
                         ('Button', 1113), ('Button', 1114)}),
    'fixture': ('工件装夹', {('Button', 1100), ('Button', 1101), ('Button', 1102)}),
    'placement': ('工件放置', {('Edit', 841), ('Edit', 845), ('Edit', 847), ('Button', 4)}),
    'tools': ('刀具库管理', {('SysListView32', 1067), ('SysListView32', 1074), ('Button', 1073)}),
    'machine': ('参数设置', {('Button', 1), ('Button', 2), ('Button', 1056)}),
}
BUTTON_TEXTS = frozenset(('确定', '取消', '添加', '修改', '导入', '导出', '选择', '安装', '卸下',
                         'OK', 'Cancel', 'Add', 'Modify', 'Import', 'Export', '长方体', '圆柱体',
                         '直接装夹', '工艺板装夹', '平口钳装夹', 'L', 'S', '放置',
                         '选择放入刀架', '添加到主轴', '装入刀具信息文件', '添加到刀库', '刀具库管理'))
IDENTITY_FIELDS = ('hwnd', 'pid', 'cls', 'id', 'parent', 'root', 'owner')


def _identity(row):
    return tuple(row[key] for key in IDENTITY_FIELDS)


def _label(text):
    return re.sub(r'[（(]&[A-Za-z][）)]', '', text).replace('&', '').strip()


def _ct_input(value):
    try:
        if not isinstance(value, str) or not value or value.startswith(('\\\\', '//')):
            raise ValueError('Use a local absolute filename')
        path = Path(value)
        if not path.is_absolute() or path.suffix.casefold() != '.ct':
            raise ValueError('Use an absolute .ct filename')
        path = path.resolve(strict=True)
        if str(path).startswith(('\\\\', '//')) or path.suffix.casefold() != '.ct' or not path.is_file():
            raise ValueError('The resolved input must be a local .ct file')
        before = path.stat()
        if not 0 < before.st_size <= 16 * 1024 * 1024:
            raise ValueError('CT input must be nonempty and at most 16 MiB')
        with path.open('rb') as stream:
            content = stream.read(16 * 1024 * 1024 + 1)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != before.st_size:
            raise ValueError('CT input changed while being read')
        return dict(path=str(path), size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    except (OSError, ValueError) as exc:
        raise DriverError('invalid_ct_input', 'Choose an existing local absolute .ct file of at most 16 MiB',
                          reason=str(exc)) from exc


class PreparationFlow:
    def __init__(self, driver):
        self.driver = driver
        self.origin = None
        self.root = None
        self.panel = None
        self.root_identity = None
        self.chooser = None
        self.stock_editor = None
        self.stock_operation = None
        self.pending = None
        self.last_trigger = None
        self.ct_input = None
        self.last_action = None
        self.seen = {}
        self.seen_text = {}

    def _panel_matches(self, context, row):
        main = context['main']['hwnd']
        if not (row['cls'] == '#32770' and row['root'] == row['hwnd'] and
                row['owner'] == main and row['parent'] == main):
            return False
        title, required = PANEL_SIGNATURES[self.panel]
        if self.driver.backend.read_text(context['process'], row['hwnd']).strip() != title:
            return False
        observed = {(child['cls'], child['id']) for child in context['windows'] if child['root'] == row['hwnd']}
        return required <= observed

    def _ct_chooser_matches(self, context, row):
        if not (row['cls'] == '#32770' and row['root'] == row['hwnd'] and
                row['owner'] == self.root and row['parent'] == self.root):
            return False
        backend, process = self.driver.backend, context['process']
        if _label(backend.read_text(process, row['hwnd'])).casefold() not in ('打开', 'open'):
            return False
        children = [child for child in context['windows'] if child['root'] == row['hwnd']]
        edits = [child for child in children if child['cls'] == 'Edit' and child['id'] == 1148]
        opens = [child for child in children if child['cls'] == 'Button' and child['id'] == 1 and child['parent'] == row['hwnd']]
        return (len(edits) == len(opens) == 1 and
                _label(backend.read_text(process, opens[0]['hwnd'])).casefold() in ('打开', 'open'))

    def _stock_editor_matches(self, context, row):
        # Both Add (1113) and Modify (1114) open the observed same-title child.
        if not (row['cls'] == '#32770' and row['root'] == row['hwnd'] and
                row['owner'] == self.root and row['parent'] == self.root):
            return False
        backend, process = self.driver.backend, context['process']
        if backend.read_text(process, row['hwnd']).strip() != '设置毛坯':
            return False
        children = [child for child in context['windows'] if child['root'] == row['hwnd']
                    and child['parent'] == row['hwnd'] and child['visible']]
        required = [('Edit', 1016), ('Edit', 1019), ('Edit', 1020), ('ComboBox', 1051)]
        labels = {1: '确定', 2: '取消', 1087: '长方体', 1088: '圆柱体'}
        for cls, control_id in required + [('Button', key) for key in labels]:
            matches = [child for child in children if child['cls'] == cls and child['id'] == control_id]
            if len(matches) != 1:
                return False
            control = matches[0]
            # Initial binding must distinguish editable dimensions from the
            # disabled preview fields in the outer same-title stock dialog.
            # A bound shape selector may subsequently disable individual fields.
            if self.stock_editor is None and cls in ('Edit', 'ComboBox') and not control['enabled']:
                return False
            if cls == 'Button' and _label(backend.read_text(process, control['hwnd'])) != labels[control_id]:
                return False
        return True

    def _unrelated(self, message, dialogs):
        if self.pending is None:
            self.pending = 'unverified_successor'
        raise DriverError('unrelated_dialog', message, pending=self.pending, trigger=self.last_trigger,
                          dialogs=[dict(hwnd=row['hwnd'], owner=row['owner'], parent=row['parent']) for row in dialogs])

    def _context(self):
        if self.origin is None:
            raise DriverError('setup_not_open', 'Open a preparation panel before sending field actions')
        context, state = self.driver._refresh(self.origin)
        raw = state['telemetry']
        if not state['responsive'] or raw is None or not raw['coherent'] or raw['worker'] or raw['cycle']:
            raise DriverError('setup_state_changed', 'Preparation requires a responsive stopped simulator')
        self.driver._require_home_idle(context)
        modals = self.driver._modals(context)
        if self.root is None:
            if not modals:
                return context, state, None, 'transition'
            if len(modals) != 1 or not self._panel_matches(context, modals[0]):
                raise DriverError('setup_dialog_unverified', 'The requested preparation dialog identity was not established')
            self.root = modals[0]['hwnd']
            self.root_identity = _identity(modals[0])
            self.pending = None
        rows = [row for row in context['windows'] if row['hwnd'] == self.root]
        if not rows:
            if modals or self.pending or not state['main_enabled']:
                raise DriverError('setup_window_changed', 'The preparation root disappeared during a dialog transition')
            self.origin = None
            self.seen = {}
            self.seen_text = {}
            return context, state, None, 'closed'
        root = rows[0]
        if _identity(root) != self.root_identity or not self._panel_matches(context, root):
            raise DriverError('setup_window_changed', 'The preparation root identity or fingerprint changed')
        extras = [row for row in modals if row['hwnd'] != self.root]
        if extras:
            if len(extras) != 1:
                self._unrelated('Multiple unverified preparation successors appeared', extras)
            child = extras[0]
            if self.pending == 'stock_open' or self.stock_editor is not None:
                if not self._stock_editor_matches(context, child):
                    self._unrelated('The successor is not the requested stock editor', extras)
                if self.stock_editor is None:
                    self.stock_editor = _identity(child)
                elif _identity(child) != self.stock_editor:
                    raise DriverError('setup_window_changed', 'The stock editor identity changed')
                if root['enabled'] and child['enabled']:
                    raise DriverError('ambiguous_dialog', 'Both stock root and its editor are enabled')
                if self.pending == 'stock_return' or not child['enabled']:
                    return context, state, None, 'transition'
                self.pending = None
                return context, state, child, 'stock_editor'
            if not (self.pending == 'ct_open' or self.chooser is not None):
                self._unrelated('An unrequested dialog appeared outside the known preparation transition', extras)
            if not self._ct_chooser_matches(context, child):
                self._unrelated('The successor is not the tools dialog CT Open chooser', extras)
            if self.chooser is None:
                self.chooser = _identity(child)
            elif _identity(child) != self.chooser:
                raise DriverError('setup_window_changed', 'The CT chooser identity changed')
            if root['enabled'] and child['enabled']:
                raise DriverError('ambiguous_dialog', 'Both the preparation root and its modal chooser are enabled')
            if self.pending == 'ct_return' or not child['enabled']:
                return context, state, None, 'transition'
            self.pending = None
            return context, state, child, 'ct_chooser'
        if self.pending in ('ct_open', 'stock_open'):
            return context, state, None, 'transition'
        if self.stock_editor is not None:
            if any(row['hwnd'] == self.stock_editor[0] for row in context['windows']):
                return context, state, None, 'transition'
            if root['enabled']:
                self.stock_editor = None
                self.stock_operation = None
                self.pending = None
        if self.chooser is not None:
            if any(row['hwnd'] == self.chooser[0] for row in context['windows']):
                return context, state, None, 'transition'
            if root['enabled']:
                self.chooser = None
                self.ct_input = None
                self.pending = None
        if not root['visible'] or not root['enabled']:
            return context, state, None, 'transition'
        if self.pending == 'unverified_successor':
            self.pending = None
        return context, state, root, 'panel'

    def _ready(self):
        timeout = self.driver.wait_timeout
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise DriverError('invalid_wait_timeout', 'A finite positive dialog transition timeout is required')
        deadline = self.driver.backend.monotonic() + min(timeout, 10.0)
        while True:
            observed = self._context()
            if observed[3] != 'transition':
                return observed
            remaining = deadline - self.driver.backend.monotonic()
            if remaining <= 0:
                raise DriverError('setup_transition_timeout', 'Preparation transition did not settle; the flow identity is retained',
                                  panel=self.panel, root=self.root, pending=self.pending)
            self.driver.backend.sleep(min(.1, remaining))

    def open(self, panel):
        if panel not in PANELS:
            raise DriverError('invalid_panel', 'Choose stock, fixture, placement, tools or machine')
        context, state = self.driver._capture()
        self.driver._guard(context, state, allow_emergency=True)
        self.driver._require_home_idle(context)
        context, state = self.driver._refresh(context)
        self.driver._guard(context, state, allow_emergency=True)
        self.driver._require_home_idle(context)
        self.driver.backend.post_command(context['process'], context['main']['hwnd'], PANELS[panel])
        self.origin, self.panel, self.root = context, panel, None
        self.root_identity = self.chooser = self.ct_input = self.last_action = None
        self.stock_editor = self.stock_operation = self.last_trigger = None
        self.pending = 'open'
        self.seen, self.seen_text = {}, {}
        return self.inspect()

    def inspect(self):
        context, state, dialog, kind = self._ready()
        if kind == 'closed':
            return dict(ok=True, outcome='dialog_closed', panel=self.panel, status=state)
        controls = []
        self.seen, self.seen_text = {}, {}
        for row in context['windows']:
            if row['root'] != dialog['hwnd'] or not row['visible']:
                continue
            item = dict(row)
            item['text'] = self.driver.backend.read_text(context['process'], row['hwnd'])
            if row['cls'] == 'ComboBox' and row['enabled']:
                item['options'] = self.driver.backend.combo_options(context['process'], row['hwnd'])
            if row['cls'] == 'SysListView32':
                item['row_count'] = self.driver.backend._send(row['hwnd'], 0x1004)
                item['selected_row'] = self.driver.backend._send(row['hwnd'], 0x100C, -1, 2)
            controls.append(item)
            self.seen[row['hwnd']] = dict(row)
            self.seen_text[row['hwnd']] = item['text']
        result = dict(ok=True, outcome='setup_dialog_ready', panel=self.panel, dialog_kind=kind, dialog=dialog,
                      controls=controls, status=state)
        if self.last_action is not None:
            result['last_action'] = dict(self.last_action)
        if kind == 'ct_chooser':
            result['input_contract'] = 'Only an explicitly supplied existing local absolute .ct file may be opened.'
        return result

    def _revalidate(self, dialog, control):
        context, _, current, kind = self._context()
        rows = [row for row in context['windows'] if row['hwnd'] == control['hwnd']]
        if current is None or _identity(current) != _identity(dialog) or len(rows) != 1 or rows[0] != control:
            raise DriverError('control_changed', 'The preparation control changed before dispatch')
        self.driver._enabled_chain(context, rows[0])
        return context, kind

    def action(self, control_id=None, value=None, button_id=None, row_index=None):
        if sum(item is not None for item in (value, button_id, row_index)) != 1:
            raise DriverError('invalid_setup_action', 'Choose exactly one field value, button or list row')
        context, _, dialog, kind = self._ready()
        if dialog is None:
            raise DriverError('setup_closed', 'The preparation dialog is no longer open')
        requested = button_id if button_id is not None else control_id
        target_classes = ({'Button'} if button_id is not None else {'SysListView32'} if row_index is not None
                          else {'Edit'} if kind == 'ct_chooser' else {'Edit', 'ComboBox'})
        candidates = [row for row in context['windows'] if row['root'] == dialog['hwnd']
                      and row['id'] == requested and row['cls'] in target_classes and row['visible'] and row['enabled']]
        if len(candidates) != 1 or candidates[0]['hwnd'] not in self.seen:
            raise DriverError('control_not_observed', 'Inspect the active dialog and use one of its enabled controls')
        control = candidates[0]
        if control != self.seen[control['hwnd']]:
            raise DriverError('control_changed', 'The observed preparation control changed')
        backend = self.driver.backend
        if kind == 'stock_editor':
            valid_button = button_id in (1, 2, 1087, 1088) and control['cls'] == 'Button'
            valid_field = value is not None and ((control['cls'] == 'Edit' and control['id'] in (1016, 1019, 1020)) or
                                                (control['cls'] == 'ComboBox' and control['id'] == 1051))
            if not (valid_button or valid_field) or row_index is not None:
                raise DriverError('unsupported_stock_action', 'Use only the observed stock dimensions, material, shape, OK or Cancel')
        if kind == 'ct_chooser':
            self._ct_action(context, dialog, control, value, button_id, row_index)
        elif button_id is not None:
            label = _label(backend.read_text(context['process'], control['hwnd']))
            if control['cls'] != 'Button' or label not in BUTTON_TEXTS:
                raise DriverError('unsupported_setup_button', 'This preparation action has no verified meaning', label=label)
            if label != _label(self.seen_text[control['hwnd']]):
                raise DriverError('control_changed', 'The preparation button label changed')
            stock_open = self.panel == 'stock' and kind == 'panel' and button_id in (1113, 1114)
            if stock_open and label != {1113: '添加', 1114: '修改'}[button_id]:
                raise DriverError('control_changed', 'The stock Add/Modify button label differs from its observed meaning')
            context, _ = self._revalidate(dialog, control)
            backend.post_command(context['process'], control['parent'], button_id, control['hwnd'])
            self.last_trigger = dict(panel=self.panel, dialog_kind=kind, button_id=button_id, label=label)
            if self.panel == 'tools' and button_id == 1073 and label == '装入刀具信息文件':
                self.pending = 'ct_open'
                self.ct_input = None
            elif stock_open:
                self.pending = 'stock_open'
                self.stock_operation = 'add' if button_id == 1113 else 'modify'
            elif kind == 'stock_editor' and button_id in (1, 2):
                self.pending = 'stock_return'
                self.last_action = dict(kind='stock_editor', operation=self.stock_operation,
                                        accepted=button_id == 1, command_sent=True, setup_effect_verified=False)
        elif row_index is not None:
            if control['cls'] != 'SysListView32':
                raise DriverError('wrong_control', 'Row selection requires a visible preparation list')
            context, _ = self._revalidate(dialog, control)
            backend.select_list_row(context['process'], control['hwnd'], row_index)
        elif control['cls'] == 'Edit':
            if len(str(value)) > 1024:
                raise DriverError('value_too_long', 'The preparation value exceeds the text limit')
            context, _ = self._revalidate(dialog, control)
            if backend.set_field(context['process'], control['hwnd'], value) != str(value):
                raise DriverError('field_not_verified', 'The preparation field did not retain the value')
        elif control['cls'] == 'ComboBox':
            context, _ = self._revalidate(dialog, control)
            backend.select_combo(context['process'], control['hwnd'], str(value))
        else:
            raise DriverError('wrong_control', 'Only observed Edit and ComboBox values can be set')
        backend.sleep(.1)
        return self.inspect()

    def _ct_action(self, context, dialog, control, value, button_id, row_index):
        backend = self.driver.backend
        if value is not None and control['cls'] == 'Edit' and control['id'] == 1148:
            supplied = _ct_input(value)
            context, _ = self._revalidate(dialog, control)
            if backend.set_field(context['process'], control['hwnd'], supplied['path']) != supplied['path']:
                raise DriverError('field_not_verified', 'The CT chooser did not retain the validated filename')
            self.ct_input = supplied
            return
        if button_id not in (1, 2) or control['cls'] != 'Button' or row_index is not None or value is not None:
            raise DriverError('unsupported_ct_action', 'Only CT filename, Open and Cancel are supported in this chooser')
        context, _ = self._revalidate(dialog, control)
        label = _label(backend.read_text(context['process'], control['hwnd'])).casefold()
        if label not in (('打开', 'open') if button_id == 1 else ('取消', 'cancel')):
            raise DriverError('control_changed', 'CT chooser button meaning changed')
        if button_id == 1:
            if self.ct_input is None:
                raise DriverError('ct_input_not_verified', 'Supply an existing absolute .ct filename before Open')
            edit = self.driver._control(context, 1148, 'Edit', dialog['hwnd'])
            current = _ct_input(self.ct_input['path'])
            if current != self.ct_input:
                raise DriverError('ct_input_changed', 'The CT input changed after filename validation')
            if backend.read_text(context['process'], edit['hwnd']) != self.ct_input['path']:
                raise DriverError('ct_input_changed', 'The displayed CT filename differs from the validated input')
        backend.post_command(context['process'], control['parent'], button_id, control['hwnd'])
        self.pending = 'ct_return'
        if button_id == 1:
            self.last_action = dict(kind='ct_import', **self.ct_input, command_sent=True, import_effect_verified=False)
        else:
            self.last_action = dict(kind='ct_import_cancel', command_sent=True)
