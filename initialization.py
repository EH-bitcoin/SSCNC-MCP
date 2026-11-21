"""Guarded preparation for the hash-pinned FANUC0iMF simulator.

RVA/control provenance: mf_static/mf_addresses.json, initialization.reference
and globals.home_busy_guard. RESET 7045 was identified on the live controller
fingerprint and must still read back as RESET at dispatch. No process memory
is written; commands use the application's existing buttons.

Reference-complete flags have not been derived. Coordinate zero plus an idle
home guard is an observation, not independent proof of a completed home cycle.
"""
import math
import struct

from errors import DriverError


REFERENCE_MODE = 7
AXIS_CONTROLS = {'x': (0, 9582), 'y': (1, 9592), 'z': (2, 9602)}
POSITIVE_CONTROL = 9586
RESET_CONTROL = 7045
HOME_BUSY_RVA = 0x5CC7FC
SELECTED_AXIS_RVA = 0x5CC7D4
MACHINE_XYZ_RVA = 0x5CC998
TOTAL_TIMEOUT = 55.0
AXIS_TIMEOUT = 20.0
ZERO_TOLERANCE_MM = 1e-6
STABLE_SECONDS = 0.3
POLL_SECONDS = 0.1

LIMITATIONS = [
    'Independent reference-complete flags have not been derived or verified.',
    'XYZ are sampled coordinate globals and may update only at motion endpoints.',
    'Window commands and read-only samples are not an atomic controller transaction.',
]


def read_reference_state(read, base):
    """Decode only the verified MF globals via a caller-supplied read function."""
    if type(base) is not int or base <= 0 or base + MACHINE_XYZ_RVA + 24 > 0x100000000:
        raise DriverError('invalid_module_base', 'A valid 32-bit module base is required')

    def unpack(rva, format_string):
        size = struct.calcsize(format_string)
        data = read(base + rva, size)
        if len(data) != size:
            raise DriverError('reference_state_unreadable', 'Reference-state read was incomplete', rva=hex(rva))
        return struct.unpack(format_string, data)

    def sample():
        busy = unpack(HOME_BUSY_RVA, '<I')[0]
        axis = unpack(SELECTED_AXIS_RVA, '<I')[0]
        xyz = list(unpack(MACHINE_XYZ_RVA, '<3d'))
        if axis not in (0, 1, 2) or not all(math.isfinite(value) for value in xyz):
            raise DriverError('reference_state_invalid', 'Reference-state values are outside the verified domain',
                              selected_axis=axis, xyz_mm=xyz)
        return dict(home_busy=busy, selected_axis=axis, xyz_mm=xyz)

    first, second = sample(), sample()
    return dict(second, coherent=first == second)


def _power(raw):
    value = raw.get('power')
    if value == 0:
        raise DriverError('power_off', 'Controller power is off; no verified power-on button is available')
    if value != 1:
        raise DriverError('power_unknown', 'Controller power is not established; no power command will be guessed', observed=value)


def _observe(driver, context=None, *, allow_emergency=False, reference=False, home_idle=False):
    if context is None:
        context, state = driver._capture()
    else:
        context, state = driver._refresh(context)
    driver._guard(context, state, allow_emergency=allow_emergency)
    _power(state['telemetry'])
    if reference and state['telemetry']['mode'] != REFERENCE_MODE:
        raise DriverError('wrong_mode', 'Reference mode 7 is required for axis preparation',
                          observed_mode=state['telemetry']['mode'])
    observed = driver.backend.read_with(context['process'], context['base'], read_reference_state)
    if not observed['coherent']:
        raise DriverError('reference_state_changing', 'Reference-state values changed during the read; no next command sent')
    if home_idle and observed['home_busy']:
        raise DriverError('home_busy', 'An existing reference motion may be active; no command sent', observed=observed)
    return context, state, observed


def _remaining(driver, deadline, code='initialization_timeout'):
    remaining = deadline - driver.backend.monotonic()
    if remaining <= 0:
        raise DriverError(code, 'The bounded controller preparation interval expired')
    return remaining


def _bounded_call(driver, deadline, function, *args):
    """Keep existing preparation methods inside this operation's time budget."""
    previous = driver.wait_timeout
    if not isinstance(previous, (int, float)) or not math.isfinite(previous) or previous <= 0:
        raise DriverError('invalid_wait_timeout', 'wait_timeout must be a finite positive number')
    driver.wait_timeout = min(previous, _remaining(driver, deadline))
    try:
        return function(*args)
    finally:
        driver.wait_timeout = previous


def _standard_button(driver, context, control_id, expected_axis=None):
    context, state, observed = _observe(driver, context, reference=True, home_idle=True)
    if expected_axis is not None and observed['selected_axis'] != expected_axis:
        raise DriverError('axis_selection_not_verified', 'The selected axis changed before reference dispatch',
                          expected_axis=expected_axis, observed=observed)
    panels = driver._panels(context)
    if len(panels) != 1:
        raise DriverError('ambiguous_panel', 'One verified standard controller panel is required', count=len(panels))
    control = driver._control(context, control_id, 'Button', panels[0]['root'])
    if control['parent'] != panels[0]['hwnd']:
        raise DriverError('window_changed', 'Reference button is not a direct child of the standard panel')
    driver.backend.post_command(context['process'], control['parent'], control_id, control['hwnd'])
    if control_id == POSITIVE_CONTROL:
        driver._paused = driver._run = None
    return context, state, observed


def _select_axis(driver, context, axis, deadline):
    axis_value, control_id = AXIS_CONTROLS[axis]
    context, _, observed = _standard_button(driver, context, control_id)
    selection_deadline = min(deadline, driver.backend.monotonic() + driver.wait_timeout)
    while driver.backend.monotonic() < selection_deadline:
        context, _, observed = _observe(driver, context, reference=True, home_idle=True)
        if observed['selected_axis'] == axis_value:
            return context
        driver.backend.sleep(min(POLL_SECONDS, selection_deadline - driver.backend.monotonic()))
    raise DriverError('axis_selection_not_verified', 'The requested axis was not read back; no positive motion sent',
                      axis=axis, observed=observed)


def _home_axis(driver, context, axis, deadline):
    axis_value = AXIS_CONTROLS[axis][0]
    context = _select_axis(driver, context, axis, deadline)
    _remaining(driver, deadline)
    context, _, before = _standard_button(driver, context, POSITIVE_CONTROL, expected_axis=axis_value)
    dispatched_at = driver.backend.monotonic()
    axis_deadline = min(deadline, dispatched_at + AXIS_TIMEOUT)
    effect_interval = max(STABLE_SECONDS, min(driver.wait_timeout, 2.0))
    stable_since = None
    busy_seen = moved = False
    observed = before
    while driver.backend.monotonic() < axis_deadline:
        try:
            context, state, observed = _observe(driver, context, reference=True)
        except DriverError as exc:
            exc.details.update(axis=axis, positive_command_sent=True, homing_may_be_active=True)
            raise
        if observed['selected_axis'] != axis_value:
            raise DriverError('axis_changed_during_home', 'Axis selection changed after reference dispatch',
                              axis=axis, observed=observed, homing_may_be_active=True)
        busy_seen |= bool(observed['home_busy'])
        moved |= abs(observed['xyz_mm'][axis_value] - before['xyz_mm'][axis_value]) > ZERO_TOLERANCE_MM
        zero = abs(observed['xyz_mm'][axis_value]) <= ZERO_TOLERANCE_MM
        now = driver.backend.monotonic()
        if zero and not observed['home_busy']:
            if stable_since is None:
                stable_since = now
            if now - stable_since >= STABLE_SECONDS:
                if busy_seen or moved:
                    return context, state, dict(axis=axis, before=before, after=observed,
                        stable_zero_observed=True, stable_seconds=now - stable_since,
                        home_busy_seen=busy_seen, coordinate_change_seen=moved,
                        command_effect_observed=True)
                if now - dispatched_at >= effect_interval:
                    raise DriverError('home_effect_unconfirmed',
                        'Coordinate was already zero and no reference activity was observed; no next axis sent',
                        axis=axis, observed=observed, positive_command_sent=True, homing_may_be_active=True)
        else:
            stable_since = None
        driver.backend.sleep(min(POLL_SECONDS, axis_deadline - now))
    raise DriverError('home_timeout', 'Axis zero and an idle home guard were not stably observed; no next axis sent',
                      axis=axis, observed=observed, homing_may_be_active=True,
                      home_busy_seen=busy_seen, coordinate_change_seen=moved)


def initialize_controller(driver, home_axes=False):
    """Release emergency and select REF; optionally observe Z, X, Y homes.

    This never guesses a power button, resets a program, starts a cut, or sends
    a later axis when an earlier home has not met the observed zero criteria.
    On a timeout the simulator may still be homing; callers must inspect it.
    """
    if type(home_axes) is not bool:
        raise DriverError('invalid_home_axes', 'home_axes must be a boolean')
    with driver._lock:
        deadline = driver.backend.monotonic() + TOTAL_TIMEOUT
        context, _, initial = _observe(driver, allow_emergency=True, home_idle=True)
        emergency = _bounded_call(driver, deadline, driver._set_emergency, False, context)
        context, _, _ = _observe(driver, context, home_idle=True)
        mode = _bounded_call(driver, deadline, driver._set_mode, 'reference', context)
        context, state, observed = _observe(driver, context, reference=True, home_idle=True)
        axes = []
        if home_axes:
            for axis in ('z', 'x', 'y'):
                _remaining(driver, deadline)
                context, state, result = _home_axis(driver, context, axis, deadline)
                axes.append(result)
            context, state, observed = _observe(driver, context, reference=True, home_idle=True)
            if not all(abs(value) <= ZERO_TOLERANCE_MM for value in observed['xyz_mm']):
                raise DriverError('home_zero_changed', 'Previously observed axis zero changed before final readback', observed=observed)
        return dict(ok=True, outcome='home_positions_observed' if home_axes else 'preparation_verified_no_homing',
                    preparation=dict(power_on_observed=True, emergency=emergency['outcome'], mode=mode['outcome']),
                    homing=dict(requested=home_axes, initial=initial, final=observed, axes=axes,
                                stable_zero_observed=bool(home_axes), independent_reference_flags_verified=False),
                    status=state, limitations=list(LIMITATIONS))


def _reset_control(driver, context):
    panels = driver._controller_panels(context)
    if len(panels) != 1:
        raise DriverError('reset_control_unverified', 'Exactly one fingerprinted controller panel is required', count=len(panels))
    control = driver._control(context, RESET_CONTROL, 'Button', panels[0]['root'])
    if control['parent'] != panels[0]['hwnd']:
        raise DriverError('reset_control_unverified', 'RESET is not a direct child of the fingerprinted controller panel')
    text = driver.backend.read_text(context['process'], control['hwnd'], limit=128)
    if text.strip().upper() != 'RESET':
        raise DriverError('reset_control_unverified', 'Button 7045 does not read back as RESET', text=text)
    return control


def _program_identity(raw):
    return raw.get('document'), raw.get('descriptor_count'), raw.get('descriptor_array')


def reset_program(driver):
    """Send verified RESET and report program-index observation, not full reset."""
    with driver._lock:
        context, _, _ = _observe(driver, allow_emergency=True, home_idle=True)
        _reset_control(driver, context)
        context, state, observed = _observe(driver, context, allow_emergency=True, home_idle=True)
        control = _reset_control(driver, context)
        before = state['telemetry']
        driver.backend.post_command(context['process'], control['parent'], RESET_CONTROL, control['hwnd'])
        driver._paused = driver._run = None
        timeout = driver.wait_timeout
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            timeout = 0.0
        deadline = driver.backend.monotonic() + min(timeout, 8.0)
        stable_since = None
        rewound = False
        program_changed = False
        while driver.backend.monotonic() < deadline:
            context, state, observed = _observe(driver, context, allow_emergency=True, home_idle=True)
            current = state['telemetry']
            program_changed |= _program_identity(current) != _program_identity(before)
            eligible = (bool(before.get('document')) and bool(before.get('descriptor_count')) and
                        before.get('index', -1) > 0 and not program_changed and current.get('index') == 0)
            now = driver.backend.monotonic()
            if eligible:
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= 0.2:
                    rewound = True
                    break
            else:
                stable_since = None
            driver.backend.sleep(min(POLL_SECONDS, deadline - now))
        return dict(ok=True, outcome='program_index_rewind_observed' if rewound else 'command_sent_effect_unconfirmed',
                    reset=dict(command_sent=True, control_id=RESET_CONTROL, index_before=before.get('index'),
                               index_after=state['telemetry'].get('index'), index_rewind_observed=rewound,
                               program_identity_changed=program_changed, full_controller_reset_verified=False),
                    status=state, reference_state=observed,
                    limitations=['A RESET button dispatch or program-index change does not prove every controller reset effect.',
                                 'If the index was already zero, this observation cannot establish a RESET effect.'])
