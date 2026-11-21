"""Optional, observational Frida provenance for the pinned MF continuous worker.

The hook locations and event sequence are specific to the pinned MF build.
No flags, controller parameters, or private application functions are written or
called. Frida Interceptor itself installs instrumentation in the target process.
This module never starts machining. Construction/status-before-arm import no SDK.
"""
import copy
import importlib
import json
import ntpath
import sys
import threading
import uuid

from constants import TARGET_PATH, TARGET_SHA256
from errors import DriverError


EVIDENCE = {
    "target_sha256": TARGET_SHA256,
    "worker_rva": "0x2f40d0",
    "continuous_loop_rva": "0x2ed8c0",
    "m_apply_rva": "0x239760",
    "run_origin_call_rva": "0x2ede08",
    "run_origin_return_rva": "0x2ede0d",
    "m30_branch_rva": "0x2398c6",
    "normal_drain_rva": "0x2ee400",
    "early_exit_rva": "0x2ef657",
    "worker_flag_clear_rva": "0x2f42f7",
    "scope": "The observed continuous worker reached its normal program end; geometry is separate.",
}


def _number(value):
    if type(value) is int: return value
    if isinstance(value, str) and len(value) <= 32:
        try: return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError: pass
    return None


def _program_key(state):
    if not isinstance(state, dict): return None
    doc, array, count = (_number(state.get(name)) for name in ("document", "descriptor_array", "count"))
    if (doc is None or array is None or count is None or not 0x10000 <= doc <= 0xffffffff or
            not 0x10000 <= array <= 0xffffffff or not 1 <= count <= 10_000_000 or state.get("coherent") is not True):
        return None
    return doc, array, count


class CompletionEvidence:
    """Bounded event reducer. It does not import Frida, touch the OS, or lock Driver."""
    def __init__(self, process, nonce, base, max_events=1024):
        if type(max_events) is not int or not 9 <= max_events <= 4096:
            raise ValueError("max_events must be between 9 and 4096")
        self.process, self.nonce, self.base = dict(process), nonce, base
        self.max_events = max_events
        self.events, self.fault, self.worker = [], None, None
        self.ready, self.last_seq, self.rejected = False, 0, 0

    def fail(self, code, message):
        if self.fault is None: self.fault = dict(code=code, message=str(message)[:512])

    def accept(self, event):
        if not isinstance(event, dict):
            self.rejected += 1
            return False
        if (event.get("nonce") != self.nonce or event.get("pid") != self.process["pid"] or
                str(event.get("creation_time")) != str(self.process["creation_time"])):
            self.rejected += 1
            return False
        if len(self.events) >= self.max_events:
            self.fail("event_limit", "Observer event bound reached; completion after lost events is unknown")
            return False
        seq = event.get("seq")
        if type(seq) is not int or seq != self.last_seq + 1:
            self.rejected += 1
            self.fail("event_order", "Observer event sequence is missing, duplicated or out of order")
            return False
        self.last_seq = seq
        # Keep only the producer's bounded evidence schema, never arbitrary data.
        retained = {name: event[name] for name in
                    ("event", "seq", "pid", "creation_time", "tid", "worker_id", "state",
                     "caller_rva", "mcode", "call_id", "block_address", "at") if name in event}
        try:
            if len(json.dumps(retained)) > 8192: raise ValueError("event too large")
        except (TypeError, ValueError):
            self.fail("invalid_event", "Observer payload exceeded the bounded evidence schema")
            return False
        self.events.append(copy.deepcopy(retained))
        if self.fault: return False
        name, state = event.get("event"), event.get("state", {})
        if name in ("read_error", "observer_error", "limit_reached"):
            self.fail("event_limit" if name == "limit_reached" else "observer_read_error",
                      event.get("error", name))
            return False
        if name == "ready":
            if (self.ready or _number(event.get("base")) != self.base or
                    ntpath.normcase(event.get("path", "")) != ntpath.normcase(TARGET_PATH) or
                    state.get("worker") != 0 or state.get("cycle") != 0 or state.get("coherent") is not True):
                self.fail("observer_not_idle", "Observer ready event did not confirm the pinned idle module")
                return False
            self.ready = True
            return True
        if not self.ready:
            self.fail("observer_not_ready", "Execution occurred before the observer completed its idle handshake")
            return False
        tid, worker_id = event.get("tid"), event.get("worker_id")
        if name == "worker_enter":
            if type(tid) is not int or tid <= 0 or type(worker_id) is not int or worker_id <= 0:
                self.fail("invalid_worker", "Worker event did not identify a native thread and instance")
                return False
            if self.worker and (self.worker["phase"] not in ("completed", "exited") or worker_id <= self.worker["id"]):
                self.fail("overlapping_workers", "Overlapping or reused worker identities cannot establish one completion chain")
                return False
            key = _program_key(state)
            if key is None:
                self.fail("invalid_program_identity", "Worker program storage was not readable/coherent")
                return False
            self.worker = dict(id=worker_id, tid=tid, phase="entered", program_key=key,
                               proof={"worker_enter": seq}, call=None, invalid=False)
            return True
        run = self.worker
        if not run or worker_id != run["id"] or tid != run["tid"]:
            self.rejected += 1
            return False
        if _program_key(state) != run["program_key"]:
            run["invalid"] = True
            self.fail("program_changed", "Program storage changed within the observed worker")
            return False
        if state.get("emergency", 0): run["invalid"] = True
        if name == "early_exit":
            run["invalid"] = True
            run["proof"]["early_exit"] = seq
            return True
        if name == "loop_enter":
            if run["phase"] != "entered": run["invalid"] = True
            run["phase"] = "loop"
        elif name == "m_apply":
            if (run["phase"] != "loop" or event.get("mcode") != 30 or
                    _number(event.get("caller_rva")) != 0x2EDE0D or
                    type(event.get("call_id")) is not int or not event["call_id"] > 0 or
                    not _number(event.get("block_address"))):
                run["invalid"] = True
                self.rejected += 1
                return False
            run["call"] = (event["call_id"], _number(event["block_address"]))
            run["phase"] = "m_apply"
        elif name in ("m30_branch", "m_apply_return"):
            previous = "m_apply" if name == "m30_branch" else "m30_branch"
            if (run["phase"] != previous or event.get("mcode") != 30 or
                    _number(event.get("caller_rva")) != 0x2EDE0D or
                    (event.get("call_id"), _number(event.get("block_address"))) != run["call"]):
                run["invalid"] = True
                self.rejected += 1
                return False
            run["phase"] = name
        elif name == "normal_drain":
            if run["phase"] != "m_apply_return": run["invalid"] = True
            run["phase"] = "drained"
        elif name == "loop_leave":
            if run["phase"] != "drained": run["invalid"] = True
            run["phase"] = "loop_left"
        elif name == "worker_exit":
            if (run["phase"] != "loop_left" or state.get("worker") != 0 or
                    state.get("cycle") != 0): run["invalid"] = True
            run["phase"] = "exited" if run["invalid"] else "completed"
        else:
            self.rejected += 1
            return False
        run["proof"][name] = seq
        return True

    def snapshot(self):
        completed = bool(self.ready and not self.fault and self.worker and self.worker["phase"] == "completed")
        return dict(completed=completed, ready=self.ready, worker=copy.deepcopy(self.worker),
                    fault=copy.deepcopy(self.fault), event_count=len(self.events), rejected_events=self.rejected,
                    events=copy.deepcopy(self.events), geometry_verified=False,
                    full_toolpath_from_first_line_verified=False, provenance=dict(EVIDENCE))


def _script_source(process, nonce, base, max_events):
    config = dict(nonce=nonce, pid=process["pid"], creation_time=str(process["creation_time"]),
                  path=TARGET_PATH, base=hex(base), max_events=max_events)
    return "'use strict';\nconst CFG = " + json.dumps(config) + ";\n" + r"""
if (Process.id !== CFG.pid || Process.arch !== 'ia32' || Process.pointerSize !== 4)
  throw new Error('Unexpected process architecture/identity');
const module = Process.getModuleByName('sscnc.exe');
const b = module.base;
const normalized = p => String(p).replace(/\//g, '\\').toLowerCase();
if (normalized(module.path) !== normalized(CFG.path) || b.toString() !== CFG.base)
  throw new Error('Pinned module identity/base mismatch');
let sequence=0, workerSerial=0, callSerial=0, limitSent=false;
const workers = new Map(), activeCalls = new Map();
function snapshot() {
  const d=b.add(0x5cb178).readPointer();
  const array=d.isNull() ? ptr(0) : d.add(0x3f0c).readPointer();
  const count=d.isNull() ? 0 : d.add(0x3f10).readS32();
  return {document:d.toString(), descriptor_array:array.toString(), count:count,
    index:d.isNull() ? null : d.add(0xd8).readS32(),
    source_line:d.isNull() ? null : d.add(0xe0).readS32(),
    worker:b.add(0x5cc83e).readU8(), cycle:b.add(0x5c6924).readU32(),
    emergency:b.add(0x5cb458).readU32(),
    coherent:d.equals(b.add(0x5cb178).readPointer()) && (d.isNull() ||
      (array.equals(d.add(0x3f0c).readPointer()) && count===d.add(0x3f10).readS32()))};
}
function emit(event) {
  if (sequence>=CFG.max_events) {
    if (!limitSent) {limitSent=true; send({event:'limit_reached', seq:++sequence,
      nonce:CFG.nonce,pid:Process.id,creation_time:CFG.creation_time});}
    return;
  }
  send(Object.assign({nonce:CFG.nonce,pid:Process.id,creation_time:CFG.creation_time,
    seq:++sequence,at:Date.now()},event));
}
function note(name, tid, id, extra) {
  try {emit(Object.assign({event:name,tid:tid,worker_id:id,state:snapshot()},extra||{}));}
  catch(error) {emit({event:'read_error',tid:tid,worker_id:id,error:String(error)});}
}
const initial=snapshot();
if (initial.worker!==0 || initial.cycle!==0 || !initial.coherent)
  throw new Error('Simulator began execution before hooks could be armed');
Interceptor.attach(b.add(0x2f40d0), {
  onEnter() {
    this.tid=Process.getCurrentThreadId(); this.id=++workerSerial;
    if (workers.has(this.tid)) emit({event:'observer_error',error:'Nested worker entry'});
    workers.set(this.tid,this.id); note('worker_enter',this.tid,this.id);
  },
  onLeave() {note('worker_exit',this.tid,this.id); workers.delete(this.tid); activeCalls.delete(this.tid);}
});
Interceptor.attach(b.add(0x2ed8c0), {
  onEnter() {this.tid=Process.getCurrentThreadId();this.id=workers.get(this.tid)||null;
    note('loop_enter',this.tid,this.id);},
  onLeave() {note('loop_leave',this.tid,this.id);}
});
Interceptor.attach(b.add(0x239760), {
  onEnter(args) {
    this.tid=Process.getCurrentThreadId();this.id=workers.get(this.tid)||null;
    try {
      this.item={call_id:++callSerial,caller_rva:this.returnAddress.sub(b).toString(),
        block_address:args[0].toString(),mcode:args[0].add(0xd8).readS32()};
      const stack=activeCalls.get(this.tid)||[];stack.push(this.item);activeCalls.set(this.tid,stack);
      if(this.item.mcode===30) note('m_apply',this.tid,this.id,this.item);
    } catch(error) {emit({event:'read_error',error:String(error)});}
  },
  onLeave() {
    if(this.item && this.item.mcode===30) note('m_apply_return',this.tid,this.id,this.item);
    const stack=activeCalls.get(this.tid)||[];
    if(this.item && stack[stack.length-1]===this.item) stack.pop();
    else emit({event:'observer_error',error:'M apply call stack mismatch'});
    if(!stack.length) activeCalls.delete(this.tid);
  }
});
Interceptor.attach(b.add(0x2398c6), {onEnter() {
  const tid=Process.getCurrentThreadId(), stack=activeCalls.get(tid)||[], item=stack[stack.length-1];
  try {
    const block=this.context.esi;
    const evidence=Object.assign({},item||{},{block_address:block.toString(),mcode:block.add(0xd8).readS32()});
    note('m30_branch',tid,workers.get(tid)||null,evidence);
  } catch(error) {emit({event:'read_error',error:String(error)});}
}});
for(const pair of [['normal_drain',0x2ee400],['early_exit',0x2ef657]]) {
  Interceptor.attach(b.add(pair[1]), {onEnter() {const tid=Process.getCurrentThreadId();
    note(pair[0],tid,workers.get(tid)||null);}});
}
emit({event:'ready',base:b.toString(),path:module.path,state:snapshot()});
"""


class CompletionMonitor:
    """Explicit arm/status/close; no automatic attach, launch or dependency install."""
    def __init__(self, driver, max_events=1024, ready_timeout=3.0):
        if type(max_events) is not int or not 9 <= max_events <= 4096:
            raise ValueError("max_events must be between 9 and 4096")
        if not 0 < ready_timeout <= 10: raise ValueError("ready_timeout must be within (0, 10]")
        self.driver, self.max_events, self.ready_timeout = driver, max_events, ready_timeout
        self._events_lock, self._operations = threading.RLock(), threading.RLock()
        self._ready_signal = threading.Event()
        self._session = self._script = self._evidence = self._target = None
        self._nonce, self._closing, self._closed = None, False, False
        self._transport_detached = False
        self._script_destroyed = False
        self._cleanup_errors = []

    def _message(self, nonce, message, data=None):
        # Frida callback: only bounded reduction under its own small lock. Never
        # call driver, wait on its lock, perform IO, unload, or detach here.
        with self._events_lock:
            if nonce != self._nonce or self._closing or self._evidence is None: return
            try:
                if message.get("type") == "send":
                    self._evidence.accept(message.get("payload"))
                else:
                    self._evidence.fail("observer_script_error", message.get("description", "Script error"))
            except Exception as exc:
                self._evidence.fail("observer_event_error", str(exc))
            if self._evidence.ready or self._evidence.fault: self._ready_signal.set()

    def _detached(self, nonce, reason, crash=None):
        with self._events_lock:
            if nonce != self._nonce: return
            self._transport_detached = True
            if not self._closing and self._evidence:
                self._evidence.fail("observer_detached", "Observer detached: " + str(reason))
                self._ready_signal.set()

    def _destroyed(self, nonce):
        with self._events_lock:
            if nonce != self._nonce: return
            self._script_destroyed = True
            if not self._closing and self._evidence:
                self._evidence.fail("observer_script_destroyed", "Observer script was destroyed while its session remained attached")
                self._ready_signal.set()

    def _poll_transport(self):
        # 17.27.4 exposes these as immediate state properties, not script RPC.
        # They also cover a signal queued but not yet delivered to Python.
        if self._session is not None and self._session.is_detached:
            self._detached(self._nonce, "state-property")
        if self._script is not None and self._script.is_destroyed:
            self._destroyed(self._nonce)

    def _view(self, outcome=None):
        with self._events_lock:
            result = self._evidence.snapshot() if self._evidence else dict(
                completed=False, ready=False, events=[], event_count=0, fault=None,
                geometry_verified=False, full_toolpath_from_first_line_verified=False, provenance=dict(EVIDENCE))
            attached = self._session is not None and not self._transport_detached
            loaded = self._script is not None and not self._script_destroyed
            active = attached and loaded and not self._closing and not self._closed and not result["fault"] and result["ready"]
            result["execution_completion_observed"] = result["completed"]
            result["completed"] = bool(result["completed"] and active)
            state = ("unarmed" if self._target is None else "cleanup_pending" if self._closing and attached else
                     "closed" if self._closed else
                     "observer_fault" if result["fault"] else "completed" if result["completed"] else
                     "observing" if result.get("worker") else "armed")
            return dict(result, ok=result["fault"] is None and state != "cleanup_pending",
                        attached=attached, instrumentation_loaded=loaded, observer_active=bool(active), state=state,
                        outcome=outcome or state, target=copy.deepcopy(self._target),
                        cleanup_errors=copy.deepcopy(self._cleanup_errors))

    def arm(self):
        # Always driver lock -> operation lock; callbacks take neither.
        with self.driver._lock, self._operations:
            self._poll_transport()
            if self._session is not None and self._transport_detached:
                self._dispose()  # Already detached: discard stale Python handles, no new native action.
            if self._session is not None:
                if self._closing or self._script_destroyed or (self._evidence and self._evidence.fault):
                    raise DriverError("observer_cleanup_required", "Close the previous observer session before arming again",
                                      status=self._view())
                result = self.status()
                return dict(result, outcome="already_armed")
            context, state = self.driver._capture()
            self.driver._guard(context, state)
            try:
                frida = importlib.import_module("frida")
            except ImportError as exc:
                raise DriverError("dependency_missing", "Optional Frida Python SDK is not installed in this MCP runtime",
                                  package="frida", action="No attach or installation was attempted") from exc
            context, state = self.driver._refresh(context)
            self.driver._guard(context, state)
            process, base = dict(context["process"]), context["base"]
            self.driver.backend._verify(process)
            nonce = uuid.uuid4().hex
            with self._events_lock:
                self._nonce, self._closed, self._closing = nonce, False, False
                self._transport_detached = False
                self._script_destroyed = False
                self._cleanup_errors = []
                self._target = dict(process=process, module_base=hex(base), sha256=TARGET_SHA256)
                self._evidence = CompletionEvidence(process, nonce, base, self.max_events)
                self._ready_signal.clear()
            try:
                self._session = frida.attach(process["pid"])
                self.driver.backend._verify(process)
                self._session.on("detached", lambda reason, crash=None: self._detached(nonce, reason, crash))
                self._script = self._session.create_script(_script_source(process, nonce, base, self.max_events))
                # The SDK defaults console.info/log output to stdout. Preserve the
                # stdio MCP channel even if a diagnostic is emitted by the agent.
                self._script.set_log_handler(lambda level, text: print(str(text)[:2048], file=sys.stderr))
                self._script.on("destroyed", lambda: self._destroyed(nonce))
                self._script.on("message", lambda message, data=None: self._message(nonce, message, data))
                self._script.load()
                if not self._ready_signal.wait(self.ready_timeout):
                    raise DriverError("observer_ready_timeout", "Observer did not confirm readiness within its deadline")
                self.driver.backend._verify(process)
                result = self._view("armed")
                if result["fault"] or not result["ready"]:
                    raise DriverError("observer_not_ready", "Observer could not establish a pinned idle session", evidence=result)
                return result
            except Exception as exc:
                cleanup = self._dispose()
                with self._events_lock:
                    self._cleanup_errors = cleanup
                    self._closed = self._session is None
                    self._evidence.fail("observer_arm_failed", str(exc))
                if isinstance(exc, DriverError):
                    details = dict(exc.details, cleanup_errors=cleanup, cleanup_pending=not self._closed)
                    raise DriverError(exc.code, str(exc), **details) from exc
                raise DriverError("observer_attach_failed", "Could not arm the optional local observer",
                                  reason=str(exc), cleanup_errors=cleanup) from exc

    def status(self):
        with self.driver._lock, self._operations:
            self._poll_transport()
            if self._session is not None and not self._closed and not self._transport_detached:
                try:
                    context = self.driver._discover()  # exact exe/hash, PID/starttime and base
                    if (context["process"] != self._target["process"] or
                            hex(context["base"]) != self._target["module_base"]):
                        raise DriverError("process_changed", "Observed simulator identity changed")
                except DriverError as exc:
                    with self._events_lock: self._evidence.fail("process_changed", str(exc))
            return self._view()

    def _dispose(self):
        errors = []
        with self._events_lock:
            self._closing = True
        self._poll_transport()
        with self._events_lock:
            if self._transport_detached:
                self._script = self._session = None
                return errors
            if self._script_destroyed: self._script = None
        if self._script is not None:
            try: self._script.unload(); self._script = None
            except Exception as exc: errors.append(dict(operation="unload", reason=str(exc)[:512]))
        if self._session is not None:
            try: self._session.detach(); self._session = self._script = None
            except Exception as exc: errors.append(dict(operation="detach", reason=str(exc)[:512]))
        return errors

    def close(self):
        with self.driver._lock, self._operations:
            if self._session is None:
                self._closed = self._target is not None
                return self._view("already_closed")
            errors = self._dispose()
            self._cleanup_errors = errors
            self._closed = self._session is None
            result = self._view("closed" if self._closed else "close_incomplete")
            if errors: result.update(ok=False, cleanup_errors=errors)
            return result
