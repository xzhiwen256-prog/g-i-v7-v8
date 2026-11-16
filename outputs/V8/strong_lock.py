"""Opt-in, stateless-in-velocity position servo. Pure math: no input APIs.

The caller supplies camera/pending-compensated error and fresh-frame timing.
There is no integral or velocity carry, report queue, or frame extrapolation.
Fractional quantization error is retained only while the requested direction
continues. Position correction is exponential and bounded by the compensated
error; velocity feedforward is separate so moving targets are not dead-zoned.

This law is more direct, not universally more stable: incorrect feedback/gain
estimates and noisy detections can increase overshoot. It is an explicit opt-in.
"""
import math


def _finite(name, value, *, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)):
        raise ValueError(name + ' must be a finite' + (' positive' if positive else '') + ' number')
    return float(value)


def _vector(name, values):
    try:
        values = tuple(values)
    except TypeError as exc:
        raise ValueError(name + ' must contain two finite numbers') from exc
    if len(values) != 2:
        raise ValueError(name + ' must contain two finite numbers')
    return tuple(_finite(name, value) for value in values)


class StrongLockServo:
    """One fresh-frame error -> one bounded integer relative command.

    The far-field smoothstep covers 20..80 compensated pixels. Chase shortens
    the user response time there; it does not change px-to-count calibration.
    Within 20 px the exact user response is used (no 20/12/80 ms floor).
    Both axes keep the historical independent limit, not a smaller radial cap.
    """
    revision = 'direct-pending-servo-20261111'

    def __init__(self):
        self.reset()

    def reset(self):
        self.remainder = [0., 0.]
        self.previous_drive = [0., 0.]
        self.last_output = (0, 0)
        self.position_counts = [0., 0.]
        self.feedforward_counts = [0., 0.]
        self.requested_counts = [0., 0.]
        self.saturated = [False, False]
        self.direction_resets = [False, False]
        self.time_constant_ms = 0.
        self.chase_multiplier = 1.
        self.alpha = 0.
        self.budget = 0.
        self.dt_ms = 0.
        self.assistance = 1.
        self.steps = 0

    def step(self, error, feedforward, dt, gain, limit, response_ms,
             chase_strength=1., assistance=1., stopped=(False, False)):
        # Validate and compute all continuous values before committing state.
        error = _vector('error', error)
        feedforward = _vector('feedforward', feedforward)
        dt = _finite('dt', dt, positive=True)
        gain = _finite('gain', gain, positive=True)
        response_ms = _finite('response_ms', response_ms, positive=True)
        chase_strength = _finite('chase_strength', chase_strength, positive=True)
        assistance = _finite('assistance', assistance)
        if not 1. <= chase_strength <= 6.:
            raise ValueError('chase_strength must be between 1 and 6')
        if not 0. <= assistance <= 1.:
            raise ValueError('assistance must be between 0 and 1')
        if type(limit) is not int or not 1 <= limit <= 480:
            raise ValueError('limit must be an integer between 1 and 480')
        try:
            stopped = tuple(stopped)
        except TypeError as exc:
            raise ValueError('stopped must contain two booleans') from exc
        if len(stopped) != 2 or any(type(v) is not bool for v in stopped):
            raise ValueError('stopped must contain two booleans')
        fraction = max(0., min(1., (math.hypot(*error) - 20.) / 60.))
        blend = fraction * fraction * (3. - 2. * fraction)
        multiplier = 1. + (chase_strength - 1.) * blend
        tau_ms = response_ms / multiplier
        if tau_ms / 1000. <= 0.:
            raise ValueError('response_ms is too small')
        alpha = -math.expm1(-dt / (tau_ms / 1000.))
        budget = min(float(limit), limit * dt * 60.)
        integer_budget = math.floor(budget)
        position = [gain * error[a] * alpha for a in range(2)]
        ff = [gain * feedforward[a] * dt for a in range(2)]
        requested = [0. if stopped[a] else assistance * (position[a] + ff[a]) for a in range(2)]
        if any(not math.isfinite(v) for v in (*position, *ff, *requested)):
            raise ValueError('Computed command must remain finite')
        remainder = list(_vector('remainder', self.remainder))
        outputs = []
        saturation = []
        direction_resets = []
        for axis, drive in enumerate(requested):
            # A new direction cannot inherit an old positive/negative fraction.
            reverse = drive * self.previous_drive[axis] < 0.
            direction_resets.append(reverse)
            if stopped[axis] or drive == 0. or reverse:
                remainder[axis] = 0.
            if drive == 0.:
                outputs.append(0)
                saturation.append(False)
                continue
            exact = drive + remainder[axis]
            if not math.isfinite(exact):
                raise ValueError('Quantized command must remain finite')
            saturated = abs(exact) > budget
            bounded = max(-budget, min(budget, exact))
            count = max(-integer_budget, min(integer_budget, round(bounded)))
            # Do not retain saturation debt: it is neither velocity nor a queue.
            remainder[axis] = 0. if saturated else bounded - count
            outputs.append(count)
            saturation.append(saturated)
        self.remainder = remainder
        self.previous_drive = requested
        self.last_output = tuple(outputs)
        self.position_counts = position
        self.feedforward_counts = ff
        self.requested_counts = requested
        self.saturated = saturation
        self.direction_resets = direction_resets
        self.time_constant_ms = tau_ms
        self.chase_multiplier = multiplier
        self.alpha = alpha
        self.budget = budget
        self.dt_ms = dt * 1000.
        self.assistance = assistance
        self.steps += 1
        return self.last_output

    def snapshot(self):
        return dict(revision=self.revision, output_counts=list(self.last_output),
                    requested_counts=list(self.requested_counts),
                    position_counts=list(self.position_counts),
                    feedforward_counts=list(self.feedforward_counts),
                    remainder_counts=list(self.remainder), saturated=list(self.saturated),
                    direction_resets=list(self.direction_resets), dt_ms=self.dt_ms,
                    time_constant_ms=self.time_constant_ms, chase_multiplier=self.chase_multiplier,
                    exponential_fraction=self.alpha, per_axis_budget_counts=self.budget,
                    subcount_budget=0. < self.budget < 1., assistance_weight=self.assistance,
                    velocity_carry=False, integral=False, report_queue=False, steps=self.steps,
                    scope='requested integer counts only; no device/game response measurement')
