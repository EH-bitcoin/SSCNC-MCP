"""Small Windows boundary: process reads and normal UI messages only.

No WriteProcessMemory, injected code, exported target functions, or licensing calls.
The caller serializes operations; every message rechecks the process/window identity.
"""
import ctypes as c
import ctypes.wintypes as w
import hashlib
import ntpath
import os
import time
from contextlib import contextmanager

from constants import TARGET_PATH
from errors import DriverError
from memory import decode_snapshot


def canonical(path):
    return ntpath.normcase(ntpath.normpath(path))


def select_module_base(candidates):
    # SNAPMODULE|SNAPMODULE32 can repeat the same WOW64 executable mapping.
    # Only identical mappings are collapsed; distinct addresses remain ambiguous.
    matches = {entry["base"] for entry in candidates if canonical(entry["path"]) == canonical(TARGET_PATH)}
    if len(matches) != 1 or not next(iter(matches), 0):
        raise DriverError("module_ambiguous", "Cannot identify exactly one pinned executable module", candidates=candidates)
    return next(iter(matches))


class WindowsBackend:
    def __init__(self):
        if os.name != "nt":
            raise DriverError("unsupported_platform", "This server requires an interactive Windows session")
        self.u = c.WinDLL("user32", use_last_error=True)
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.CB = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        self._declare()

    def _declare(self):
        def signature(lib, name, arguments, returns):
            function = getattr(lib, name)
            function.argtypes, function.restype = arguments, returns
        k, u = self.k, self.u
        signature(k, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
        signature(k, "CloseHandle", [w.HANDLE], w.BOOL)
        signature(k, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)], w.BOOL)
        signature(k, "GetProcessTimes", [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4, w.BOOL)
        signature(k, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)
        signature(k, "ReadProcessMemory", [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)], w.BOOL)
        signature(u, "EnumWindows", [self.CB, w.LPARAM], w.BOOL)
        signature(u, "EnumChildWindows", [w.HWND, self.CB, w.LPARAM], w.BOOL)
        signature(u, "GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD)
        signature(u, "GetClassNameW", [w.HWND, w.LPWSTR, c.c_int], c.c_int)
        signature(u, "GetDlgCtrlID", [w.HWND], c.c_int)
        signature(u, "GetParent", [w.HWND], w.HWND)
        signature(u, "GetAncestor", [w.HWND, w.UINT], w.HWND)
        signature(u, "GetWindow", [w.HWND, w.UINT], w.HWND)
        signature(u, "IsWindow", [w.HWND], w.BOOL)
        signature(u, "IsWindowVisible", [w.HWND], w.BOOL)
        signature(u, "IsWindowEnabled", [w.HWND], w.BOOL)
        signature(u, "GetWindowLongW", [w.HWND, c.c_int], c.c_long)
        signature(u, "PostMessageW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL)
        signature(u, "SendMessageTimeoutW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, c.POINTER(c.c_size_t)], w.LPARAM)

    def _fail(self, code, message):
        raise DriverError(code, message, winerror=c.get_last_error())

    @contextmanager
    def _handle(self, pid, access=0x1000):
        handle = self.k.OpenProcess(access, False, pid)
        if not handle: self._fail("process_access", "Cannot inspect the selected simulator process")
        try: yield handle
        finally: self.k.CloseHandle(handle)

    def _process_identity(self, handle, pid):
        buf, size = c.create_unicode_buffer(32768), w.DWORD(32768)
        if not self.k.QueryFullProcessImageNameW(handle, 0, buf, c.byref(size)):
            self._fail("process_identity", "Cannot read process executable path")
        times = [w.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *[c.byref(t) for t in times]):
            self._fail("process_identity", "Cannot read process creation time")
        birth = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return dict(pid=pid, path=buf.value, creation_time=birth)

    def _verify(self, process, handle=None):
        if handle is None:
            with self._handle(process["pid"]) as opened: return self._verify(process, opened)
        current = self._process_identity(handle, process["pid"])
        if canonical(current["path"]) != canonical(TARGET_PATH) or current["creation_time"] != process["creation_time"]:
            raise DriverError("process_changed", "Simulator identity changed; no command was sent")

    def list_processes(self):
        class Entry(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                        ("th32DefaultHeapID", c.c_size_t), ("th32ModuleID", w.DWORD),
                        ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                        ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
        self.k.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(Entry)]
        self.k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
        handle = self.k.CreateToolhelp32Snapshot(2, 0)
        if handle == c.c_void_p(-1).value: self._fail("process_inventory", "Cannot enumerate processes")
        found = []
        try:
            entry = Entry(dwSize=c.sizeof(Entry))
            ok = self.k.Process32FirstW(handle, c.byref(entry))
            while ok:
                if entry.szExeFile.casefold() == "sscnc.exe":
                    with self._handle(entry.th32ProcessID) as process_handle:
                        found.append(self._process_identity(process_handle, entry.th32ProcessID))
                ok = self.k.Process32NextW(handle, c.byref(entry))
        finally: self.k.CloseHandle(handle)
        return found

    @staticmethod
    def sha256(path):
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
            return digest.hexdigest()
        except OSError as exc:
            raise DriverError("binary_unreadable", "Cannot hash the pinned simulator binary", reason=str(exc)) from exc

    def module_base(self, process):
        self._verify(process)
        class Module(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                        ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD),
                        ("modBaseAddr", c.c_void_p), ("modBaseSize", w.DWORD), ("hModule", w.HMODULE),
                        ("szModule", w.WCHAR * 256), ("szExePath", w.WCHAR * 260)]
        self.k.Module32FirstW.argtypes = [w.HANDLE, c.POINTER(Module)]
        self.k.Module32NextW.argtypes = [w.HANDLE, c.POINTER(Module)]
        handle = self.k.CreateToolhelp32Snapshot(0x18, process["pid"])
        if handle == c.c_void_p(-1).value: self._fail("module_inventory", "Cannot enumerate simulator modules")
        candidates = []
        try:
            entry = Module(dwSize=c.sizeof(Module))
            ok = self.k.Module32FirstW(handle, c.byref(entry))
            while ok:
                if entry.szModule.casefold() == "sscnc.exe":
                    candidates.append(dict(path=entry.szExePath, base=entry.modBaseAddr or 0))
                ok = self.k.Module32NextW(handle, c.byref(entry))
        finally: self.k.CloseHandle(handle)
        return select_module_base(candidates)

    def _window(self, hwnd):
        pid, cls = w.DWORD(), c.create_unicode_buffer(256)
        self.u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        self.u.GetClassNameW(hwnd, cls, len(cls))
        return dict(hwnd=int(hwnd), pid=pid.value, cls=cls.value, id=self.u.GetDlgCtrlID(hwnd),
                    parent=int(self.u.GetParent(hwnd) or 0), root=int(self.u.GetAncestor(hwnd, 2) or 0),
                    owner=int(self.u.GetWindow(hwnd, 4) or 0),
                    visible=bool(self.u.IsWindowVisible(hwnd)), enabled=bool(self.u.IsWindowEnabled(hwnd)))

    def windows(self, process):
        self._verify(process)
        rows, tops = {}, []
        @self.CB
        def collect_top(hwnd, _):
            row = self._window(hwnd)
            if row["pid"] == process["pid"]: rows[row["hwnd"]] = row; tops.append(hwnd)
            return True
        @self.CB
        def collect_child(hwnd, _):
            row = self._window(hwnd)
            if row["pid"] == process["pid"]: rows[row["hwnd"]] = row
            return True
        if not self.u.EnumWindows(collect_top, 0): self._fail("window_inventory", "Cannot enumerate top windows")
        for top in tops: self.u.EnumChildWindows(top, collect_child, 0)
        return list(rows.values())

    def _send(self, hwnd, message, wp=0, lp=0, timeout=500):
        result = c.c_size_t()
        c.set_last_error(0)
        if not self.u.SendMessageTimeoutW(hwnd, message, wp, lp, 3, timeout, c.byref(result)):
            self._fail("message_timeout", "Window did not answer within the bounded timeout")
        return result.value

    def probe(self, hwnd):
        try: self._send(hwnd, 0); return True
        except DriverError: return False

    def read_state(self, process, base):
        return self.read_with(process, base, decode_snapshot)

    def read_with(self, process, base, decoder):
        with self._handle(process["pid"], 0x1000 | 0x10) as handle:
            self._verify(process, handle)
            def read(address, size):
                buffer, read_size = c.create_string_buffer(size), c.c_size_t()
                if not self.k.ReadProcessMemory(handle, address, buffer, size, c.byref(read_size)) or read_size.value != size:
                    self._fail("memory_read_failed", "Read-only simulator telemetry could not be read")
                return buffer.raw
            return decoder(read, base)

    def read_setup(self, process, base):
        from read_setup import read_setup
        return self.read_with(process, base, read_setup)

    def program_source(self, process, base):
        from read_setup import read_program_source
        return self.read_with(process, base, read_program_source)

    def read_text(self, process, hwnd, limit=32768):
        self._verify(process)
        row = self._window(hwnd)
        if row['pid'] != process['pid'] or not self.u.IsWindow(hwnd):
            raise DriverError('window_changed', 'Text source changed identity')
        size = self._send(hwnd, 14)
        if size > limit:
            raise DriverError('text_too_large', 'Control text exceeds the bounded limit')
        output = c.create_unicode_buffer(size + 1)
        self._send(hwnd, 13, len(output), c.cast(output, c.c_void_p).value)
        return output.value

    def set_field(self, process, hwnd, value):
        self._check_window(process, hwnd, 'Edit')
        data = c.create_unicode_buffer(str(value))
        if not self._send(hwnd, 12, 0, c.cast(data, c.c_void_p).value):
            raise DriverError('field_rejected', 'The setup field rejected the value')
        return self.read_text(process, hwnd)

    def menu_items(self, process, hwnd):
        self._verify(process)
        self.u.GetMenu.argtypes, self.u.GetMenu.restype = [w.HWND], w.HMENU
        self.u.GetSubMenu.argtypes, self.u.GetSubMenu.restype = [w.HMENU, c.c_int], w.HMENU
        self.u.GetMenuItemCount.argtypes = [w.HMENU]
        self.u.GetMenuItemID.argtypes, self.u.GetMenuItemID.restype = [w.HMENU, c.c_int], w.UINT
        self.u.GetMenuStringW.argtypes = [w.HMENU, w.UINT, w.LPWSTR, c.c_int, w.UINT]
        items = []
        def walk(menu, parent, depth=0):
            if not menu or depth > 4:
                return
            for index in range(min(self.u.GetMenuItemCount(menu), 100)):
                label = c.create_unicode_buffer(512)
                self.u.GetMenuStringW(menu, index, label, len(label), 0x400)
                path = parent + [label.value]
                command = self.u.GetMenuItemID(menu, index)
                if command not in (0, 0xFFFFFFFF):
                    items.append(dict(command=command, path=path))
                walk(self.u.GetSubMenu(menu, index), path, depth + 1)
        walk(self.u.GetMenu(hwnd), [])
        return items

    def menu_checked(self, process, hwnd, command):
        self._check_window(process, hwnd)
        self.menu_items(process, hwnd)  # Establish menu API signatures and verify ownership.
        self.u.GetMenu.argtypes, self.u.GetMenu.restype = [w.HWND], w.HMENU
        self.u.GetMenuState.argtypes, self.u.GetMenuState.restype = [w.HMENU, w.UINT, w.UINT], w.UINT
        root = self.u.GetMenu(hwnd)
        def parent_menu(menu, popup_index=0, depth=0):
            if not menu or depth > 4:
                return None
            for index in range(min(self.u.GetMenuItemCount(menu), 100)):
                if self.u.GetMenuItemID(menu, index) == command:
                    return menu, popup_index
                child = self.u.GetSubMenu(menu, index)
                match = parent_menu(child, index, depth + 1) if child else None
                if match:
                    return match
            return None
        match = parent_menu(root)
        if match is None:
            raise DriverError('menu_missing', 'The display menu item is unavailable')
        self._check_window(process, hwnd)
        # MFC lazily refreshes CCmdUI checkmarks when the popup is initialized.
        # This sends no selected command and never displays or clicks a menu.
        self._send(hwnd, 0x117, match[0], match[1])  # WM_INITMENUPOPUP
        self._check_window(process, hwnd)
        value = self.u.GetMenuState(root, command, 0)
        if value == 0xFFFFFFFF:
            raise DriverError('menu_missing', 'The display menu item is unavailable')
        return bool(value & 8)

    def combo_options(self, process, hwnd):
        self._check_window(process, hwnd, 'ComboBox')
        count = self._send(hwnd, 0x146)
        if count > 500:
            raise DriverError('invalid_combo', 'Unexpected number of options')
        values = []
        for index in range(count):
            size = self._send(hwnd, 0x149, index)
            if size > 4096:
                raise DriverError('invalid_combo', 'Option text exceeds the limit')
            data = c.create_unicode_buffer(size + 1)
            self._send(hwnd, 0x148, index, c.cast(data, c.c_void_p).value)
            values.append(data.value)
        return values

    def select_combo(self, process, hwnd, value):
        row = self._check_window(process, hwnd, 'ComboBox')
        parent_identity = self._check_window(process, row['parent'])
        options = self.combo_options(process, hwnd)
        if options.count(value) != 1:
            raise DriverError('unknown_option', 'Choose an exact observed option', options=options)
        def revalidate():
            fresh = self._check_window(process, hwnd, 'ComboBox')
            if fresh != row:
                raise DriverError('window_changed', 'Combo identity or state changed before dispatch')
            parent = self._check_window(process, fresh['parent'])
            if parent != parent_identity or parent['pid'] != fresh['pid'] or parent['root'] != fresh['root']:
                raise DriverError('window_changed', 'Combo parent no longer belongs to its verified dialog')
            return fresh
        index = options.index(value)
        revalidate()
        self._send(hwnd, 0x14E, index)
        if self._send(hwnd, 0x147) != index:
            raise DriverError('selection_not_verified', 'Combo selection did not read back')
        fresh = revalidate()
        if not self.u.PostMessageW(fresh['parent'], 0x111, fresh['id'] | (1 << 16), hwnd):
            self._fail('command_rejected', 'Windows rejected the combo selection notification')

    def select_list_row(self, process, hwnd, index):
        row = self._check_window(process, hwnd, 'SysListView32')
        parent_identity = self._check_window(process, row['parent'])
        def revalidate():
            fresh = self._check_window(process, hwnd, 'SysListView32')
            if fresh != row:
                raise DriverError('window_changed', 'List identity or state changed before dispatch')
            parent = self._check_window(process, fresh['parent'])
            if parent != parent_identity or parent['pid'] != fresh['pid'] or parent['root'] != fresh['root']:
                raise DriverError('window_changed', 'List parent no longer belongs to its verified dialog')
        def send(message, wp=0, lp=0):
            revalidate()
            # _send reports bounded delivery failures; keyboard messages may
            # legitimately return zero, so verify the final selected row.
            return self._send(hwnd, message, wp, lp)
        count = send(0x1004)
        if not isinstance(index, int) or not 0 <= index < min(count, 1000):
            raise DriverError('invalid_row', 'Row index is outside the observed list')
        send(0x100, 0x24, 0)
        send(0x101, 0x24, 0)
        for _ in range(index):
            send(0x100, 0x28, 0)
            send(0x101, 0x28, 0)
        selected = send(0x100C, -1, 2)
        revalidate()
        if selected != index:
            raise DriverError('selection_not_verified', 'List selection did not read back', observed=selected)

    def _check_window(self, process, hwnd, expected_class=None):
        self._verify(process)
        row = self._window(hwnd)
        if not self.u.IsWindow(hwnd) or row["pid"] != process["pid"]:
            raise DriverError("window_changed", "The command target no longer belongs to the pinned process")
        if expected_class and row["cls"] != expected_class:
            raise DriverError("window_changed", "Unexpected control class")
        # GetAncestor(GA_ROOT) excludes the owner. A modal's disabled owner is normal;
        # a disabled child ancestor inside the same root must still block dispatch.
        cursor, seen = hwnd, set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            if not self.u.IsWindowEnabled(cursor) or not self.u.IsWindowVisible(cursor):
                raise DriverError("control_disabled", "Target or its actual top parent is disabled/hidden")
            if cursor == row["root"]: break
            cursor = self.u.GetParent(cursor)
        if cursor != row["root"]:
            raise DriverError("window_changed", "Cannot verify the target ancestor chain")
        return row

    def post_command(self, process, hwnd, command, control=0):
        self._check_window(process, hwnd)
        if control:
            child = self._check_window(process, control)
            if child["parent"] != hwnd or child["id"] != command:
                raise DriverError("window_changed", "Button ID or direct parent changed")
        if not self.u.PostMessageW(hwnd, 0x111, command, control):
            self._fail("command_rejected", "Windows rejected WM_COMMAND")

    def select_workpiece(self, process, hwnd):
        row = self._check_window(process, hwnd, "ListBox")
        if row["id"] != 1067: raise DriverError("wrong_control", "Expected workpiece selection list 1067")
        count = self._send(hwnd, 0x18B)
        if not 0 < count < 100: raise DriverError("invalid_selection_list", "Unexpected item count")
        matches = [i for i in range(count) if self._send(hwnd, 0x199, i) == 1]
        if len(matches) != 1:
            raise DriverError("ambiguous_workpiece_item", "Workpiece item data 1 is not unique")
        index = matches[0]
        if self.u.GetWindowLongW(hwnd, -16) & (8 | 0x800):
            self._send(hwnd, 0x185, 0, -1)
            self._send(hwnd, 0x185, 1, index)
            selected = self._send(hwnd, 0x187, index) == 1
        else:
            self._send(hwnd, 0x186, index)
            selected = self._send(hwnd, 0x188) == index
        if not selected: raise DriverError("selection_not_confirmed", "List selection did not read back")
        return dict(index=index, item_data=1)

    def read_workpiece_selection(self, process, hwnd):
        """Revalidate selection without changing the ListBox or sending clicks."""
        row = self._check_window(process, hwnd, "ListBox")
        if row["id"] != 1067: raise DriverError("wrong_control", "Expected workpiece selection list 1067")
        count = self._send(hwnd, 0x18B)
        if not 0 < count < 100: raise DriverError("invalid_selection_list", "Unexpected item count")
        if self.u.GetWindowLongW(hwnd, -16) & (8 | 0x800):
            indices = [index for index in range(count) if self._send(hwnd, 0x187, index) == 1]
        else:
            selected = self._send(hwnd, 0x188)
            indices = [selected] if 0 <= selected < count else []
        if len(indices) != 1 or self._send(hwnd, 0x199, indices[0]) != 1:
            raise DriverError("selection_changed", "Exactly one workpiece item with data 1 must remain selected")
        return dict(index=indices[0], item_data=1)

    def set_text_readback(self, process, hwnd, text):
        row = self._check_window(process, hwnd, "Edit")
        if row["id"] != 1148: raise DriverError("wrong_control", "Expected filename Edit 1148")
        data = c.create_unicode_buffer(text)
        if not self._send(hwnd, 12, 0, c.cast(data, c.c_void_p).value):
            raise DriverError("filename_rejected", "WM_SETTEXT did not accept the filename")
        output = c.create_unicode_buffer(4096)
        self._send(hwnd, 13, len(output), c.cast(output, c.c_void_p).value)
        return output.value

    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)
