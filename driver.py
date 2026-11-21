"""Guarded SSCNC operations; all mutable state is serialized per server process."""
import hashlib
import ntpath
import threading
from pathlib import Path, PureWindowsPath

from constants import CONTROLLER_FINGERPRINT, HOLD_ID, MAIN_CLASS, MEM_MODE, SAVE_AS_ID, START_ID, TARGET_PATH, TARGET_SHA256, VIEWS, WORKSPACE
from errors import DriverError


def _same_path(left, right):
    return ntpath.normcase(ntpath.normpath(left)) == ntpath.normcase(ntpath.normpath(right))


def _session(context, telemetry):
    process = context["process"]
    return (process["pid"], process["creation_time"], telemetry["document"],
            telemetry["descriptor_count"], telemetry.get("descriptor_array"))


def _position(context, telemetry):
    return (_session(context, telemetry), telemetry["index"], telemetry["source_line"],
            tuple(telemetry["xyz"]), telemetry["m30_count"], telemetry["end_count"])


class Driver:
    def __init__(self, backend=None, wait_timeout=8.0):
        if backend is None:
            from win32_backend import WindowsBackend
            backend = WindowsBackend()
        self.backend = backend
        self.wait_timeout = wait_timeout
        self._lock = threading.RLock()
        self._paused = None
        self._run = None

    def _discover(self):
        processes = self.backend.list_processes()
        matches = [p for p in processes if _same_path(p["path"], TARGET_PATH)]
        if not matches:
            code = "wrong_executable" if processes else "not_running"
            raise DriverError(code, "No running instance of the exact pinned FANUC0iMF executable was found")
        if len(matches) != 1:
            raise DriverError("ambiguous_process", "Multiple pinned simulator instances are running", pids=[p["pid"] for p in matches])
        if self.backend.sha256(TARGET_PATH).lower() != TARGET_SHA256:
            raise DriverError("binary_hash_mismatch", "Executable hash differs from the supported MF binary")
        process = matches[0]
        windows = self.backend.windows(process)
        mains = [row for row in windows if row["cls"] == MAIN_CLASS and row["visible"]]
        if len(mains) != 1:
            raise DriverError("ambiguous_main_window", "Exactly one visible simulator main window is required", count=len(mains))
        return dict(process=process, main=mains[0], windows=windows, base=self.backend.module_base(process))

    @staticmethod
    def _panels(context):
        parents = {}
        for row in context["windows"]:
            if row["visible"] and row["cls"] == "Button" and row["id"] in (9500, HOLD_ID, START_ID):
                parents.setdefault(row["parent"], set()).add(row["id"])
        return [row for row in context["windows"] if row["cls"] == "#32770" and
                parents.get(row["hwnd"]) == {9500, HOLD_ID, START_ID} and
                (row["root"] == context["main"]["hwnd"] or row["owner"] == context["main"]["hwnd"])]

    @staticmethod
    def _controller_panels(context):
        main_hwnd = context["main"]["hwnd"]
        candidates = []
        for row in context["windows"]:
            if not (row["visible"] and row["cls"] == "#32770" and row["root"] == row["hwnd"] and
                    row["owner"] == main_hwnd and row["parent"] == main_hwnd):
                continue
            children = {(child["cls"], child["id"]) for child in context["windows"]
                        if child["parent"] == row["hwnd"] and child["root"] == row["hwnd"] and child["visible"]}
            if CONTROLLER_FINGERPRINT <= children: candidates.append(row)
        return candidates

    @staticmethod
    def _modals(context):
        excluded = set()
        # Only one unambiguous, structurally identified window of each permanent
        # panel type is excluded. Titles (including blank titles) are not used.
        for panels in (Driver._panels(context), Driver._controller_panels(context)):
            if len(panels) == 1: excluded.add(panels[0]["hwnd"])
        return [row for row in context["windows"] if row["visible"] and row["cls"] == "#32770" and
                row["root"] == row["hwnd"] and row["hwnd"] not in excluded]

    def _capture(self, context=None):
        if context is None: context = self._discover()
        else:
            context = dict(context, windows=self.backend.windows(context["process"]))
            main = [w for w in context["windows"] if w["hwnd"] == context["main"]["hwnd"]]
            if len(main) != 1: raise DriverError("window_changed", "Simulator main window disappeared")
            context["main"] = main[0]
        responsive = self.backend.probe(context["main"]["hwnd"])
        telemetry, read_error = None, None
        try: telemetry = self.backend.read_state(context["process"], context["base"])
        except DriverError as exc: read_error = exc.as_dict()
        response = dict(ok=True, state="stopped_unknown", responsive=responsive,
                        target=dict(path=TARGET_PATH, sha256=TARGET_SHA256, pid=context["process"]["pid"],
                                    creation_time=context["process"]["creation_time"], module_base=hex(context["base"]),
                                    main_hwnd=context["main"]["hwnd"]),
                        main_enabled=context["main"]["enabled"], modal_windows=self._modals(context),
                        standard_panels=self._panels(context),
                        controller_panels=self._controller_panels(context),
                        telemetry=telemetry, completion={"confirmed": False, "evidence": []})
        if read_error: response["telemetry_error"] = read_error
        if telemetry is not None: self._interpret(context, telemetry, response)
        if not responsive: response["state"] = "unresponsive"
        elif telemetry is None: response["state"] = "unreadable"
        elif not telemetry["coherent"]: response["state"] = "busy"
        response["limitations"] = [
            "Read-only samples are not an atomic controller snapshot; XYZ can update at block endpoints.",
            "M30 counters can change during preview. No read-only sample proves normal worker drain, cut accuracy or full completion.",
        ]
        return context, response

    def _interpret(self, context, telemetry, response):
        key = _session(context, telemetry)
        if self._run and self._run["key"] != key: self._run = None
        if self._paused and self._paused != _position(context, telemetry): self._paused = None
        if telemetry["worker"]:
            if self._run is None: self._begin_run(context, telemetry)
            self._run["saw_worker"] = True
        if self._run:
            self._run["saw_progress"] |= telemetry["index"] != self._run["index"]
        if telemetry["worker"] or telemetry["cycle"]:
            response["state"] = "busy"
        elif self._paused == _position(context, telemetry):
            response["state"] = "paused"
        elif self._run and self._run["saw_worker"] and self._run["saw_progress"]:
            run = self._run
            if (telemetry["m30_count"] > run["m30"] and telemetry["end_count"] > run["end"] and
                    telemetry["current_mcode"] == 30 and not telemetry["emergency"] and telemetry["coherent"]):
                response["state"] = "possible_complete"
                response["completion"]["evidence"] = [
                    "Worker activity observed in this server session", "Program index changed",
                    "M30 and end counters increased", "Current M code is 30; cycle and worker are now zero",
                ]
                response["completion"]["missing"] = ["Run-origin M30 and normal-drain provenance", "Geometric/depth verification"]

    def _begin_run(self, context, telemetry):
        self._run = dict(key=_session(context, telemetry), index=telemetry["index"],
                         m30=telemetry["m30_count"], end=telemetry["end_count"], saw_worker=False, saw_progress=False)

    def status(self):
        with self._lock:
            try: return self._capture()[1]
            except DriverError as exc:
                return dict(ok=False, state=exc.code, error=exc.as_dict(), completion={"confirmed": False, "evidence": []})

    def _guard(self, context, state, activity="idle", modal=None, mem=False, save_scope=None, allow_emergency=False):
        if not state["responsive"]: raise DriverError("unresponsive", "Simulator main window is not responding; no action sent")
        dialogs = self._modals(context)
        if modal is None:
            if dialogs: raise DriverError("modal_dialog", "Close the existing modal dialog before this action", windows=dialogs)
            if not context["main"]["enabled"]: raise DriverError("main_disabled", "Simulator top-level parent is disabled")
        else:
            allowed = {modal}
            if save_scope is not None:
                allowed |= self._scoped_save_dialogs(context, save_scope, modal)
            if any(d["hwnd"] not in allowed for d in dialogs) or not any(d["hwnd"] == modal and d["enabled"] for d in dialogs):
                raise DriverError("unexpected_dialog", "Only this save flow's verified selection and filename dialogs may coexist")
        raw = state["telemetry"]
        if raw is None: raise DriverError("unreadable", "Cannot verify simulator state before a command")
        if not raw["coherent"]: raise DriverError("state_changing", "Telemetry changed during the read; retry after state settles")
        if raw["emergency"] and not allow_emergency: raise DriverError("emergency_active", "Emergency stop is active; this tool does not release it")
        if activity == "idle" and (raw["worker"] or raw["cycle"]):
            raise DriverError("busy", "Simulation is active; pause before this action")
        if activity == "running" and not raw["worker"]:
            raise DriverError("already_stopped", "No active worker; no Feed Hold will be sent")
        if mem:
            if raw["mode"] != MEM_MODE:
                raise DriverError("wrong_mode", "MEM mode is required; the tool does not change modes", observed_mode=raw["mode"], expected_mode=2)
            if not raw["power"]: raise DriverError("power_off", "Controller power state is off")
            if not raw["document"] or not raw["descriptor_count"]:
                raise DriverError("no_program", "A loaded executable program is required")

    @staticmethod
    def _enabled_chain(context, row):
        by_id = {w["hwnd"]: w for w in context["windows"]}
        cursor, seen = row, set()
        while cursor and cursor["hwnd"] not in seen:
            seen.add(cursor["hwnd"])
            if not cursor["enabled"] or not cursor["visible"]:
                raise DriverError("control_disabled", "Control or its actual parent chain is disabled/hidden")
            if cursor["hwnd"] == row["root"]: return
            cursor = by_id.get(cursor["parent"])
        raise DriverError("window_changed", "Cannot establish the actual control parent chain")

    def _control(self, context, control_id, cls, root):
        rows = [w for w in context["windows"] if w["id"] == control_id and w["cls"] == cls and w["root"] == root and w["visible"]]
        if len(rows) != 1:
            raise DriverError("ambiguous_control", "Exactly one matching visible control is required", control_id=control_id, count=len(rows))
        self._enabled_chain(context, rows[0])
        return rows[0]

    def _refresh(self, expected):
        context = self._discover()
        if (context["process"] != expected["process"] or context["base"] != expected["base"] or
                context["main"]["hwnd"] != expected["main"]["hwnd"]):
            raise DriverError("process_changed", "Simulator/window identity changed during the action")
        return self._capture(context)

    def _button(self, context, control_id, activity, bookmark=None):
        fresh, state = self._refresh(context)
        self._guard(fresh, state, activity=activity, mem=control_id == START_ID)
        if bookmark is not None and _position(fresh, state["telemetry"]) != bookmark:
            raise DriverError("position_changed", "Verified program position changed before dispatch; no Start was sent")
        panels = self._panels(fresh)
        if len(panels) != 1:
            raise DriverError("ambiguous_panel", "Exactly one standard MEM/Hold/Start panel is required", count=len(panels))
        control = self._control(fresh, control_id, "Button", panels[0]["root"])
        if control["parent"] != panels[0]["hwnd"]:
            raise DriverError("window_changed", "Button is no longer a direct child of the identified standard panel")
        self.backend.post_command(fresh["process"], control["parent"], control_id, control["hwnd"])

    def _start_or_resume(self, resume):
        with self._lock:
            context, state = self._capture()
            self._guard(context, state, mem=True)
            raw = state["telemetry"]
            bookmark = self._paused
            if resume:
                if bookmark is None or bookmark != _position(context, raw):
                    raise DriverError("pause_not_verified", "Resume requires a stable pause observed by this server; do not infer pause from zero flags")
            elif bookmark is not None or raw["index"] not in (0, -1):
                raise DriverError("start_position_not_initial", "Start requires an initial program index; use verified resume after pause. This tool never resets the program", index=raw["index"])
            self._button(context, START_ID, "idle", bookmark if resume else _position(context, raw))
            self._paused = None
            self._begin_run(context, raw)
            deadline = self.backend.monotonic() + self.wait_timeout
            latest = state
            while self.backend.monotonic() < deadline:
                _, latest = self._capture(context)
                if latest["state"] == "unresponsive": break
                if latest["telemetry"] and latest["telemetry"]["worker"]:
                    return dict(ok=True, outcome="activity_observed", status=latest)
                self.backend.sleep(0.1)
            return dict(ok=True, outcome="command_sent_effect_unconfirmed", status=latest)

    def start(self): return self._start_or_resume(False)
    def resume(self): return self._start_or_resume(True)

    def pause(self):
        with self._lock:
            context, state = self._capture()
            raw = state["telemetry"]
            if raw is not None and not raw["worker"]:
                return dict(ok=True, outcome="already_stopped", status=state)
            self._guard(context, state, activity="running")
            try: self._button(context, HOLD_ID, "running")
            except DriverError as exc:
                if exc.code == "already_stopped": return dict(ok=True, outcome="already_stopped", status=self._capture(context)[1])
                raise
            baseline = (raw["m30_count"], raw["end_count"])
            deadline, stable, stable_at = self.backend.monotonic() + self.wait_timeout, None, None
            latest = state
            while self.backend.monotonic() < deadline:
                current, latest = self._capture(context)
                sample = latest["telemetry"]
                if latest["state"] == "unresponsive": break
                if sample and sample["coherent"] and not (sample["worker"] or sample["cycle"]):
                    position = _position(current, sample)
                    if stable == position and self.backend.monotonic() - stable_at >= 0.2:
                        if (sample["m30_count"], sample["end_count"]) == baseline and not sample["emergency"]:
                            self._paused = position
                            latest["state"] = "paused"
                            return dict(ok=True, outcome="paused_at_stable_position", status=latest,
                                        pause_semantics="Worker exited and sampled position is stable; mid-interpolation hold is not proven")
                        return dict(ok=True, outcome="worker_stopped_not_verified_pause", status=latest)
                    if stable != position: stable, stable_at = position, self.backend.monotonic()
                else: stable = stable_at = None
                self.backend.sleep(0.1)
            return dict(ok=True, outcome="hold_sent_stop_unconfirmed", status=latest)

    def set_view(self, view: str):
        if view not in VIEWS:
            raise DriverError("invalid_view", "Supported views: front, right, top, zoom_in, zoom_out")
        with self._lock:
            context, state = self._capture()
            self._guard(context, state)
            context, state = self._refresh(context)
            self._guard(context, state)
            self.backend.post_command(context["process"], context["main"]["hwnd"], VIEWS[view])
            return dict(ok=True, outcome="view_command_sent", view=view, visual_effect_verified=False,
                        status=self._capture(context)[1])

    @staticmethod
    def _output_path(filename):
        if not isinstance(filename, str) or not filename or len(filename) > 240:
            raise DriverError("invalid_output_path", "Provide a full .wp path of at most 240 characters")
        win = PureWindowsPath(filename)
        if not win.is_absolute() or len(win.drive) != 2 or filename.startswith(("\\\\", "//")):
            raise DriverError("path_outside_workspace", "Output must be an absolute local path inside SSCNC_WORKSPACE")
        for part in win.parts[1:]:
            if part in (".", "..") or part.endswith((".", " ")) or any(ch in '<>:"|?*' or ord(ch) < 32 for ch in part):
                raise DriverError("invalid_output_path", "Traversal, device/stream syntax, and invalid filename characters are not accepted")
            if PureWindowsPath(part).is_reserved():
                raise DriverError("invalid_output_path", "Windows reserved filenames are not accepted")
        path, root = Path(filename).resolve(strict=False), Path(WORKSPACE).resolve(strict=True)
        try: path.relative_to(root)
        except ValueError as exc: raise DriverError("path_outside_workspace", "Resolved output leaves the workspace") from exc
        if path.suffix.casefold() != ".wp": raise DriverError("invalid_extension", "Workpiece output must use .wp")
        if not path.parent.is_dir(): raise DriverError("output_parent_missing", "Create the output directory before saving")
        if path.exists() or path.is_symlink(): raise DriverError("output_exists", "Refusing to overwrite an existing workpiece")
        try: str(path).encode("mbcs")
        except (UnicodeError, LookupError) as exc:
            raise DriverError("unsupported_filename", "Filename cannot be represented by the local Windows code page") from exc
        return path

    def _save_owners(self, context):
        main = context["main"]["hwnd"]
        owners = {main}
        for panels in (self._panels(context), self._controller_panels(context)):
            if len(panels) == 1:
                panel = panels[0]
                if panel["root"] == panel["hwnd"] and panel["owner"] == main and panel["parent"] == main:
                    owners.add(panel["hwnd"])
        return owners

    def _make_save_scope(self, origin, fresh, dialog, listbox, selection):
        # This token exists only within the current save_workpiece call. It is not
        # a global exception for selection dialogs that happened to be open.
        # MFC can pre-create a hidden dialog and show it for this command. Window
        # handles also outlive visibility; only pre-existing visible windows are
        # ineligible. The flow still revalidates ownership, controls and selection.
        previous_visible_hwnds = {row["hwnd"] for row in origin["windows"] if row["visible"]}
        main = origin["main"]["hwnd"]
        if (dialog["hwnd"] in previous_visible_hwnds or dialog["root"] != dialog["hwnd"] or
                dialog["owner"] not in self._save_owners(fresh) or dialog["parent"] != dialog["owner"] or selection["item_data"] != 1):
            raise DriverError("unexpected_dialog", "Selection dialog did not newly appear for this Save As flow")
        confirm = self._control(fresh, 1, "Button", dialog["hwnd"])
        if listbox["parent"] != dialog["hwnd"] or confirm["parent"] != dialog["hwnd"]:
            raise DriverError("unexpected_dialog", "Selection controls have an unexpected direct parent")
        return dict(process=dict(origin["process"]), main=main, previous_visible_hwnds=previous_visible_hwnds,
                    selection_dialog=dict(dialog), listbox=dict(listbox), confirm=dict(confirm), selected=dict(selection))

    def _scoped_save_dialogs(self, context, scope, filename_hwnd=None):
        if context["process"] != scope["process"] or context["main"]["hwnd"] != scope["main"]:
            raise DriverError("process_changed", "Save flow identity changed")
        allowed = set()
        expected = scope["selection_dialog"]
        owners = self._save_owners(context)
        if expected["owner"] not in owners:
            raise DriverError("unexpected_dialog", "Save dialog owner is no longer a verified application panel")
        current = [row for row in context["windows"] if row["hwnd"] == expected["hwnd"] and row["visible"]]
        if current:
            row = current[0]
            fields = ("pid", "cls", "root", "owner", "parent")
            if any(row[key] != expected[key] for key in fields) or not row["enabled"]:
                raise DriverError("unexpected_dialog", "This save flow's selection dialog changed identity or became disabled")
            listbox = self._control(context, 1067, "ListBox", row["hwnd"])
            confirm = self._control(context, 1, "Button", row["hwnd"])
            if (listbox["hwnd"] != scope["listbox"]["hwnd"] or listbox["parent"] != row["hwnd"] or
                    confirm["hwnd"] != scope["confirm"]["hwnd"] or confirm["parent"] != row["hwnd"]):
                raise DriverError("unexpected_dialog", "Selection dialog controls were replaced")
            selected = self.backend.read_workpiece_selection(context["process"], listbox["hwnd"])
            if selected != scope["selected"]:
                raise DriverError("selection_changed", "The selected workpiece item changed during Save As")
            allowed.add(row["hwnd"])
        if filename_hwnd is not None:
            candidates = [row for row in context["windows"] if row["hwnd"] == filename_hwnd and row["visible"]]
            if len(candidates) != 1:
                raise DriverError("unexpected_dialog", "Expected filename dialog disappeared")
            row = candidates[0]
            if (row["hwnd"] in scope["previous_visible_hwnds"] or row["hwnd"] == expected["hwnd"] or
                    row["cls"] != "#32770" or row["root"] != row["hwnd"] or
                    row["pid"] != context["process"]["pid"] or row["parent"] != row["owner"] or
                    row["owner"] not in {scope["main"], expected["owner"]} or row["owner"] not in owners or not row["enabled"]):
                raise DriverError("unexpected_dialog", "Filename dialog must newly appear under this save flow's verified application owner")
            self._control(context, 1148, "Edit", filename_hwnd)
            confirm = self._control(context, 1, "Button", filename_hwnd)
            if confirm["parent"] != filename_hwnd:
                raise DriverError("unexpected_dialog", "Filename IDOK has the wrong direct parent")
            allowed.add(filename_hwnd)
        return allowed

    def _wait_dialog(self, context, control_id, cls, save_scope=None):
        deadline = self.backend.monotonic() + self.wait_timeout
        while self.backend.monotonic() < deadline:
            fresh, state = self._refresh(context)
            dialogs = self._modals(fresh)
            candidates = [dialog for dialog in dialogs if any(w["root"] == dialog["hwnd"] and w["id"] == control_id and w["cls"] == cls and w["visible"] for w in fresh["windows"])]
            if len(candidates) == 1:
                dialog = candidates[0]
                if control_id == 1067 and (dialog["owner"] not in self._save_owners(fresh) or dialog["parent"] != dialog["owner"]):
                    raise DriverError("unexpected_dialog", "Selection dialog owner is not a verified application panel")
                self._guard(fresh, state, modal=dialog["hwnd"], save_scope=save_scope)
                return fresh, dialog, self._control(fresh, control_id, cls, dialog["hwnd"])
            if len(candidates) > 1: raise DriverError("ambiguous_dialog", "Multiple candidate save dialogs found")
            if not state["responsive"]: raise DriverError("unresponsive", "Simulator stopped responding while opening save dialog")
            self.backend.sleep(0.1)
        raise DriverError("save_dialog_timeout", "Expected save control did not appear; no further confirmation sent", control_id=control_id)

    def _confirm(self, context, dialog, save_scope=None):
        fresh, state = self._refresh(context)
        self._guard(fresh, state, modal=dialog["hwnd"], save_scope=save_scope)
        button = self._control(fresh, 1, "Button", dialog["hwnd"])
        if button["parent"] != dialog["hwnd"]:
            raise DriverError("unexpected_dialog", "IDOK is not a direct child of the expected dialog")
        self.backend.post_command(fresh["process"], dialog["hwnd"], 1, button["hwnd"])

    def save_workpiece(self, filename: str):
        with self._lock:
            path = self._output_path(filename)
            context, state = self._capture()
            self._guard(context, state)
            context, state = self._refresh(context)
            self._guard(context, state)
            self.backend.post_command(context["process"], context["main"]["hwnd"], SAVE_AS_ID)
            fresh, selection_dialog, listbox = self._wait_dialog(context, 1067, "ListBox")
            selection = self.backend.select_workpiece(fresh["process"], listbox["hwnd"])
            save_scope = self._make_save_scope(context, fresh, selection_dialog, listbox, selection)
            self._confirm(context, selection_dialog)
            fresh, filename_dialog, edit = self._wait_dialog(context, 1148, "Edit", save_scope=save_scope)
            readback = self.backend.set_text_readback(fresh["process"], edit["hwnd"], str(path))
            if readback != str(path): raise DriverError("filename_readback_mismatch", "Filename readback differs; save was not confirmed")
            # Recheck after editing and immediately before the final confirmation.
            self._output_path(str(path))
            self._confirm(context, filename_dialog, save_scope=save_scope)
            deadline, prior_stat = self.backend.monotonic() + self.wait_timeout, None
            closure_resamples = 0
            while self.backend.monotonic() < deadline:
                fresh, state = self._refresh(context)
                dialogs = self._modals(fresh)
                # Final confirmation has already been posted exactly once. A
                # closed dialog is a successful postcondition, not a stale HWND
                # that should still be interrogated for its old controls.
                if path.is_file() and state["main_enabled"] and state["responsive"] and not dialogs:
                    stat = path.stat()
                    current_stat = (stat.st_size, stat.st_mtime_ns)
                    if stat.st_size > 0 and current_stat == prior_stat:
                        digest = hashlib.sha256(path.read_bytes()).hexdigest()
                        return dict(ok=True, outcome="file_created", path=str(path), bytes=stat.st_size,
                                    sha256=digest, selection=selection, geometry_verified=False,
                                    closure_resamples=closure_resamples)
                    prior_stat = current_stat
                    self.backend.sleep(0.1)
                    continue
                prior_stat = None
                if dialogs:
                    expected_hwnds = {filename_dialog["hwnd"], selection_dialog["hwnd"]}
                    if any(d["hwnd"] not in expected_hwnds for d in dialogs):
                        raise DriverError("unexpected_save_prompt", "A new dialog appeared; overwrite/error prompts will never be confirmed", windows=dialogs)
                    filename_visible = any(d["hwnd"] == filename_dialog["hwnd"] for d in dialogs)
                    try:
                        self._scoped_save_dialogs(fresh, save_scope, filename_dialog["hwnd"] if filename_visible else None)
                    except DriverError as exc:
                        # The verified save windows may disappear between
                        # EnumWindows and a read-only ListBox query. Re-sample;
                        # never repeat Save As, WM_SETTEXT, IDOK or any write.
                        closing = exc.code == "window_changed" or (exc.code == "ambiguous_control" and exc.details.get("count") == 0)
                        if not closing: raise
                        closure_resamples += 1
                        self.backend.sleep(0.1)
                        continue
                if not state["responsive"]: break
                self.backend.sleep(0.1)
            raise DriverError("save_not_verified", "Save confirmation sent, but a stable new file and closed dialog were not both observed", path=str(path), closure_resamples=closure_resamples)
