"""Bounded target motion estimates using observations and submitted camera input.

The estimated feedback delay is a model fit, not a hardware timing measurement.
No input is emitted by this module.
"""
from collections import deque
from bisect import bisect_left
import math
import statistics


def exact_float_mean(values):
    """Same exact float mean as statistics.mean, without Fraction allocation.

    Binary-float denominators are powers of two. Accumulate on their common
    integer denominator, then perform one integer division to a rounded float.
    Keep stdlib semantics for empty, mixed-type and nonfinite inputs.
    """
    values = list(values)
    if not values or any(type(value) is not float or not math.isfinite(value) for value in values):
        return statistics.mean(values)
    ratios = [value.as_integer_ratio() for value in values]
    denominator = max(item[1] for item in ratios)
    numerator = sum(n * (denominator // d) for n, d in ratios)
    return numerator / (denominator * len(values))


def bounded(vector,maximum):
    length=math.hypot(*vector)
    scale=min(1.,maximum/length) if length else 1.
    return [value*scale for value in vector]


class MotionPredictor:
    def __init__(self):
        self.minimum_observation_interval=.004
        self.reset()

    def reset(self):
        self.commands=deque()
        self.total=[0.,0.]
        self.baseline=[0.,0.]
        self.manual_commands=deque()
        self.manual_total=[0.,0.]
        self.manual_baseline=[0.,0.]
        # Deques remain O(1) for ingestion/pruning. Repeated bisections use a
        # list view (deque middle indexing is O(n)), rebuilt only after input.
        # Like all predictor operations, callers serialize access to this state.
        self._command_lookup=None
        self._manual_lookup=None
        self.tracking_resets=0
        self.tracking_resets_retaining_inputs=0
        self.reset_tracking()

    def reset_tracking(self):
        """Clear entity-specific estimates, not camera motion already submitted.

        Histories describe the camera/input path, not the currently selected
        entity. Full reset remains the default for stop, focus and calibration.
        """
        self.tracking_resets += 1
        if self.commands or self.manual_commands:
            self.tracking_resets_retaining_inputs += 1
        self.observations=deque(maxlen=16)
        self.velocity_observations=deque(maxlen=16)
        self.velocity=[0.,0.]
        self.lag=.035
        self.ready=False
        self.observe_calls=0
        self.lag_searches=0
        self.fit_error=0.
        self.lead=[0.,0.]
        self.pending=[0.,0.]

    def sent(self,now,delta,gain):
        self._command_lookup=None
        self.total=[self.total[axis]+delta[axis]/gain for axis in range(2)]
        self.commands.append((now,tuple(self.total)))
        while self.commands and now-self.commands[0][0]>.8:
            self.baseline=list(self.commands.popleft()[1])

    def camera_at(self,when):
        if self._command_lookup is None:
            self._command_lookup=list(self.commands)
        if self._manual_lookup is None:
            self._manual_lookup=list(self.manual_commands)
        index=bisect_left(self._command_lookup,(when-1e-9,))-1
        value=self._command_lookup[index][1] if index>=0 else self.baseline
        index=bisect_left(self._manual_lookup,(when-1e-9,))-1
        manual=self._manual_lookup[index][1] if index>=0 else self.manual_baseline
        return [value[axis]+manual[axis] for axis in range(2)]

    def external(self,events,gain):
        self._manual_lookup=None
        # Separate monotonic histories allow reports that arrive after a HID
        # command but carry an earlier timestamp. Do not count our HID twice.
        for stamp,x,y in events:
            self.manual_total=[self.manual_total[a]+(x,y)[a]/gain for a in range(2)]
            self.manual_commands.append((stamp,tuple(self.manual_total)))
        if events:
            cutoff=events[-1][0]-.8
            while self.manual_commands and self.manual_commands[0][0]<cutoff:
                self.manual_baseline=list(self.manual_commands.popleft()[1])

    def camera_between(self,start,end):
        a,b=self.camera_at(start),self.camera_at(end)
        return [b[axis]-a[axis] for axis in range(2)]

    @staticmethod
    def _velocity_intervals(observations):
        # All delay candidates share these screen-space/time differences.
        # Keep subtraction/addition/division in the original order; do not
        # replace division by reciprocal multiplication (rounding can differ).
        return [((a[0]+b[0])/2,b[0],b[0]-a[0],b[1]-a[1],b[2]-a[2])
                for a,b in zip(observations,observations[1:])]

    def _velocities(self,lag,observations,intervals=None):
        values=[]
        if len(observations) < 2:
            return values
        if intervals is None:
            intervals=self._velocity_intervals(observations)
        previous_camera=self.camera_at(observations[0][0]-lag)
        for middle,end,dt,dx,dy in intervals:
            current_camera=self.camera_at(end-lag)
            # Exactly two axes: avoid per-interval generators/temporary vectors.
            values.append((middle,end,
                           (dx+(current_camera[0]-previous_camera[0]))/dt,
                           (dy+(current_camera[1]-previous_camera[1]))/dt))
            previous_camera=current_camera
        return values

    @staticmethod
    def _fit_geometry(values):
        # The 21 lag candidates use identical observation timestamps. Compute
        # timestamp-only terms once, without changing floating-point order.
        middle=exact_float_mean(value[0] for value in values)
        offsets=[value[0]-middle for value in values]
        variance=sum(offset**2 for offset in offsets)
        return offsets,variance,values[-1][1]-middle

    @staticmethod
    def _fit(values,geometry=None,*,score_only=False):
        offsets,variance,end_offset=MotionPredictor._fit_geometry(values) if geometry is None else geometry
        mean_x=exact_float_mean(value[2] for value in values)
        mean_y=exact_float_mean(value[3] for value in values)
        slope_x=sum(offset*value[2] for offset,value in zip(offsets,values))/variance if variance else 0.
        slope_y=sum(offset*value[3] for offset,value in zip(offsets,values))/variance if variance else 0.
        # Account for a target accelerating smoothly. Otherwise a change in
        # target speed can be mistaken for a change in camera input delay.
        residuals=[math.hypot(value[2]-mean_x-slope_x*offset,value[3]-mean_y-slope_y*offset)
                   for offset,value in zip(offsets,values)]
        # Huber cost limits a bad detection's influence without discarding all
        # the input-response spikes needed to distinguish feedback delays.
        score=exact_float_mean(r*r if r<300 else 600*r-90000 for r in residuals)
        if score_only:
            # Lag selection uses only cost, not center or sorted median. The
            # final velocity fit still computes the full original statistics.
            return score
        center=[mean_x+slope_x*end_offset,mean_y+slope_y*end_offset]
        return center,score,statistics.median(residuals)

    def observe(self,point,when,velocity_point=None):
        self.observe_calls += 1
        previous=self.observations[-1] if self.observations else None
        dt=when-previous[0] if previous else 0.
        if previous and (dt<self.minimum_observation_interval or dt>.12):
            self.observations.clear()
            self.velocity_observations.clear()
            self.velocity=[0.,0.]
            self.ready=False
        self.observations.append((when,*point))
        self.velocity_observations.append((when,*(point if velocity_point is None else velocity_point)))
        if len(self.observations)<6:
            return
        observations=list(self.observations)
        native_velocities=getattr(self._velocities,'__func__',None) is MotionPredictor._velocities
        intervals=self._velocity_intervals(observations) if native_velocities else None
        def candidate_values(lag):
            return (self._velocities(lag,observations,intervals) if native_velocities
                    else self._velocities(lag,observations))
        current_values=candidate_values(self.lag)
        # Keep subclass/reference overrides intact. Geometry is local to this
        # observe call, so target reset, new times and new inputs cannot reuse it.
        shared_geometry=(self._fit_geometry(current_values)
                         if self._fit is MotionPredictor._fit and native_velocities else None)
        def candidate_score(values):
            return (self._fit(values,shared_geometry,score_only=True) if shared_geometry is not None
                    else self._fit(values)[1])
        current_score=candidate_score(current_values)
        if (self.commands or self.manual_commands) and current_score>3600.:
            self.lag_searches += 1
            fits=[]
            for step in range(21):
                lag=step*.005
                # Reuse only an EXACTLY equal lag within this one observation.
                # No epsilon matching, approximate score, or cross-frame cache.
                score=(current_score if shared_geometry is not None and native_velocities and lag==self.lag
                       else candidate_score(candidate_values(lag)))
                fits.append((score,lag))
            best=min(score for score,_ in fits)
            plausible=[lag for score,lag in fits if score<=best+max(200.,best*.1)]
            self.lag=min(plausible,key=lambda lag:abs(lag-self.lag))
        velocity_observations=list(self.velocity_observations)
        estimate,score,spread=self._fit(self._velocities(self.lag,velocity_observations[-6:]))
        self.fit_error=round(math.sqrt(max(0.,score)),2)
        self.ready=spread<180 and math.hypot(*estimate)<1200
        if not self.ready:
            self.velocity=[0.,0.]
            return
        world=[]
        for stamp,x,y in velocity_observations[-10:]:
            camera=self.camera_at(stamp-self.lag)
            world.append((x+camera[0],y+camera[1]))
        extent=[max(point[axis] for point in world)-min(point[axis] for point in world) for axis in range(2)]
        # Small box jitter can have a large instantaneous derivative. Require
        # sustained world displacement before allowing velocity feedforward.
        if math.hypot(*extent)<8.:
            self.velocity=[0.,0.]
            return
        alpha=-math.expm1(-max(.004,min(.05,dt))/.02)
        self.velocity=[self.velocity[axis]+alpha*(estimate[axis]-self.velocity[axis]) for axis in range(2)]
        self.velocity=bounded(self.velocity,900.)

    def expected_points(self,when):
        if not self.observations:
            return []
        stamp,x,y=self.observations[-1]
        dt=max(0.,min(.15,when-stamp))
        velocity=self.velocity if self.ready else (0.,0.)
        # Early delay uncertainty is used for association only. It never emits
        # motion to a missing target. Once fitted, use the preferred delay.
        lags=[self.lag] if self.ready else [self.lag,0.,.02,.04,.06,.08,.1]
        result=[]
        for lag in lags:
            camera=self.camera_between(stamp-lag,when-lag)
            result.append((x+velocity[0]*dt-camera[0],y+velocity[1]*dt-camera[1]))
        return result

    def control(self,point,area,observed_at,now):
        velocity=list(self.velocity) if self.ready else [0.,0.]
        speed=math.hypot(*velocity)
        weight=max(0.,min(1.,(speed-20.)/60.))
        velocity=[value*weight for value in velocity]
        age=max(0.,min(.12,now-observed_at))
        horizon=age+self.lag*.8
        self.lead=bounded([value*horizon for value in velocity],24.)
        self.pending=self.camera_between(observed_at-self.lag,now+1e-8)
        error=[point[axis]-area[key]/2+self.lead[axis] for axis,key in enumerate(('width','height'))]
        # Reserve input not yet visible in the measured frame, without turning
        # around solely because a prediction crossed the measured aim point.
        corrected=[math.copysign(max(0.,min(abs(value),(value-self.pending[axis])*math.copysign(1.,value))),value)
                   for axis,value in enumerate(error)]
        return corrected,velocity

    def snapshot(self):
        return dict(math_revision="exact-predictor-math-20261109",
                    lookup_revision="indexed-camera-history-20261109",
                    fit_geometry_reuse=True,
                    algorithm_revision="score-only-candidates-20261109",
                    lag_candidates=21,
                    candidate_interval_reuse=True,
                    input_coordination_revision="camera-ledger-20261109",
                    camera_history=dict(virtual_reports=len(self.commands),manual_reports=len(self.manual_commands),
                        tracking_resets=self.tracking_resets,retained_on_tracking_reset=self.tracking_resets_retaining_inputs),
                    observe_calls=self.observe_calls,lag_searches=self.lag_searches,
                    ready=self.ready,estimated_feedback_ms=round(self.lag*1000,1),
                    target_velocity_px_s=[round(value,2) for value in self.velocity],
                    velocity_fit_error=self.fit_error,lead_pixels=[round(value,2) for value in self.lead],
                    pending_camera_pixels=[round(value,2) for value in self.pending])
