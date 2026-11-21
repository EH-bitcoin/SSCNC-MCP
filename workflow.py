"""Preparation and inspection operations around the verified simulator driver."""
import hashlib
from pathlib import Path

from driver import Driver, _same_path
from errors import DriverError


MODES = {'edit': (1, 9510), 'mem': (2, 9500), 'reference': (7, 9540)}


def existing_input(filename, extensions):
    path = Path(filename)
    if not path.is_absolute() or path.suffix.lower() not in extensions:
        raise DriverError('invalid_input_path', 'Use an absolute path with a supported extension', extensions=sorted(extensions))
    path = path.resolve()
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise DriverError('invalid_input_file', 'Input must be an existing file of at most 64 MiB')
    return path


class WorkflowDriver(Driver):
    def launch_simulator(self, bring_to_front=True, maximize=False):
        from simulator_launcher import launch_simulator
        return launch_simulator(self, bring_to_front, maximize)

    @staticmethod
    def inspect_program(filename, start_line=1, max_lines=100):
        from file_inspection import inspect_program
        return inspect_program(filename, start_line, max_lines)

    @staticmethod
    def inspect_workpiece(filename):
        from file_inspection import inspect_workpiece
        return inspect_workpiece(filename)

    @staticmethod
    def validate_pocket(filename, output_directory):
        from file_inspection import validate_pocket
        return validate_pocket(filename, output_directory)

    def __init__(self, backend=None, wait_timeout=8.0):
        super().__init__(backend, wait_timeout)
        from preparation import PreparationFlow
        self.preparation = PreparationFlow(self)
        self.monitor = None

    def initialize_controller(self, home_axes=False):
        from initialization import initialize_controller
        with self._lock:
            return initialize_controller(self, home_axes)

    def reset_program(self):
        from initialization import reset_program
        with self._lock:
            return reset_program(self)

    def _require_home_idle(self, context):
        from initialization import read_reference_state
        observed = self.backend.read_with(context['process'], context['base'], read_reference_state)
        if not observed.get('coherent'):
            raise DriverError('reference_state_changing', 'Reference motion state is not stable')
        if observed.get('home_busy') != 0:
            raise DriverError('home_busy', 'Reference motion must stop before changing preparation state')
        return observed

    def completion_monitor(self, action='status'):
        from completion_monitor import CompletionMonitor
        if self.monitor is None:
            self.monitor = CompletionMonitor(self)
        if action not in ('arm', 'status', 'close'):
            raise DriverError('invalid_monitor_action', 'Choose arm, status or close')
        return getattr(self.monitor, action)()

    def capture_screenshot(self, filename, include_owned=True):
        from view_extensions import capture_screenshot
        with self._lock:
            context, state = self._capture()
            if not state['responsive']:
                raise DriverError('unresponsive', 'The simulator does not respond')
            result = capture_screenshot(self.backend, context['process'], context['main']['hwnd'], filename, include_owned)
            result['ok'] = True
            result['status_at_capture'] = state
            return result

    def show_program_page(self, start_line=1, max_lines=80):
        from view_extensions import show_program_page
        with self._lock:
            context, state = self._capture()
            self._guard(context, state)
            return dict(ok=True, **show_program_page(self.backend, context['process'], context['main']['hwnd'], start_line, max_lines))

    def controller_program_page(self, action='open'):
        from view_extensions import controller_program_page
        with self._lock:
            context, state = self._capture()
            self._guard(context, state)
            self._require_home_idle(context)
            return dict(ok=True, **controller_program_page(self.backend, context['process'], context['main']['hwnd'], action))

    def rotate_view(self, dx=0.12, dy=0.08):
        from view_extensions import rotate_view_relative
        with self._lock:
            context, state = self._capture()
            self._guard(context, state)
            return dict(ok=True, **rotate_view_relative(self.backend, context['process'], context['main']['hwnd'], dx, dy))

    def controller_keys(self, keys):
        allowed = {'POS', 'PROG', 'OFS', 'SYS', 'MSG', 'PAGEUP', 'PAGEDOWN', 'UP', 'DOWN', 'LEFT', 'RIGHT',
                   'INPUT', 'CAN', 'DOT', 'MINUS', 'BTNL', 'BTNR', 'BTN1', 'BTN2', 'BTN3', 'BTN4', 'BTN5'}
        allowed.update('0123456789XYZOHDF')
        if not isinstance(keys, list) or not 1 <= len(keys) <= 40 or any(key not in allowed for key in keys):
            raise DriverError('invalid_controller_keys', 'Use up to 40 observed navigation, offset-entry or numeric keys', allowed=sorted(allowed))
        with self._lock:
            origin, _ = self._capture()
            for key in keys:
                context, state = self._refresh(origin)
                self._guard(context, state)
                self._require_home_idle(context)
                panels = self._controller_panels(context)
                if len(panels) != 1:
                    raise DriverError('ambiguous_controller', 'One controller keyboard is required')
                buttons = [row for row in context['windows'] if row['visible'] and row['cls'] == 'Button'
                           and row['parent'] == panels[0]['hwnd'] and
                           self.backend.read_text(context['process'], row['hwnd']).upper() == key]
                if len(buttons) != 1:
                    raise DriverError('controller_key_missing', 'Exactly one observed controller key is required', key=key)
                button = buttons[0]
                self._enabled_chain(context, button)
                self.backend.post_command(context['process'], button['parent'], button['id'], button['hwnd'])
                self.backend.sleep(.08)
            return dict(ok=True, outcome='controller_keys_sent', keys=keys, value_readback_required=True)

    def set_display(self, item, visible):
        commands = {'workpiece': 32798, 'stock': 32791, 'tool': 32795, 'toolpath': 32797, 'coordinates': 32792}
        if item not in commands or not isinstance(visible, bool):
            raise DriverError('invalid_display', 'Choose workpiece, stock, tool, toolpath or coordinates and a boolean')
        with self._lock:
            context, state = self._capture()
            self._guard(context, state)
            current = self.backend.menu_checked(context['process'], context['main']['hwnd'], commands[item])
            if current != visible:
                self.backend.post_command(context['process'], context['main']['hwnd'], commands[item])
            deadline = self.backend.monotonic() + self.wait_timeout
            while self.backend.monotonic() < deadline:
                observed = self.backend.menu_checked(context['process'], context['main']['hwnd'], commands[item])
                if observed == visible:
                    return dict(ok=True, outcome='display_option_verified', item=item, visible=visible, visual_effect_verified=False)
                self.backend.sleep(.1)
            raise DriverError('display_not_verified', 'The display option did not read back')

    def open_setup(self, panel):
        with self._lock:
            return self.preparation.open(panel)

    def setup_action(self, control_id=None, value=None, button_id=None, row_index=None):
        with self._lock:
            return self.preparation.action(control_id, value, button_id, row_index)

    def inspect_setup_dialog(self):
        with self._lock:
            return self.preparation.inspect()

    def set_emergency(self, active):
        return self._set_emergency(active)

    def _set_emergency(self, active, origin=None):
        """Keep compound preparation bound to its original process identity."""
        if not isinstance(active, bool):
            raise DriverError('invalid_emergency', 'active must be a boolean')
        with self._lock:
            context, state = self._capture() if origin is None else self._refresh(origin)
            self._guard(context, state, allow_emergency=True)
            if bool(state['telemetry']['emergency']) == active:
                return dict(ok=True, outcome='already_set', active=active, status=state)
            context, state = self._refresh(context)
            self._guard(context, state, allow_emergency=True)
            if bool(state['telemetry']['emergency']) == active:
                return dict(ok=True, outcome='already_set', active=active, status=state)
            panels = self._panels(context)
            if len(panels) != 1:
                raise DriverError('ambiguous_panel', 'One standard controller panel is required')
            control = self._control(context, 9651, 'Static', panels[0]['root'])
            self.backend.post_command(context['process'], control['parent'], 9651, control['hwnd'])
            deadline = self.backend.monotonic() + self.wait_timeout
            while self.backend.monotonic() < deadline:
                context, state = self._refresh(context)
                if bool(state['telemetry']['emergency']) == active:
                    return dict(ok=True, outcome='emergency_state_verified', active=active, status=state)
                self.backend.sleep(0.1)
            raise DriverError('emergency_not_verified', 'Requested emergency state was not observed')

    def set_mode(self, mode):
        return self._set_mode(mode)

    def _set_mode(self, mode, origin=None):
        """Switch modes without rediscovering a replacement process mid-workflow."""
        if mode not in MODES:
            raise DriverError('invalid_mode', 'Supported modes are edit, mem and reference')
        value, control_id = MODES[mode]
        with self._lock:
            context, state = self._capture() if origin is None else self._refresh(origin)
            self._guard(context, state)
            self._require_home_idle(context)
            if state['telemetry']['mode'] == value:
                return dict(ok=True, outcome='already_in_mode', mode=mode, status=state)
            self._button(context, control_id, 'idle')
            deadline = self.backend.monotonic() + self.wait_timeout
            while self.backend.monotonic() < deadline:
                context, state = self._refresh(context)
                self._guard(context, state)
                if state['telemetry']['mode'] == value:
                    return dict(ok=True, outcome='mode_verified', mode=mode, status=state)
                self.backend.sleep(0.1)
            raise DriverError('mode_not_verified', 'The requested mode was not observed', observed=state['telemetry']['mode'])

    def setup_status(self):
        with self._lock:
            context, state = self._capture()
            raw = state['telemetry']
            if raw is None or not raw['coherent'] or raw['worker'] or raw['cycle']:
                raise DriverError('setup_busy', 'Read setup after the simulator has stopped')
            setup = self.backend.read_setup(context['process'], context['base'])
            from file_state import read_document_flags
            flags = self.backend.read_with(context['process'], context['base'], read_document_flags)
            return dict(ok=True, status=state, setup=setup, document_flags=flags)

    def _dialog_texts(self, context, dialog):
        return [self.backend.read_text(context['process'], row['hwnd'])
                for row in context['windows'] if row['root'] == dialog['hwnd'] and row['visible']
                and row['cls'] in ('Static', 'Button', 'Edit')]

    def _load_file(self, path, kind, discard_unsaved):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self._lock:
            context, state = self._capture()
            self._guard(context, state, allow_emergency=True)
            context, state = self._refresh(context)
            self._guard(context, state, allow_emergency=True)
            self._require_home_idle(context)
            from file_state import read_document_flags
            flags = self.backend.read_with(context['process'], context['base'], read_document_flags)
            if not flags['coherent']:
                raise DriverError('document_state_changing', 'Document modification flags changed before loading')
            if not discard_unsaved and (flags['nc_dirty'] or flags['file_open_will_prompt']):
                raise DriverError('unsaved_document', 'Save current modified data before opening another file',
                                  document_flags=flags, native_prompt_left_open=False,
                                  requires_explicit_save_or_discard=True)
            self.backend.post_command(context['process'], context['main']['hwnd'], 57601)
            deadline = self.backend.monotonic() + max(self.wait_timeout, 15.0)
            submitted = False
            answered = set()
            while self.backend.monotonic() < deadline:
                context, state = self._refresh(context)
                raw = state['telemetry']
                if not state['responsive'] or raw is None or raw['worker'] or raw['cycle']:
                    raise DriverError('state_changed', 'Simulator activity changed during loading')
                if not raw['coherent']:
                    self.backend.sleep(.1)
                    continue
                self._require_home_idle(context)
                dialogs = self._modals(context)
                if not dialogs and submitted and state['main_enabled']:
                    if self.backend.sha256(str(path)) != digest:
                        raise DriverError('input_changed', 'Input changed while it was being loaded')
                    setup = self.backend.read_setup(context['process'], context['base'])
                    if not setup.get('coherent'):
                        self.backend.sleep(.1)
                        continue
                    return self._verify_loaded(path, kind, digest, context, state, setup)
                if len(dialogs) == 1 and dialogs[0]['enabled']:
                    dialog = dialogs[0]
                    if (dialog['owner'] not in self._save_owners(context) or
                            dialog['parent'] != dialog['owner']):
                        raise DriverError('unexpected_dialog', 'The open dialog is not owned by the verified application')
                    try:
                        texts = self._dialog_texts(context, dialog)
                    except DriverError as exc:
                        if exc.code == 'window_changed':
                            self.backend.sleep(.1)
                            continue
                        raise
                    if any('文件已被修改' in text or 'save changes' in text.lower() for text in texts):
                        if dialog['hwnd'] in answered:
                            self.backend.sleep(.1)
                            continue
                        choice = 7 if discard_unsaved else 2
                        choices = [row for row in context['windows'] if row['root'] == dialog['hwnd']
                                   and row['cls'] == 'Button' and row['id'] == choice and row['visible'] and row['enabled']]
                        if not choices and not discard_unsaved:
                            raise DriverError('unsaved_document',
                                              'The native save-changes prompt has no Cancel button; it is left open to preserve edits',
                                              native_prompt_left_open=True, requested_file=str(path),
                                              requires_explicit_save_or_discard=True)
                        button = self._control(context, choice, 'Button', dialog['hwnd'])
                        self.backend.post_command(context['process'], dialog['hwnd'], choice, button['hwnd'])
                        answered.add(dialog['hwnd'])
                        if not discard_unsaved:
                            raise DriverError('unsaved_document', 'Loading cancelled to preserve current edits; save them before loading')
                    else:
                        edits = [row for row in context['windows'] if row['root'] == dialog['hwnd']
                                 and row['cls'] == 'Edit' and row['id'] == 1148 and row['visible']]
                        if edits and not submitted:
                            title = self.backend.read_text(context['process'], dialog['hwnd']).strip()
                            button = self._control(context, 1, 'Button', dialog['hwnd'])
                            label = self.backend.read_text(context['process'], button['hwnd']).replace('&', '').strip()
                            if title not in ('打开', 'Open') or label not in ('打开(O)', '打开', 'Open', 'Open(O)'):
                                raise DriverError('unexpected_dialog', 'Expected the normal file-open dialog', title=title, button=label)
                            edit = self._control(context, 1148, 'Edit', dialog['hwnd'])
                            if self.backend.set_text_readback(context['process'], edit['hwnd'], str(path)) != str(path):
                                raise DriverError('filename_not_verified', 'File dialog did not retain the requested input')
                            self.backend.post_command(context['process'], dialog['hwnd'], 1, button['hwnd'])
                            self._paused = self._run = None
                            submitted = True
                        elif not edits:
                            raise DriverError('unexpected_dialog', 'Loading encountered an unexpected dialog', text=texts)
                elif len(dialogs) > 1:
                    raise DriverError('unexpected_dialog', 'Loading encountered multiple dialogs')
                self.backend.sleep(0.1)
            raise DriverError('load_not_verified', 'Loading did not finish within the bounded wait', submitted=submitted)

    def _verify_loaded(self, path, kind, digest, context, state, setup):
        files = setup.get('files', {})
        observed = files.get(kind)
        if isinstance(observed, dict):
            observed = observed.get('path') or observed.get('value')
        if not isinstance(observed, str) or not _same_path(observed, str(path)):
            raise DriverError('load_not_verified', 'The application file path does not match the requested input', observed=observed)
        result = dict(ok=True, outcome='loaded_path_verified', kind=kind, path=str(path), sha256=digest,
                      setup=setup, status=state)
        if kind == 'program':
            source = self.backend.program_source(context['process'], context['base'])
            if not source.get('coherent'):
                raise DriverError('program_changing', 'Loaded program source changed during readback')
            from file_inspection import inspect_program, _decode_program
            file_text, _ = _decode_program(path.read_bytes())
            def normalize(text):
                return text.replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')
            if normalize(file_text) != normalize(source['text']):
                raise DriverError('program_content_mismatch', 'The live editor content differs from the requested program',
                                  file_sha256=digest, editor_sha256=source.get('sha256'))
            result['loaded_source'] = {key: value for key, value in source.items() if key != 'text'}
            result['file_summary'] = inspect_program(str(path), 1, 1)
            result['source_bytes_match'] = source.get('sha256') == digest
            result['source_content_match'] = True
            result['outcome'] = 'program_content_verified'
        return result

    def load_program(self, filename, discard_unsaved=False):
        path = existing_input(filename, {'.cnc', '.nc', '.txt'})
        return self._load_file(path, 'program', discard_unsaved)

    def open_workpiece(self, filename, discard_unsaved=False):
        path = existing_input(filename, {'.wp'})
        from file_inspection import inspect_workpiece
        inspect_workpiece(str(path))
        return self._load_file(path, 'workpiece', discard_unsaved)

    def read_program(self, start_line=1, max_lines=100):
        if type(start_line) is not int or type(max_lines) is not int or not 1 <= start_line or not 1 <= max_lines <= 300:
            raise DriverError('invalid_page', 'Use start_line >= 1 and max_lines between 1 and 300')
        with self._lock:
            context, state = self._capture()
            if state['telemetry'] is None or not state['telemetry']['coherent']:
                raise DriverError('state_changing', 'Cannot read a coherent program snapshot')
            source = self.backend.program_source(context['process'], context['base'])
            if not source.get('coherent'):
                raise DriverError('program_changing', 'The editor source changed during readback')
            lines = source.pop('text', '').splitlines()
            source['line_count'] = len(lines)
            source['start_line'] = start_line
            page, budget = [], 60000
            for i in range(start_line - 1, min(len(lines), start_line - 1 + max_lines)):
                text = lines[i][:min(4000, budget)]
                if not budget:
                    break
                page.append(dict(number=i + 1, text=text, truncated=len(text) != len(lines[i])))
                budget -= len(text)
            source['lines'] = page
            source['next_line'] = start_line + len(page) if start_line + len(page) <= len(lines) else None
            return dict(ok=True, program=source)

    def inspect_ui(self):
        with self._lock:
            context, state = self._capture()
            controls = []
            for row in context['windows']:
                if row['visible'] and row['cls'] in ('#32770', 'Edit', 'Static', 'Button', 'ComboBox', 'ListBox'):
                    item = dict(row)
                    item['text'] = self.backend.read_text(context['process'], row['hwnd'])
                    controls.append(item)
            return dict(ok=True, status=state, controls=controls,
                        menus=self.backend.menu_items(context['process'], context['main']['hwnd']))
