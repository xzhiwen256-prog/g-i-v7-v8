"""Experimental joint camera-response/delay model for opt-in strong locking.

A fit to submitted counts and image observations is NOT hardware calibration.
Until identifiable feedback exists, pending motion uses a 100 ms bootstrap
horizon. This can delay additional corrections. Association keeps the caller's
original spatial/ambiguity gates; hypotheses never authorize missing-target
output. No device APIs or input commands exist in this module.
"""
import math
from collections import deque
from adaptive_filter import OneEuro2D
from motion_prediction import MotionPredictor, bounded


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _point(values, name):
    try:
        values = tuple(values)
    except TypeError as exc:
        raise ValueError(name + ' must have two finite coordinates') from exc
    if len(values) != 2 or not all(_finite(v) for v in values):
        raise ValueError(name + ' must have two finite coordinates')
    return values


def _detrend(values, offsets, variance):
    mean = sum(values)/len(values)
    slope = sum(t*v for t,v in zip(offsets, values))/variance if variance else 0.
    return [v-mean-slope*t for t,v in zip(offsets, values)]


class LockMotionPredictor(MotionPredictor):
    history_capacity = 32
    bootstrap_max_ms = 100.
    scale_bounds = (.6, 1.4)
    minimum_explained = .90
    minimum_excitation = 80.

    def __init__(self):
        self.velocity_filter_config = dict(min_cutoff=4., beta=.06, derivative_cutoff=6.)
        super().__init__()
        self.minimum_observation_interval = .001

    def reset(self):
        self.response_scale = 1.
        self.identified = False
        self._model_lag = self.bootstrap_max_ms / 1000.
        self._last_model_search = None
        self._model_searches = 0
        self._model_updates = 0
        self._search_reasons = dict(bootstrap=0, periodic=0, fit_error=0)
        self._identification = dict(status='awaiting_excitation', samples=0,
                                    score=None, explained=None, excitation=None)
        self._velocity_rebuilds = 0
        super().reset()

    def reset_tracking(self):
        # Camera mapping belongs to the input path, not to a tracked entity.
        super().reset_tracking()
        # At 240 fresh frames/s, sixteen points cover only ~63 ms and can
        # erase the input edge before a 60-100 ms response is identified.
        # This is model history, NOT a queue of frames or pending commands.
        self.observations = deque(maxlen=self.history_capacity)
        self.velocity_observations = deque(maxlen=self.history_capacity)
        self.lag = self._model_lag

    def camera_at(self, when):
        if not _finite(when):
            raise ValueError('Camera timestamp must be finite')
        result = [value*self.response_scale for value in super().camera_at(when)]
        if not all(math.isfinite(value) for value in result):
            raise ValueError('Camera coordinates overflowed')
        return result

    def sent(self, now, delta, gain):
        delta = _point(delta, 'Submitted delta')
        if (not _finite(now) or not _finite(gain) or gain <= 0.
                or (self.commands and now < self.commands[-1][0])):
            raise ValueError('Submitted timestamp/gain is invalid')
        if not all(math.isfinite(self.total[a]+delta[a]/gain) for a in range(2)):
            raise ValueError('Submitted camera history overflowed')
        super().sent(now, delta, gain)

    def external(self, events, gain):
        if not _finite(gain) or gain <= 0.:
            raise ValueError('Physical input gain must be positive and finite')
        events = list(events)
        previous = self.manual_commands[-1][0] if self.manual_commands else -math.inf
        total = list(self.manual_total)
        for event in events:
            if len(event) != 3 or not all(_finite(v) for v in event) or event[0] < previous:
                raise ValueError('Physical input events must be finite and monotonic')
            previous = event[0]
            total = [total[a]+event[a+1]/gain for a in range(2)]
            if not all(math.isfinite(value) for value in total):
                raise ValueError('Physical camera history overflowed')
        super().external(events, gain)

    def _identify(self, observations, when):
        reason = ('bootstrap' if not self.identified else
                  'fit_error' if self.fit_error > 80. else 'periodic')
        if (self.identified and reason == 'periodic' and self._last_model_search is not None
                and when-self._last_model_search < .1):
            return
        self._last_model_search = when
        self._model_searches += 1
        self.lag_searches += 1
        self._search_reasons[reason] += 1
        intervals = self._velocity_intervals(observations)
        mid = sum(v[0] for v in intervals)/len(intervals)
        offsets = [v[0]-mid for v in intervals]
        variance = sum(t*t for t in offsets)
        screens = [_detrend([v[a+3]/v[2] for v in intervals], offsets, variance) for a in range(2)]
        raw_energy = sum(v*v for axis in screens for v in axis)
        self._identification = dict(status='insufficient_excitation', samples=len(observations),
                                    score=None, explained=None, excitation=None, searched_at=when)
        # No camera response -> no inferred calibration, including initial
        # waiting frames where the true delayed plant has not reacted at all.
        if not math.isfinite(raw_energy) or raw_energy/len(intervals) <= 400.:
            return
        fits = []
        for step in range(21):
            lag = step*.005
            cameras = [super(LockMotionPredictor, self).camera_at(v[0]-lag) for v in observations]
            camera_res = []
            for axis in range(2):
                rates = [(b[axis]-a[axis])/interval[2]
                         for a,b,interval in zip(cameras, cameras[1:], intervals)]
                camera_res.append(_detrend(rates, offsets, variance))
            excitation = sum(v*v for axis in camera_res for v in axis)
            if not math.isfinite(excitation) or excitation/len(intervals) < self.minimum_excitation**2:
                continue
            covariance = sum(s*c for sa,ca in zip(screens,camera_res) for s,c in zip(sa,ca))
            scale = -covariance/excitation
            if not math.isfinite(scale) or not self.scale_bounds[0] <= scale <= self.scale_bounds[1]:
                continue
            residual = sum((s+scale*c)**2 for sa,ca in zip(screens,camera_res) for s,c in zip(sa,ca))
            explained = 1.-residual/raw_energy
            if not math.isfinite(residual) or not math.isfinite(explained) or explained < self.minimum_explained:
                continue
            fits.append((residual/len(intervals),lag,scale,explained,math.sqrt(excitation/len(intervals))))
        if not fits:
            return
        best = min(v[0] for v in fits)
        plausible = [v for v in fits if v[0] <= best+max(25.,best*.1)]
        score, lag, scale, explained, excitation = min(plausible, key=lambda v: abs(v[1]-self.lag))
        self.response_scale = scale
        self.lag = self._model_lag = lag
        self.identified = True
        self._model_updates += 1
        self._identification = dict(status='fitted_not_hardware_calibration', samples=len(observations),
                                    score=score, explained=explained, excitation=excitation,
                                    searched_at=when, fitted_at=when)

    def observe(self, point, when, velocity_point=None):
        point = _point(point, 'Observation')
        if velocity_point is not None:
            _point(velocity_point, 'Velocity observation')
        previous = self.observations[-1] if self.observations else None
        if not _finite(when) or (previous is not None and when <= previous[0]):
            raise ValueError('Observation timestamp must be finite and strictly increasing')
        dt = when-previous[0] if previous else 0.
        self.observe_calls += 1
        if previous and (dt < self.minimum_observation_interval or dt > .12):
            self.observations.clear()
            self.velocity_observations.clear()
            self.velocity = [0.,0.]
            self.ready = False
        self.observations.append((when,*point))
        if len(self.observations) < 6:
            return
        observations = list(self.observations)
        self._identify(observations, when)
        # Rebuild this short world-space filter under ONE current response
        # model. Stored points filtered with an old scale would introduce a
        # coordinate jump proportional to total camera travel after a refit.
        filter_ = OneEuro2D(**self.velocity_filter_config)
        velocity_observations = []
        world = []
        for stamp,x,y in observations:
            camera = self.camera_at(stamp-self.lag)
            filtered = filter_.update((x+camera[0],y+camera[1]),stamp)
            velocity_observations.append((stamp,filtered[0]-camera[0],filtered[1]-camera[1]))
            world.append(filtered)
        self._velocity_rebuilds += 1
        self.velocity_observations.clear()
        self.velocity_observations.extend(velocity_observations)
        estimate, score, spread = self._fit(self._velocities(self.lag, velocity_observations[-6:]))
        self.fit_error = round(math.sqrt(max(0.,score)),2)
        self.ready = spread < 180. and math.hypot(*estimate) < 1200.
        if not self.ready:
            self.velocity = [0.,0.]
            return
        recent_world = world[-10:]
        extent = [max(p[a] for p in recent_world)-min(p[a] for p in recent_world) for a in range(2)]
        if math.hypot(*extent) < 8.:
            self.velocity = [0.,0.]
            return
        alpha = -math.expm1(-max(.004,min(.05,dt))/.02)
        self.velocity = bounded([self.velocity[a]+alpha*(estimate[a]-self.velocity[a]) for a in range(2)],900.)

    def expected_points(self, when):
        if not _finite(when):
            raise ValueError('Association timestamp must be finite')
        if self.identified:
            return super().expected_points(when)
        if not self.observations:
            return []
        stamp,x,y = self.observations[-1]
        hypotheses = []
        # These hypotheses do not widen the existing hard spatial gate or
        # bypass its ambiguity rejection. They only model submitted camera
        # input before feedback identifies which response is plausible.
        for lag in (.1,.08,.06,.04,.02,0.):
            a = super().camera_at(stamp-lag)
            b = super().camera_at(when-lag)
            for scale in (1.,.8,1.2):
                point = (x-scale*(b[0]-a[0]), y-scale*(b[1]-a[1]))
                if all(math.isfinite(v) for v in point):
                    hypotheses.append(point)
        return hypotheses

    def snapshot(self):
        snapshot = super().snapshot()
        snapshot['algorithm_revision'] = 'joint-response-bootstrap-20261111'
        snapshot['camera_response_model'] = dict(
            response_scale=self.response_scale, identified=self.identified, delay_ms=self.lag*1000.,
            bootstrap_max_ms=self.bootstrap_max_ms, history_capacity=self.history_capacity,
            scale_bounds=list(self.scale_bounds),
            searches=self._model_searches, updates=self._model_updates,
            search_reasons=dict(self._search_reasons), evidence=dict(self._identification),
            minimum_explained=self.minimum_explained, minimum_excitation_px_s=self.minimum_excitation,
            velocity_filter_rebuilds=self._velocity_rebuilds,
            velocity_filter_config=dict(self.velocity_filter_config),
            unmodeled_motion_risk=True, hardware_calibration=False,
            scope='image/input model fit only; bootstrap can defer output; response scaling changes actual counts')
        return snapshot
