"""Real desktop capture and bounded native G-code list pagination.

The driver must serialize these helpers and apply its stopped/no-modal guard before
show_program_page. Capture reads the displayed screen; it never restores, raises,
redraws, stitches or annotates a simulator window. No target memory is written.
"""
import ctypes as c
import ctypes.wintypes as w
import hashlib
import math
import ntpath
import os
import struct
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from constants import MAIN_CLASS, TARGET_PATH, TARGET_SHA256, VIEWS, WORKSPACE
from errors import DriverError

MAX_PIXELS = 32_000_000


def _verify_target(backend, process):
    if ntpath.normcase(ntpath.normpath(process.get("path", ""))) != ntpath.normcase(TARGET_PATH):
        raise DriverError("wrong_executable", "Capture requires the pinned FANUC0iMF executable")
    backend._verify(process)
    if backend.sha256(TARGET_PATH).lower() != TARGET_SHA256:
        raise DriverError("binary_hash_mismatch", "Executable differs from the supported MF binary")


def _png_path(filename):
    if not isinstance(filename, str) or not filename or filename.startswith(("\\\\", "//")):
        raise DriverError("invalid_output_path", "Use a local absolute PNG path")
    path = Path(filename)
    if not path.is_absolute() or path.suffix.lower() != ".png":
        raise DriverError("invalid_output_path", "Use an absolute .png filename")
    if any(":" in part or part.endswith((" ", ".")) for part in path.parts[1:]):
        raise DriverError("invalid_output_path", "Alternate streams and ambiguous Windows names are rejected")
    if path.is_reserved():
        raise DriverError("invalid_output_path", "Reserved device names are rejected")
    try:
        root = Path(WORKSPACE).resolve(strict=True)
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise DriverError("invalid_output_path", "The output parent must already exist", reason=str(exc)) from exc
    resolved = parent / path.name
    if not resolved.is_relative_to(root):
        raise DriverError("path_outside_workspace", "Screenshots must stay inside the configured workspace")
    if resolved.exists():
        raise DriverError("output_exists", "Screenshot output already exists; it will not be overwritten")
    return resolved


def _intersection(a, b):
    out = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return out if out[0] < out[2] and out[1] < out[3] else None


def _subtract(rect, cover):
    overlap = _intersection(rect, cover)
    if overlap is None: return [rect]
    left, top, right, bottom = rect
    x1, y1, x2, y2 = overlap
    return [r for r in ((left, top, right, y1), (left, y2, right, bottom),
                        (left, y1, x1, y2), (x2, y1, right, y2))
            if r[0] < r[2] and r[1] < r[3]]


def _owned_by(row, main_hwnd, rows):
    current, seen = row["hwnd"], set()
    while current and current not in seen:
        if current == main_hwnd: return True
        seen.add(current)
        current = rows.get(current, {}).get("owner", 0)
    return False


def _maximized_capture_rect(row, monitors):
    """Clip verified maximized nonclient borders, never offscreen client pixels."""
    raw = tuple(row["rect"])
    monitor = row.get("monitor_rect")
    if not row.get("zoomed") or not monitor:
        return raw
    monitor = tuple(monitor)
    clipped = _intersection(raw, monitor)
    if clipped == raw:
        return raw
    client, margin = row.get("client_screen_rect"), row.get("max_frame_margin")
    if (monitor not in {tuple(rect) for rect in monitors} or clipped is None or
            not client or _intersection(tuple(client), monitor) != tuple(client) or
            _intersection(tuple(client), raw) != tuple(client) or
            not margin or len(margin) != 2 or
            any(type(value) is not int or not 0 < value <= 128 for value in margin)):
        raise DriverError("capture_offscreen", "Maximized border clipping lacks monitor/client/frame evidence")
    overflow = (max(0, monitor[0]-raw[0]), max(0, monitor[1]-raw[1]),
                max(0, raw[2]-monitor[2]), max(0, raw[3]-monitor[3]))
    if any(value > limit for value, limit in zip(overflow, (margin[0], margin[1], margin[0], margin[1]))):
        raise DriverError("capture_offscreen", "Maximized overflow exceeds the current system nonclient frame")
    return clipped


def _capture_plan(scene, pid, main_hwnd, include_owned):
    rows = [dict(row) for row in scene["windows"]]  # native Z order, foreground first
    by_hwnd = {row["hwnd"]: row for row in rows}
    main = by_hwnd.get(main_hwnd)
    if not main or main["pid"] != pid or main["cls"] != MAIN_CLASS:
        raise DriverError("window_changed", "Expected simulator main window is absent or changed")
    if main.get("iconic"):
        raise DriverError("window_minimized", "The simulator is minimized; no automatic restore was attempted")
    if not main.get("visible") or main.get("cloaked"):
        raise DriverError("window_hidden", "The simulator is hidden or on an unavailable desktop")
    selected = [row for row in rows if row["pid"] == pid and row.get("visible") and
                not row.get("iconic") and not row.get("cloaked") and
                (row["hwnd"] == main_hwnd or (include_owned and _owned_by(row, main_hwnd, by_hwnd)))]
    window_rectangles = []
    for row in selected:
        rect = row["rect"]
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise DriverError("capture_invalid_rect", "Selected window has no drawable screen rectangle")
        # Layered/transparent selected windows could reveal other applications.
        if row.get("exstyle", 0) & (0x80000 | 0x20):
            raise DriverError("capture_transparent_window", "Transparent/layered window coverage is not verified")
        capture_rect = _maximized_capture_rect(row, scene["monitors"])
        window_rectangles.append(dict(hwnd=row["hwnd"], raw_rect=list(rect),
                                      capture_rect=list(capture_rect), maximized=bool(row.get("zoomed")),
                                      maximized_frame_clipped=tuple(rect) != capture_rect))
        row["rect"] = capture_rect
    raw_bounds = (min(row["raw_rect"][0] for row in window_rectangles),
                  min(row["raw_rect"][1] for row in window_rectangles),
                  max(row["raw_rect"][2] for row in window_rectangles),
                  max(row["raw_rect"][3] for row in window_rectangles))
    bounds = (min(row["rect"][0] for row in selected), min(row["rect"][1] for row in selected),
              max(row["rect"][2] for row in selected), max(row["rect"][3] for row in selected))
    if (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) > MAX_PIXELS:
        raise DriverError("capture_too_large", "Requested screen rectangle exceeds the pixel bound")
    uncovered = [bounds]
    for monitor in scene["monitors"]:
        uncovered = [part for rect in uncovered for part in _subtract(rect, monitor)]
    if uncovered:
        raise DriverError("capture_offscreen", "Selected windows are partly offscreen or span a monitor gap")
    allowed = {row["hwnd"] for row in selected}
    remaining, coverage = [bounds], []
    for row in rows:
        if not row.get("visible") or row.get("iconic") or row.get("cloaked"): continue
        intersections = [part for rect in remaining if (part := _intersection(rect, row["rect"]))]
        if not intersections: continue
        if row["hwnd"] not in allowed:
            raise DriverError("capture_occluded", "Another window covers the requested screenshot",
                              hwnd=row["hwnd"], pid=row["pid"], rectangles=intersections)
        coverage.append((row["hwnd"], tuple(row["rect"]), row.get("owner", 0)))
        remaining = [part for rect in remaining for part in _subtract(rect, row["rect"])]
        if not remaining: break
    if remaining:
        raise DriverError("capture_background_gap", "The rectangular capture would include non-application desktop gaps")
    return dict(rect=bounds, raw_rect=raw_bounds, hwnds=sorted(allowed), coverage=coverage,
                window_rectangles=window_rectangles)


def _png_bytes(width, height, bgrx):
    if len(bgrx) != width * height * 4:
        raise DriverError("capture_pixel_buffer", "Screen pixel buffer size is inconsistent")
    raw, stride = bytearray(), width * 4
    black, low, high = 0, 255, 0
    colors = set()
    for start in range(0, len(bgrx), stride):
        source = bgrx[start:start + stride]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = source[2::4], source[1::4], source[0::4]
        raw.append(0)  # PNG filter None; pixels remain unchanged.
        raw.extend(rgb)
        # Bound quality analysis to a uniform sample; this is a warning detector,
        # not a claim that the OpenGL viewport, geometry or text is correct.
        step = max(1, width // 128)
        for index in range(0, width, step):
            r, g, b = rgb[index * 3:index * 3 + 3]
            black += int(max(r, g, b) <= 3)
            low, high = min(low, r, g, b), max(high, r, g, b)
            if len(colors) < 257: colors.add((r, g, b))
    samples = ((width - 1) // max(1, width // 128) + 1) * height
    issues = []
    if black / samples >= 0.995: issues.append("black_frame")
    elif len(colors) == 1 or high - low <= 2: issues.append("uniform_frame")
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    return png, dict(issues=issues, black_fraction=round(black / samples, 6),
                     sampled_colors_up_to_257=len(colors), rendering_verified=False)


def capture_screenshot(backend, process, main_hwnd, filename, include_owned=True, *, screen=None):
    """Save one unaltered real screen rectangle as a new workspace PNG.

    The caller does not get a successful image when there is occlusion, window
    movement, a hidden/minimized target, or an offscreen/desktop gap. Black/flat
    pixel data is preserved with usable=False, rather than being replaced.
    """
    if type(include_owned) is not bool:
        raise DriverError("invalid_capture_option", "include_owned must be a boolean")
    output = _png_path(filename)
    _verify_target(backend, process)
    screen = screen or NativeScreen(backend)
    before = _capture_plan(screen.scene(), process["pid"], main_hwnd, include_owned)
    pixels = screen.grab(before["rect"])
    backend._verify(process)
    after = _capture_plan(screen.scene(), process["pid"], main_hwnd, include_owned)
    if before != after:
        raise DriverError("capture_scene_changed", "Window geometry or foreground coverage changed during capture")
    left, top, right, bottom = before["rect"]
    png, quality = _png_bytes(right - left, bottom - top, pixels)
    # Repeat path resolution, then CREATE_NEW: no existing file can be overwritten.
    if _png_path(filename) != output:
        raise DriverError("output_path_changed", "Output path changed during capture")
    try:
        with output.open("xb") as stream: stream.write(png)
    except FileExistsError as exc:
        raise DriverError("output_exists", "Output appeared during capture; it was not overwritten") from exc
    except OSError as exc:
        raise DriverError("capture_write_failed", "Could not write screenshot", reason=str(exc)) from exc
    return dict(outcome="captured" if not quality["issues"] else "captured_with_quality_issue",
                filename=str(output), bytes=len(png), sha256=hashlib.sha256(png).hexdigest(),
                pid=process["pid"], main_hwnd=main_hwnd, window_hwnds=before["hwnds"],
                screen_rect=list(before["rect"]), width=right-left, height=bottom-top,
                raw_rect=list(before["raw_rect"]), capture_rect=list(before["rect"]),
                window_rectangles=before["window_rectangles"],
                source="screen_dc_bitblt", assembled_from_multiple_captures=False,
                usable=not quality["issues"], quality=quality,
                captured_at_utc=datetime.now(timezone.utc).isoformat(),
                limitations=["Foreground coverage is checked before and after, not atomically with every pixel.",
                             "Pixel quality heuristics do not verify OpenGL scene correctness or program completeness."])


class NativeScreen:
    """Bounded Win32/GDI screen reader, no PrintWindow or input events."""
    def __init__(self, backend):
        if os.name != "nt":
            raise DriverError("unsupported_platform", "Screen capture requires Windows")
        self.backend, self.u = backend, backend.u
        self.g = c.WinDLL("gdi32", use_last_error=True)
        self.d = c.WinDLL("dwmapi", use_last_error=True)
        def sig(lib, name, args, result):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result
        sig(self.u, "GetTopWindow", [w.HWND], w.HWND)
        sig(self.u, "IsIconic", [w.HWND], w.BOOL)
        sig(self.u, "IsZoomed", [w.HWND], w.BOOL)
        sig(self.u, "GetWindowRect", [w.HWND, c.POINTER(w.RECT)], w.BOOL)
        sig(self.u, "GetClientRect", [w.HWND, c.POINTER(w.RECT)], w.BOOL)
        sig(self.u, "ClientToScreen", [w.HWND, c.POINTER(w.POINT)], w.BOOL)
        sig(self.u, "MonitorFromWindow", [w.HWND, w.DWORD], w.HANDLE)
        class MONITORINFO(c.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]
        self.MONITORINFO = MONITORINFO
        sig(self.u, "GetMonitorInfoW", [w.HANDLE, c.POINTER(MONITORINFO)], w.BOOL)
        sig(self.u, "GetDpiForWindow", [w.HWND], w.UINT)
        sig(self.u, "GetSystemMetricsForDpi", [c.c_int, w.UINT], c.c_int)
        sig(self.u, "GetDC", [w.HWND], w.HDC)
        sig(self.u, "ReleaseDC", [w.HWND, w.HDC], c.c_int)
        sig(self.u, "SetThreadDpiAwarenessContext", [c.c_void_p], c.c_void_p)
        self.MONITORPROC = c.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, c.POINTER(w.RECT), w.LPARAM)
        sig(self.u, "EnumDisplayMonitors", [w.HDC, c.POINTER(w.RECT), self.MONITORPROC, w.LPARAM], w.BOOL)
        sig(self.d, "DwmGetWindowAttribute", [w.HWND, w.DWORD, c.c_void_p, w.DWORD], c.c_long)
        sig(self.g, "CreateCompatibleDC", [w.HDC], w.HDC)
        sig(self.g, "CreateCompatibleBitmap", [w.HDC, c.c_int, c.c_int], w.HBITMAP)
        sig(self.g, "SelectObject", [w.HDC, w.HANDLE], w.HANDLE)
        sig(self.g, "BitBlt", [w.HDC, c.c_int, c.c_int, c.c_int, c.c_int, w.HDC,
                               c.c_int, c.c_int, w.DWORD], w.BOOL)
        sig(self.g, "GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.c_void_p, w.UINT], c.c_int)
        sig(self.g, "DeleteObject", [w.HANDLE], w.BOOL)
        sig(self.g, "DeleteDC", [w.HDC], w.BOOL)

    @contextmanager
    def _dpi(self):
        old = self.u.SetThreadDpiAwarenessContext(c.c_void_p(-4))
        if not old: raise DriverError("capture_dpi", "Could not establish physical-pixel DPI coordinates")
        try: yield
        finally: self.u.SetThreadDpiAwarenessContext(old)

    def scene(self):
        rows, monitors = [], []
        with self._dpi():
            hwnd, seen = self.u.GetTopWindow(None), set()
            while hwnd and hwnd not in seen:
                if len(seen) >= 4096:
                    raise DriverError("capture_window_bound", "Desktop window inventory exceeded its bound")
                seen.add(hwnd)
                row = self.backend._window(hwnd)
                if row["visible"]:
                    rect, cloaked = w.RECT(), w.DWORD()
                    row["iconic"] = bool(self.u.IsIconic(hwnd))
                    row["zoomed"] = bool(self.u.IsZoomed(hwnd))
                    if self.d.DwmGetWindowAttribute(hwnd, 14, c.byref(cloaked), c.sizeof(cloaked)) != 0:
                        raise DriverError("capture_desktop_state", "Cannot verify desktop visibility")
                    row["cloaked"] = bool(cloaked.value)
                    if self.d.DwmGetWindowAttribute(hwnd, 9, c.byref(rect), c.sizeof(rect)) != 0:
                        if not self.u.GetWindowRect(hwnd, c.byref(rect)):
                            raise DriverError("capture_window_rect", "A window changed during rectangle inspection")
                    row["rect"] = [rect.left, rect.top, rect.right, rect.bottom]
                    row["exstyle"] = self.u.GetWindowLongW(hwnd, -20)
                    if row["zoomed"] and not row["iconic"]:
                        info, client = self.MONITORINFO(), w.RECT()
                        info.cbSize = c.sizeof(info)
                        monitor = self.u.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
                        if monitor and self.u.GetMonitorInfoW(monitor, c.byref(info)):
                            mr = info.rcMonitor
                            row["monitor_rect"] = [mr.left, mr.top, mr.right, mr.bottom]
                        if self.u.GetClientRect(hwnd, c.byref(client)):
                            first, last = w.POINT(client.left, client.top), w.POINT(client.right, client.bottom)
                            if (self.u.ClientToScreen(hwnd, c.byref(first)) and
                                    self.u.ClientToScreen(hwnd, c.byref(last))):
                                row["client_screen_rect"] = [first.x, first.y, last.x, last.y]
                        dpi = self.u.GetDpiForWindow(hwnd)
                        if dpi:
                            padded = self.u.GetSystemMetricsForDpi(92, dpi)  # SM_CXPADDEDBORDER
                            row["max_frame_margin"] = [self.u.GetSystemMetricsForDpi(32, dpi) + padded,
                                                       self.u.GetSystemMetricsForDpi(33, dpi) + padded]
                else:
                    row.update(iconic=bool(self.u.IsIconic(hwnd)), cloaked=False,
                               rect=[0, 0, 0, 0], exstyle=0, zoomed=False)
                rows.append(row)
                hwnd = self.u.GetWindow(hwnd, 2)  # GW_HWNDNEXT: foreground to background
            @self.MONITORPROC
            def monitor_callback(handle, dc, rect_ptr, param):
                rect = rect_ptr.contents
                monitors.append([rect.left, rect.top, rect.right, rect.bottom])
                return True
            if not self.u.EnumDisplayMonitors(None, None, monitor_callback, 0) or not monitors:
                raise DriverError("capture_monitor_inventory", "Cannot enumerate visible display surfaces")
        return dict(windows=rows, monitors=monitors)

    def grab(self, rect):
        left, top, right, bottom = rect
        width, height = right-left, bottom-top
        source = memory = bitmap = old = None
        with self._dpi():
            try:
                source = self.u.GetDC(None)
                memory = self.g.CreateCompatibleDC(source) if source else None
                bitmap = self.g.CreateCompatibleBitmap(source, width, height) if memory else None
                if not bitmap: raise DriverError("capture_gdi", "Could not allocate the screen capture bitmap")
                old = self.g.SelectObject(memory, bitmap)
                if not old or old == c.c_void_p(-1).value:
                    raise DriverError("capture_gdi", "Could not select the capture bitmap")
                # SRCCOPY | CAPTUREBLT: real composited screen pixels, no target repaint.
                if not self.g.BitBlt(memory, 0, 0, width, height, source, left, top, 0x40CC0020):
                    raise DriverError("capture_gdi", "Screen pixel copy failed")
                self.g.SelectObject(memory, old)
                old = None  # GetDIBits requires the bitmap not to be selected in a DC.
                info = c.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32,
                                                          0, width*height*4, 0, 0, 0, 0))
                pixels = c.create_string_buffer(width*height*4)
                if self.g.GetDIBits(source, bitmap, 0, height, pixels, info, 0) != height:
                    raise DriverError("capture_gdi", "Could not read all captured bitmap rows")
                return pixels.raw
            finally:
                if old and memory: self.g.SelectObject(memory, old)
                if bitmap: self.g.DeleteObject(bitmap)
                if memory: self.g.DeleteDC(memory)
                if source: self.u.ReleaseDC(None, source)


def _pagination(start_line, max_lines):
    if type(start_line) is not int or type(max_lines) is not int or start_line < 1 or not 1 <= max_lines <= 500:
        raise DriverError("invalid_pagination", "Use a one-based positive start_line and max_lines from 1 to 500")


def read_program_page(backend, process, main_hwnd, start_line=1, max_lines=80):
    """Report the unsupported text extraction honestly; never substitute file text."""
    _pagination(start_line, max_lines)
    _verify_target(backend, process)
    rows = backend.windows(process)
    candidates = [dict(hwnd=row["hwnd"], cls=row["cls"], id=row.get("id"), parent=row["parent"])
                  for row in rows if row.get("visible") and row.get("root") == main_hwnd and
                  row.get("id") == 59648 and row["cls"] in ("SysListView32", "AfxFrameOrView100s")]
    return dict(outcome="pending", supported=False, start_line=start_line, max_lines=max_lines,
                lines=[], complete_program=False, visible_candidate_controls=candidates[:20],
                reason="MF uses a custom MFC NC view and a SysListView32 G-code list. WM_GETTEXT is not its program text.",
                pending="A verified read-only program-buffer or accessibility text provider is required.",
                alternative="show_program_page scrolls the native list; capture_screenshot records the real displayed page.")


def show_program_page(backend, process, main_hwnd, start_line=1, max_lines=80):
    """Scroll the visible native G-code list, leaving selection/execution untouched.

    Only pointer-free standard ListView messages are used. The driver must first
    apply its stopped/no-modal guard. Row indices are not asserted to be NC source
    lines: blank/comment/expanded records may have a different mapping.
    """
    _pagination(start_line, max_lines)
    _verify_target(backend, process)
    backend._check_window(process, main_hwnd, MAIN_CLASS)
    rows = backend.windows(process)
    by_hwnd = {row["hwnd"]: row for row in rows}
    candidates = []
    for row in rows:
        parent = by_hwnd.get(row.get("parent"), {})
        grandparent = by_hwnd.get(parent.get("parent"), {})
        if (row.get("visible") and row.get("root") == main_hwnd and
                row.get("cls") == "SysListView32" and row.get("id") == 59648 and
                parent.get("visible") and parent.get("id") == 65280 and parent.get("cls", "").startswith("Afx:") and
                grandparent.get("cls") == "MDIClient" and grandparent.get("parent") == main_hwnd):
            candidates.append(row)
    if not candidates:
        raise DriverError("program_view_unavailable", "Open the native G-code MDI list first; no verified visible list was found")
    if len(candidates) != 1:
        raise DriverError("program_view_ambiguous", "More than one matching G-code list is visible")
    hwnd = candidates[0]["hwnd"]
    def send(message, wp=0, lp=0):
        row = backend._check_window(process, hwnd, "SysListView32")
        if row["parent"] != candidates[0]["parent"] or row["id"] != 59648:
            raise DriverError("window_changed", "G-code list identity changed")
        return backend._send(hwnd, message, wp, lp)
    if backend.u.GetWindowLongW(hwnd, -16) & 3 != 1:
        raise DriverError("program_view_style", "Native G-code list is not in report mode")
    count, capacity, old_top = send(0x1004), send(0x1028), send(0x1027)
    if not 0 <= count <= 1_000_000 or not 1 <= capacity <= 500:
        raise DriverError("program_view_bounds", "Unexpected native list size or visible-row capacity")
    if start_line > count:
        raise DriverError("program_line_out_of_range", "Requested row is outside the native list", row_count=count)
    first = start_line - 1
    last = min(count - 1, first + min(max_lines, capacity) - 1)
    for index in dict.fromkeys((first, last)):
        if not send(0x1013, index, 0):  # LVM_ENSUREVISIBLE, fPartialOK=FALSE
            raise DriverError("program_scroll_rejected", "Native list did not accept the requested visible row")
    new_count, top, after_capacity = send(0x1004), send(0x1027), send(0x1028)
    if new_count != count or after_capacity != capacity or not 0 <= top < count:
        raise DriverError("program_view_changed", "Program list changed while paging")
    end = min(count, top + capacity)
    if not top <= first <= last < end:
        raise DriverError("program_scroll_not_verified", "Requested rows are not in the reported visible range")
    return dict(outcome="scroll_observed" if top != old_top else "already_visible", hwnd=hwnd,
                requested_range=dict(first=start_line, last=last+1),
                actual_visible_range=dict(first=top+1, last=end), row_count=count,
                visible_capacity=capacity, program_selection_changed=False,
                source_line_mapping_verified=False, text_read=False,
                screenshot_required=True)


def _decode_controller_program_state(read, base):
    """Read the pinned CRT's EDIT text model; all offsets have static provenance."""
    def number(address, signed=False):
        data = read(address, 4)
        if len(data) != 4:
            raise DriverError("program_page_unreadable", "Incomplete controller page read")
        return int.from_bytes(data, "little", signed=signed)
    crt = number(base + 0x5CB180)
    if not 0x10000 <= crt <= 0xFFFFFFFF - 0x1D758:
        raise DriverError("controller_pointer_invalid", "CRT pointer is outside the supported x86 address range")
    control = crt + 0x11638  # CRT + program(3550) + EDIT panel(DAD0) + text(618)
    fields = {"controller_hwnd": (crt+0x20, False), "page": (crt+0x1D750, True),
              "subpage": (crt+0x1263C, True), "text_hwnd": (control+0x20, False),
              "model": (control+0xD0, False), "cursor_row": (control+0x430, True),
              "cursor_column": (control+0x434, True), "active_row": (control+0x438, True),
              "top_row": (control+0x43C, True), "page_capacity": (control+0x440, True),
              "row_count": (base+0x5D1854, True)}
    result = {name: number(address, signed) for name, (address, signed) in fields.items()}
    result.update(crt_pointer=crt, text_control=control)
    if result["model"] != base + 0x5D1850:
        raise DriverError("program_page_model_mismatch", "EDIT control is not bound to the verified loaded NC source")
    count = result["row_count"]
    if (not 1 <= count <= 1_000_000 or not 0 <= result["top_row"] < count or
            not -1 <= result["cursor_row"] <= count or not -1 <= result["active_row"] <= count or
            not 0 <= result["cursor_column"] <= 1_000_000 or not 0 <= result["page_capacity"] <= 500):
        raise DriverError("program_page_bounds", "Controller page counters are outside their bounded range")
    result["coherent"] = (crt == number(base+0x5CB180) and
                          all(result[name] == number(address, signed)
                              for name, (address, signed) in fields.items()))
    return result


def _read_controller_program_state(backend, process, base):
    with backend._handle(process["pid"], 0x1000 | 0x10) as handle:
        backend._verify(process, handle)
        def read(address, size):
            data, count = c.create_string_buffer(size), c.c_size_t()
            if not backend.k.ReadProcessMemory(handle, address, data, size, c.byref(count)) or count.value != size:
                raise DriverError("program_page_unreadable", "Could not read the pinned controller program view")
            return data.raw
        result = _decode_controller_program_state(read, base)
        backend._verify(process, handle)
        return result


def controller_program_page(backend, process, main_hwnd, action="open"):
    """Operate one native EDIT/PROG page button, with before/after invariants.

    Caller holds Driver._lock. No mode changes, source edits, private calls or
    memory writes occur. Native paging can move the EDIT cursor, so this is not
    a read-only operation. It must not change the executable program position.
    """
    from driver import Driver
    commands = {"open": (7031, "PROG"), "previous": (7042, "PAGEUP"), "next": (7043, "PAGEDOWN")}
    if not isinstance(action, str) or action not in commands:
        raise DriverError("invalid_program_page_action", "Use open, previous or next")
    _verify_target(backend, process)
    base = backend.module_base(process)
    guard = Driver(backend)
    def sample():
        rows = backend.windows(process)
        mains = [row for row in rows if row["hwnd"] == main_hwnd and row["cls"] == MAIN_CLASS]
        if len(mains) != 1:
            raise DriverError("window_changed", "Simulator main window identity changed")
        raw = backend.read_state(process, base)
        context = dict(process=process, base=base, main=mains[0], windows=rows)
        guard._guard(context, dict(responsive=backend.probe(main_hwnd), telemetry=raw))
        if raw["mode"] != 1:
            raise DriverError("wrong_mode", "EDIT mode is required for controller program paging; no mode was changed",
                              observed_mode=raw["mode"], expected_mode=1)
        if not raw["power"] or not raw["document"] or not raw["descriptor_count"]:
            raise DriverError("no_program", "A powered controller with a loaded program is required")
        panels = Driver._controller_panels(context)
        if len(panels) != 1:
            raise DriverError("controller_panel_unavailable", "Exactly one visible verified controller panel is required")
        page = _read_controller_program_state(backend, process, base)
        if not page["coherent"]:
            raise DriverError("state_changing", "Controller page changed during its read-only snapshot")
        if page["controller_hwnd"] != panels[0]["hwnd"] or page["model"] != base+0x5D1850:
            raise DriverError("program_page_model_mismatch", "CRT HWND or loaded source model does not match the visible controller")
        return context, raw, page
    def position(raw):
        return tuple(raw[name] for name in ("document", "descriptor_array", "descriptor_count", "index", "source_line")) + tuple(raw["xyz"])
    def visible(context, page):
        matches = [row for row in context["windows"] if row["hwnd"] == page["text_hwnd"] and
                   row["root"] == page["controller_hwnd"] and row["cls"] == "Static"]
        if page["page"] != 1 or page["subpage"] != 0 or len(matches) != 1 or page["page_capacity"] < 1:
            return False
        try: Driver._enabled_chain(context, matches[0])
        except DriverError: return False
        return True
    context, raw, before = sample()
    source = backend.program_source(process, base)
    if not source.get("coherent") or not source.get("sha256"):
        raise DriverError("program_changing", "Cannot verify the live NC source before paging")
    current_position = position(raw)
    already_open = action == "open" and visible(context, before)
    if action != "open" and not visible(context, before):
        raise DriverError("controller_program_page_not_open", "Open the default EDIT/PROG text page before paging")
    command, caption = commands[action]
    sent = False
    if not already_open:
        buttons = [row for row in context["windows"] if row.get("id") == command and row["cls"] == "Button" and row["visible"] and
                   row["parent"] == before["controller_hwnd"] and row["root"] == before["controller_hwnd"]]
        if len(buttons) != 1:
            raise DriverError("program_page_button_unavailable", "The native program page button is absent or ambiguous")
        button = backend._check_window(process, buttons[0]["hwnd"], "Button")
        Driver._enabled_chain(context, button)
        if backend.read_text(process, button["hwnd"], 32).strip().upper() != caption:
            raise DriverError("program_page_button_mismatch", "Program page button caption did not match its pinned ID")
        refreshed, fresh_raw, fresh_page = sample()
        if position(fresh_raw) != current_position or fresh_page != before:
            raise DriverError("state_changing", "Program or controller page changed before dispatch")
        parent = backend._check_window(process, before["controller_hwnd"], "#32770")
        child = backend._check_window(process, button["hwnd"], "Button")
        if child["parent"] != parent["hwnd"] or child["id"] != command:
            raise DriverError("window_changed", "The native button identity changed before dispatch")
        # BN_CLICKED=0. LPARAM is the verified button HWND, never a target pointer.
        backend._send(parent["hwnd"], 0x111, command, child["hwnd"], timeout=1000)
        sent = True
    deadline = time.monotonic() + 1.0
    while True:
        context, after_raw, after = sample()
        if position(after_raw) != current_position:
            raise DriverError("execution_state_changed", "Program execution position/XYZ changed while paging; no further command sent",
                              command_sent=sent, before=raw, after=after_raw)
        if (after["crt_pointer"] != before["crt_pointer"] or after["text_control"] != before["text_control"] or
                after["model"] != before["model"] or after["row_count"] != before["row_count"]):
            raise DriverError("program_page_changed", "Controller or source model changed during the page action", command_sent=sent)
        if visible(context, after): break
        if time.monotonic() >= deadline:
            raise DriverError("program_page_not_verified", "The command did not expose the verified EDIT/PROG text page",
                              command_sent=sent, observed=after)
        time.sleep(0.05)
    observed_source = backend.program_source(process, base)
    if not observed_source.get("coherent") or observed_source.get("sha256") != source["sha256"]:
        raise DriverError("program_content_changed", "Loaded NC source changed during the page action", command_sent=sent)
    changed = after["top_row"] != before["top_row"]
    outcome = "already_open" if already_open else "program_page_opened" if action == "open" else "scroll_observed" if changed else "no_scroll_observed"
    return dict(outcome=outcome, action=action, command_sent=sent, button_id=command,
                controller_hwnd=after["controller_hwnd"], text_hwnd=after["text_hwnd"],
                before=before, after=after, execution_state_unchanged=True, source_content_unchanged=True,
                source_sha256=source["sha256"], selection_may_change=True,
                exact_visible_range_verified=False, source_line_mapping_verified=False, screenshot_required=True,
                limitations=["top_row/cursor_row are zero-based native text-model rows, not executable descriptor indices.",
                             "Page capacity and wrapping do not establish an exact last visible source line.",
                             "Native EDIT cursor movement is allowed; execution position and source hash were compared."])


def _read_view_state(backend, process, base):
    """Read only the pinned MF view and interaction fields; never call its VAs."""
    with backend._handle(process["pid"], 0x1000 | 0x10) as handle:
        backend._verify(process, handle)
        def read(address, size=4):
            buffer, count = c.create_string_buffer(size), c.c_size_t()
            if not backend.k.ReadProcessMemory(handle, address, buffer, size, c.byref(count)) or count.value != size:
                raise DriverError("view_memory_unreadable", "Cannot read the pinned MF view state")
            return int.from_bytes(buffer.raw, "little")
        pointer = read(base + 0x5CB170)
        if not 0x10000 <= pointer <= 0xFFFFFFFF - 0x605C:
            raise DriverError("view_pointer_invalid", "Current MF view pointer is invalid")
        result = dict(view_pointer=pointer, hwnd=read(pointer + 0x20),
                      interaction_mode=read(pointer + 0x5C58), left_drag=read(pointer + 0x5C5C),
                      right_drag=read(pointer + 0x5C60), alternate_view=read(pointer + 0x5934),
                      special_layout=read(base + 0x5CCAD4), overlay_enabled=read(base + 0x5CCB2E, 1))
        result["coherent"] = pointer == read(base + 0x5CB170)
        return result


def _client_rect(backend, hwnd):
    backend.u.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
    backend.u.GetClientRect.restype = w.BOOL
    rect = w.RECT()
    if not backend.u.GetClientRect(hwnd, c.byref(rect)):
        raise DriverError("view_client_rect", "Could not read the native view client area")
    return rect.left, rect.top, rect.right, rect.bottom


def rotate_view_relative(backend, process, main_hwnd, dx=0.12, dy=0.08):
    """Send one bounded relative drag through the normal MF view handlers.

    dx/dy are fractions of client width/height, NOT degrees. The driver must
    serialize and apply its no-modal guard. No mouse cursor is repositioned.
    Normal 3D context only; retain rotate mode after the operation.
    """
    if (any(type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 0.35
            for value in (dx, dy)) or (dx == 0 and dy == 0)):
        raise DriverError("invalid_rotation", "Use finite normalized dx/dy within +/-0.35 and a nonzero displacement")
    _verify_target(backend, process)
    base = backend.module_base(process)
    telemetry = backend.read_state(process, base)
    if not telemetry.get("coherent") or telemetry.get("worker") or telemetry.get("cycle"):
        raise DriverError("busy", "Relative view interaction requires a coherent stopped simulator")
    if telemetry.get("emergency"):
        raise DriverError("emergency_active", "Emergency state blocks this view operation")
    backend._check_window(process, main_hwnd, MAIN_CLASS)
    initial = _read_view_state(backend, process, base)
    if not initial["coherent"]:
        raise DriverError("view_state_changed", "View identity changed during readback")
    if initial["alternate_view"] or initial["special_layout"] or initial["overlay_enabled"]:
        raise DriverError("view_context_unsupported", "Relative rotation is limited to the ordinary 3D view")
    if initial["left_drag"] or initial["right_drag"]:
        raise DriverError("view_interaction_busy", "An existing view drag is active")
    hwnd = initial["hwnd"]
    row = backend._check_window(process, hwnd, "AfxFrameOrView100s")
    if row["root"] != main_hwnd or row["id"] != 59648:
        raise DriverError("wrong_view_window", "View pointer HWND does not match the main MDI viewport")
    rect = _client_rect(backend, hwnd)
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    if rect[:2] != (0, 0) or not 100 <= width <= 32767 or not 100 <= height <= 32767:
        raise DriverError("view_client_rect", "Viewport dimensions are unsuitable for a bounded client-coordinate drag")
    start = (width // 2, height // 2)
    end = (round(start[0] + dx * width), round(start[1] + dy * height))
    if end == start:
        raise DriverError("invalid_rotation", "Requested displacement rounds to zero screen pixels")
    def check_view():
        row = backend._check_window(process, hwnd, "AfxFrameOrView100s")
        current = _read_view_state(backend, process, base)
        if (not current["coherent"] or current["view_pointer"] != initial["view_pointer"] or
                current["hwnd"] != hwnd or row["root"] != main_hwnd or
                current["alternate_view"] or current["special_layout"] or current["overlay_enabled"] or
                _client_rect(backend, hwnd) != rect):
            raise DriverError("view_state_changed", "View identity, layout or rectangle changed during interaction")
        return current
    mode_changed = initial["interaction_mode"] != 32777
    if mode_changed:
        backend._check_window(process, main_hwnd, MAIN_CLASS)
        backend._send(main_hwnd, 0x111, 32777, 0)
    if check_view()["interaction_mode"] != 32777:
        raise DriverError("rotation_mode_not_observed", "Normal rotate command did not read back as rotate mode")
    def packed(point): return (point[1] << 16) | point[0]
    last_point, release_error = start, None
    try:
        # A timed-out press may still have been dispatched: always attempt one
        # release to the same verified target in finally, never resend the press.
        backend._send(hwnd, 0x201, 1, packed(start))
        if not check_view()["left_drag"]:
            raise DriverError("rotation_press_not_observed", "MF did not enter left-drag state")
        for step in range(1, 9):
            if check_view()["interaction_mode"] != 32777:
                raise DriverError("view_state_changed", "Rotate mode changed during drag")
            last_point = (round(start[0] + (end[0] - start[0]) * step / 8),
                          round(start[1] + (end[1] - start[1]) * step / 8))
            backend._send(hwnd, 0x200, 1, packed(last_point))
    finally:
        try:
            # Do not require the old rectangle/mode for button release, but never
            # send to a disappeared HWND or another process after a race.
            backend._check_window(process, hwnd, "AfxFrameOrView100s")
            current = _read_view_state(backend, process, base)
            if current["view_pointer"] != initial["view_pointer"] or current["hwnd"] != hwnd:
                raise DriverError("view_state_changed", "Original drag target disappeared before release")
            backend._send(hwnd, 0x202, 0, packed(last_point))
        except DriverError as exc:
            release_error = exc
        if release_error:
            raise DriverError("rotation_release_unverified", "Could not release the verified original drag target",
                              reason=release_error.as_dict()) from release_error
    final = check_view()
    if final["left_drag"] or final["right_drag"]:
        raise DriverError("rotation_release_unverified", "MF drag flags remained active after release")
    return dict(outcome="relative_drag_sent", hwnd=hwnd, normalized_delta=[dx, dy],
                start_client=list(start), end_client=list(end), mode_changed=mode_changed,
                interaction_mode_after=final["interaction_mode"], drag_released=True,
                absolute_angle_verified=False, visual_change_verified=False,
                screenshot_required=True, coordinate_space="view_client_logical_pixels")


def view_capabilities():
    """Evidence catalogue, not a dispatcher or permission to call private VAs."""
    return dict(
        supported={name: dict(command_id=value, runtime_verified=True) for name, value in VIEWS.items()},
        pending={
            "reset": dict(command_id=32781, runtime_verified=False, evidence="MF range handler 0x6ECDD0 calls 0x6E11B0 for 0x800D; naming is not closed"),
            "full_screen": dict(command_id=32919, runtime_verified=False, evidence="Menu label plus MF handlers 0x68E2F0 / 0x6DCD20; window layout, not camera fitting"),
            "exit_full_screen": dict(command_id=32895, runtime_verified=False, evidence="Menu label plus MF handler 0x6DB910"),
            "show_workpiece": dict(command_id=32798, runtime_verified=False, evidence="Historical observed toggle; MF handler 0x710970; current state readback still required"),
            "rotate_mode": dict(command_id=32777, runtime_verified=False, evidence="MF 0x6ECDD0 toggles object+0x5C58; enables relative mouse interaction, not an angle"),
        },
        arbitrary_angles=dict(supported=False, reason="No verified external yaw/pitch/roll parameter interface"),
        program_text=dict(supported=False, reason="Custom MFC view/ListView; WM_GETTEXT is not program content"),
        native_program_paging=dict(supported=True, runtime_verified=False, source="Standard scalar ListView messages on structurally identified G-code list"),
        relative_rotation=dict(supported=True, runtime_verified=False, evidence="MF mouse map 0x923D78/0x923DA8/0x923DC0; normal-view 0x8009 branch calls 0x6CD060 with client deltas"),
        target_sha256=TARGET_SHA256)
