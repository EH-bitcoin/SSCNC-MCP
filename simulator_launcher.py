"""Open the pinned normal login entry, or bring an existing MF window forward.

No controller buttons, keys, licensing selection, simulator child executable, or
machining command is invoked. Tests can supply ``driver.launcher_system``;
otherwise the Windows boundary below is created using the existing backend.
"""
from __future__ import annotations

import ctypes as c
import ctypes.wintypes as w
import ntpath
import subprocess
from contextlib import contextmanager
from pathlib import Path

from constants import INSTALL_DIRECTORY, MAIN_CLASS, TARGET_PATH
from errors import DriverError


LAUNCHER_PATH = str(Path(INSTALL_DIRECTORY) / "CNCLogin.exe")
LAUNCHER_SHA256 = "2dbc2285b40934141a58ead14516afcb391461ca2e108206e54ad13803161d3d"
MUTEX_NAME = r"Local\SSCNC-MCP-Pinned-CNCLogin-2dbc2285b4093414"


def _same_path(left, right):
    return (isinstance(left, str) and isinstance(right, str)
            and ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right)))


def _same_identity(left, right):
    return (left.get("pid") == right.get("pid")
            and left.get("creation_time") == right.get("creation_time")
            and _same_path(left.get("path"), right.get("path")))


class WindowsLauncherSystem:
    """Small OS boundary; enumerate/read identity, normal Popen, window activation."""

    def __init__(self, backend, *, popen=None):
        self.backend = backend
        self._popen = subprocess.Popen if popen is None else popen
        self._children = []

    @contextmanager
    def exclusive(self):
        # A session-local named mutex also serializes separate MCP server
        # processes. It does not coordinate manual launches by external programs.
        kernel = self.backend.k
        kernel.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
        kernel.CreateMutexW.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.ReleaseMutex.argtypes = [w.HANDLE]
        kernel.ReleaseMutex.restype = w.BOOL
        handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise DriverError("launcher_lock_failed", "Cannot serialize normal launcher requests")
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, 1000)
            if result not in (0, 0x80):
                raise DriverError("launcher_busy", "Another launcher request is in progress", wait_result=int(result))
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)

    def list_processes(self):
        class Entry(c.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", c.c_size_t),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG),
                        ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
        kernel = self.backend.k
        for name in ("Process32FirstW", "Process32NextW"):
            function = getattr(kernel, name)
            function.argtypes = [w.HANDLE, c.POINTER(Entry)]
            function.restype = w.BOOL
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if not handle or handle == c.c_void_p(-1).value:
            raise DriverError("process_inventory", "Cannot enumerate launcher/simulator processes")
        found = []
        try:
            entry = Entry(dwSize=c.sizeof(Entry))
            c.set_last_error(0)
            available = kernel.Process32FirstW(handle, c.byref(entry))
            while available:
                if entry.szExeFile.casefold() in ("cnclogin.exe", "sscnc.exe"):
                    with self.backend._handle(entry.th32ProcessID) as process_handle:
                        identity = self.backend._process_identity(process_handle, entry.th32ProcessID)
                    identity["name"] = entry.szExeFile
                    found.append(identity)
                c.set_last_error(0)
                available = kernel.Process32NextW(handle, c.byref(entry))
            if c.get_last_error() not in (0, 18):  # ERROR_NO_MORE_FILES
                raise DriverError("process_inventory", "Process enumeration did not complete")
        finally:
            kernel.CloseHandle(handle)
        return found

    def file_identity(self, filename):
        path = Path(filename)
        try:
            if not path.is_file():
                return {"exists": False, "path": filename, "sha256": None}
            resolved = str(path.resolve(strict=True))
            return {"exists": True, "path": resolved,
                    "sha256": self.backend.sha256(resolved)}
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, DriverError):
                raise
            raise DriverError("launcher_unreadable", "Cannot inspect the pinned login executable") from exc

    def start_launcher(self, filename, cwd):
        if not _same_path(filename, LAUNCHER_PATH) or not _same_path(cwd, INSTALL_DIRECTORY):
            raise DriverError("launcher_path_mismatch", "Only the pinned normal login executable may be started")
        try:
            child = self._popen([LAUNCHER_PATH], cwd=INSTALL_DIRECTORY,
                                shell=False, close_fds=True)
        except OSError as exc:
            raise DriverError("launcher_start_failed", "Windows could not start the normal login executable") from exc
        self._children = [item for item in self._children if item.poll() is None]
        self._children.append(child)
        with self.backend._handle(child.pid) as handle:
            identity = self.backend._process_identity(handle, child.pid)
        if not _same_path(identity.get("path"), LAUNCHER_PATH):
            raise DriverError("launcher_identity_mismatch", "Created process does not match the pinned login executable", pid=child.pid)
        exit_code = child.poll()
        if exit_code is not None:
            raise DriverError("launcher_exited", "Login process exited before verification", pid=child.pid, exit_code=exit_code)
        return identity

    def _verify_main(self, process, hwnd):
        self.backend._verify(process)
        if not isinstance(hwnd, int) or isinstance(hwnd, bool) or hwnd <= 0 or not self.backend.u.IsWindow(hwnd):
            raise DriverError("window_changed", "The simulator main window is unavailable")
        row = self.backend._window(hwnd)
        if row["pid"] != process["pid"] or row["cls"] != MAIN_CLASS or row["root"] != hwnd:
            raise DriverError("window_changed", "The main HWND no longer belongs to the verified simulator")

    def _foreground_belongs(self, process, main, foreground):
        user = self.backend.u
        if not foreground or not user.IsWindow(foreground):
            return False
        seen, cursor = set(), foreground
        for _ in range(16):
            if not cursor or cursor in seen or not user.IsWindow(cursor):
                return False
            seen.add(cursor)
            row = self.backend._window(cursor)
            if row["pid"] != process["pid"]:
                return False
            if cursor == main:
                return True
            # Foreground is a top-level window. Ownership, not title or common
            # process ID alone, ties a dialog to this exact main window.
            cursor = int(user.GetWindow(cursor, 4) or 0)  # GW_OWNER
        return False

    def bring_to_front(self, process, main_hwnd, maximize=False):
        if not isinstance(maximize, bool):
            raise DriverError("invalid_maximize_option", "maximize must be a boolean")
        user = self.backend.u
        for name, arguments, returns in (
            ("IsIconic", [w.HWND], w.BOOL),
            ("IsZoomed", [w.HWND], w.BOOL),
            ("IsWindowVisible", [w.HWND], w.BOOL),
            ("ShowWindowAsync", [w.HWND, c.c_int], w.BOOL),
            ("SetForegroundWindow", [w.HWND], w.BOOL),
            ("GetForegroundWindow", [], w.HWND),
        ):
            function = getattr(user, name)
            function.argtypes, function.restype = arguments, returns
        self._verify_main(process, main_hwnd)
        restore_requested = False
        maximize_accepted = None
        if maximize:
            if not user.IsZoomed(main_hwnd) or user.IsIconic(main_hwnd):
                self._verify_main(process, main_hwnd)
                maximize_accepted = bool(user.ShowWindowAsync(main_hwnd, 3))  # SW_MAXIMIZE
        elif user.IsIconic(main_hwnd):
            self._verify_main(process, main_hwnd)
            restore_requested = bool(user.ShowWindowAsync(main_hwnd, 9))  # SW_RESTORE
        self._verify_main(process, main_hwnd)
        accepted = bool(user.SetForegroundWindow(main_hwnd))
        deadline = self.backend.monotonic() + 1.0
        while True:
            self._verify_main(process, main_hwnd)
            foreground = int(user.GetForegroundWindow() or 0)
            verified = (not user.IsIconic(main_hwnd) and bool(user.IsWindowVisible(main_hwnd))
                        and self._foreground_belongs(process, main_hwnd, foreground))
            maximized = (bool(user.IsZoomed(main_hwnd)) and not user.IsIconic(main_hwnd)
                         and bool(user.IsWindowVisible(main_hwnd))) if maximize else None
            if (verified and (not maximize or maximized)) or self.backend.monotonic() >= deadline:
                self._verify_main(process, main_hwnd)
                return {"foreground_verified": bool(verified), "foreground_hwnd": foreground,
                        "restore_requested": restore_requested,
                        "maximize_requested": maximize,
                        "maximize_request_accepted": maximize_accepted,
                        "maximized_verified": maximized,
                        "foreground_request_accepted": accepted,
                        "verification": "Foreground must be this main HWND or a same-process owned window; an explicit maximize request also requires IsZoomed readback. No keys or clicks are sent."}
            self.backend.sleep(0.05)


def _classify(processes):
    target, launcher = [], []
    for process in processes:
        path = process.get("path")
        name = process.get("name", ntpath.basename(path) if isinstance(path, str) else "").casefold()
        if name not in ("sscnc.exe", "cnclogin.exe"):
            continue
        if (not isinstance(process.get("pid"), int) or process["pid"] <= 0
                or not isinstance(process.get("creation_time"), int) or process["creation_time"] <= 0):
            raise DriverError("process_identity", "A related process has incomplete identity")
        expected = TARGET_PATH if name == "sscnc.exe" else LAUNCHER_PATH
        if not _same_path(path, expected):
            raise DriverError("conflicting_instance", "A different simulator or login executable is running; no new instance is started", process=process)
        (target if name == "sscnc.exe" else launcher).append(process)
    if len(target) > 1 or len(launcher) > 1:
        raise DriverError("ambiguous_process", "Multiple simulator or login instances are running", simulator_pids=[p["pid"] for p in target], launcher_pids=[p["pid"] for p in launcher])
    return target[0] if target else None, launcher[0] if launcher else None


def _verify_launcher_file(system):
    info = system.file_identity(LAUNCHER_PATH)
    if not info.get("exists"):
        raise DriverError("launcher_missing", "The pinned CNCLogin.exe is not installed", path=LAUNCHER_PATH)
    if not _same_path(info.get("path"), LAUNCHER_PATH):
        raise DriverError("launcher_path_mismatch", "The login executable resolves outside its pinned path")
    if str(info.get("sha256", "")).lower() != LAUNCHER_SHA256:
        raise DriverError("launcher_hash_mismatch", "The login executable differs from the verified local binary", expected=LAUNCHER_SHA256)


def _login_required(instance, *, started):
    return {"ok": True, "outcome": "login_required", "launcher_started": started,
            "launcher": dict(instance, sha256=LAUNCHER_SHA256),
            "login_page_verified": False, "foreground_requested": False,
            "foreground_verified": None, "maximize_requested": False,
            "maximized_verified": None, "machining_started": False,
            "next_action": "Complete normal login in CNCLogin.exe, then retry this tool. Authorization options and Run are not selected automatically.",
            "verification": "The pinned login process is present; its visible login page and successful authorization have not been verified."}


def _existing(driver, system, processes, bring_to_front, maximize):
    target, launcher = _classify(processes)
    if target:
        status = driver.status()  # Reuses the target's path/hash/window checks.
        if not status.get("ok"):
            return {"ok": False, "outcome": "existing_instance_unverified", "launcher_started": False,
                    "status": status, "foreground_requested": bring_to_front,
                    "foreground_verified": False if bring_to_front else None,
                    "maximize_requested": maximize,
                    "maximized_verified": False if maximize else None, "machining_started": False}
        if not _same_identity(target, status.get("target", {})):
            raise DriverError("process_changed", "Simulator identity changed while checking its existing instance")
        result = {"ok": True, "outcome": "already_running", "launcher_started": False,
                  "status": status, "foreground_requested": bring_to_front,
                  "foreground_verified": None, "maximize_requested": maximize,
                  "maximized_verified": None, "machining_started": False}
        if bring_to_front:
            foreground = system.bring_to_front(target, status["target"].get("main_hwnd"), maximize=maximize)
            result["foreground"] = foreground
            result["foreground_verified"] = foreground["foreground_verified"] is True
            if maximize:
                result["maximized_verified"] = foreground.get("maximized_verified") is True
        return result
    if launcher:
        _verify_launcher_file(system)
        return _login_required(launcher, started=False)
    return None


def launch_simulator(driver, bring_to_front=True, maximize=False) -> dict:
    """Reuse MF, reuse normal login, or start only the pinned normal login entry.

    ``driver.launcher_system`` may replace the OS boundary for offline tests.
    Windows foreground policy is respected; an unsuccessful request returns
    foreground_verified=False. Login process presence is not a page/login proof.
    ``maximize=True`` requires ``bring_to_front=True`` and affects only an
    already running, verified simulator main window, never the login entry.
    """
    if not isinstance(bring_to_front, bool):
        raise DriverError("invalid_foreground_option", "bring_to_front must be a boolean")
    if not isinstance(maximize, bool) or (maximize and not bring_to_front):
        raise DriverError("invalid_maximize_option", "maximize must be a boolean and requires bring_to_front=True")
    with driver._lock:
        system = getattr(driver, "launcher_system", None)
        if system is None:
            system = driver.launcher_system = WindowsLauncherSystem(driver.backend)
        with system.exclusive():
            existing = _existing(driver, system, system.list_processes(), bring_to_front, maximize)
            if existing is not None:
                return existing
            _verify_launcher_file(system)
            # Check again after hashing; another ordinary launch may have appeared.
            existing = _existing(driver, system, system.list_processes(), bring_to_front, maximize)
            if existing is not None:
                return existing
            instance = system.start_launcher(LAUNCHER_PATH, INSTALL_DIRECTORY)
            target, launcher = _classify([instance])
            if target or not launcher:
                raise DriverError("launcher_identity_mismatch", "Created process is not the pinned login executable")
            return _login_required(launcher, started=True)
