"""Target continuity and bounded, time-based camera correction.

No commands are sent here. The caller dispatches only on a fresh image.
"""
import math
from collections import deque
from motion_prediction import MotionPredictor
from pd_control import PDController
from strong_lock import StrongLockServo
from engagement_tracking import EngagementTracker
from aim_region import CHEST_FILTER_CONFIG


def _distance(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])


def _copy(target):
    return dict(target, row=dict(target['row'], box=list(target['row']['box'])))


class LegacyStableAim:
    @property
    def control_algorithm(self):
        return getattr(self,'_control_algorithm','legacy')

    @control_algorithm.setter
    def control_algorithm(self,value):
        previous=self.control_algorithm
        self._control_algorithm=value
        if previous!=value and hasattr(self,'predictor') and ('lock' in (previous,value)):
            # The algorithm transition is disarmed by CursorFollower. Keep the
            # historical predictor byte-for-byte for legacy/PD calculations.
            from lock_prediction import LockMotionPredictor
            interval=self.predictor.minimum_observation_interval
            self.predictor=LockMotionPredictor() if value=='lock' else MotionPredictor()
            self.predictor.minimum_observation_interval=interval
            self.reset()

    def __init__(self, selector):
        self.selector = selector
        self.predictive = False
        self.soft_assist = False
        self.tolerance_px = 0.
        self.control_algorithm="legacy"
        self.pd_kp=1.;self.pd_kd=.25
        self.pd=PDController()
        self.lock_servo=StrongLockServo()
        self.engagement=EngagementTracker()
        self.target_continuity_enabled=False
        self.predictor = MotionPredictor()
        self.reset()

    def reset(self):
        self.locked = self.pending = None
        self.retain_target=False
        self.engagement.reset()
        self.engagement_state=dict(status="inactive",reason=None,support=0,translation=None)
        self.pending_frames = self.promotion_frames = 0
        self.lost_since = None
        self.area_key = None
        self.acquired_at = 0
        self.state = 'no_target'
        self.waiting_for_feedback = False
        self.observed_at = 0.
        self.lock_frame_at = None
        self.miss_reason = None
        self.association = 'none'
        self.reset_output()

    def reset_output(self, reset_prediction=True, preserve_camera=False):
        self.pd.reset()
        self.lock_servo.reset()
        self.last_time = None
        self.velocity = [0., 0.]
        self.remainder = [0., 0.]
        self.last_delta = (0, 0)
        self.centered = False
        self.dt_ms = 0
        self.effective_response_ms = 0
        self.acceleration_multiplier = 1.
        self.previous_error = None
        self.brake_until = 0.
        self.recent_outputs = deque()
        if reset_prediction:
            if preserve_camera:
                self.predictor.reset_tracking()
            else:
                self.predictor.reset()

    def pause_target(self, now, reason):
        """Clear output, not the engaged identity or submitted camera history."""
        self.reset_output(reset_prediction=False)
        self.waiting_for_feedback=False
        self.lost_since=now if self.lost_since is None else self.lost_since
        self.state='fire_target_hold'
        self.miss_reason=reason

    def _remember_engagement(self, candidates, target):
        if self.target_continuity_enabled:
            self.engagement.observe(candidates,target,self.observed_at)

    def _matches(self, candidate, previous, gain):
        if candidate['aim_source'] != previous['aim_source'] or candidate['row']['label'] != previous['row']['label']:
            return False
        a, b = candidate['row']['box'], previous['row']['box']
        for dimension in (0, 1):
            ratio = (a[dimension+2]-a[dimension])/(b[dimension+2]-b[dimension])
            if not .35 <= ratio <= 2.8:
                return False
        diagonal = math.hypot(b[2]-b[0], b[3]-b[1])
        margin = math.hypot(*self.last_delta)/max(gain,.05)
        gate = max(16, min(80, diagonal*1.5+margin))
        return _distance(candidate['point'], previous['point']) <= gate

    def _accept(self, target):
        self.locked = _copy(target)
        self.lost_since = None
        self.pending = None
        self.pending_frames = 0
        self.state = 'tracking'
        self.miss_reason = None
        if self.predictive:
            self.predictor.observe(target['point'],self.observed_at)
        return target

    def choose(self, rows, area, confidence, aim_height_ratio, prefer_head, now, gain, observed_at=None):
        area_key = tuple(area[key] for key in ('left','top','width','height'))
        if area_key != self.area_key:
            # A full controller reset already initialized the first region.
            # Do not erase physical reports ingested earlier in this same tick.
            if self.area_key is not None:
                self.reset()  # A real region/coordinate change remains a new epoch.
            self.area_key = area_key
        self.observed_at = now if observed_at is None else observed_at
        candidates = [self.selector([row],area,confidence,aim_height_ratio,prefer_head) for row in rows]
        candidates = [item for item in candidates if item is not None]
        # Reject ambiguous associations instead of arbitrarily transferring a
        # lock when two detections could plausibly be the same tracked target.
        # This is only a screen-space heuristic: it is not a persistent player ID.
        ambiguity_margin = 8.0
        def candidate_key(item):
            return (item['point'][0], item['point'][1], str(item['row'].get('label', '')))

        if self.locked is not None and self.retain_target and self.target_continuity_enabled:
            match=self.engagement.match(candidates,self.observed_at)
            self.engagement_state={key:match.get(key) for key in ('status','reason','support','translation')}
            if match['status']=='matched':
                target=match['target']
                expected=self.predictor.expected_points(self.observed_at) if self.predictive else [self.locked['point']]
                reanchor=bool(expected and min(_distance(target['point'],point) for point in expected)>8.)
                if reanchor:
                    # Visual scene motion may include recoil, not submitted HID.
                    # Never add it to the input ledger or teach it as target speed.
                    self.reset_output(preserve_camera=True)
                self.engagement_state['velocity_reanchored']=reanchor
                self.association='fire_scene_consensus'
                self._remember_engagement(candidates,target)
                return self._accept(target)
            if match['status']=='uncertain':
                self.association='fire_scene_uncertain'
                self.pause_target(now,'fire_identity_uncertain')
                return None
        if self.locked is not None:
            expected=self.predictor.expected_points(self.observed_at) if self.predictive else []
            scored=[]
            if expected:
                box=self.locked['row']['box']
                gate=max(16.,min(80.,math.hypot(box[2]-box[0],box[3]-box[1])*1.5))
                previous=_copy(self.locked)
                for item in candidates:
                    distances=[]
                    for index,point in enumerate(expected):
                        previous['point']=point
                        if self._matches(item,previous,gain) and _distance(item['point'],point)<=gate:
                            # Before strong-mode camera identification, lag/scale
                            # hypotheses have no known preference. Ranking them
                            # by list order can transfer the lock to a distractor
                            # still sitting at the old screen coordinate.
                            penalty=(0. if self.control_algorithm=='lock' and
                                     not getattr(self.predictor,'identified',False) else index*4.)
                            distances.append(_distance(item['point'],point)+penalty)
                    if distances:
                        scored.append((min(distances),item))
                self.association='camera_compensated'
            else:
                scored=[(_distance(item['point'],self.locked['point']),item)
                        for item in candidates if self._matches(item,self.locked,gain)]
                self.association='screen_position'
            scored.sort(key=lambda pair:(pair[0],candidate_key(pair[1])))
            ambiguous=(len(scored)>1 and scored[1][0]-scored[0][0]<ambiguity_margin)
            if scored and not ambiguous:
                target=scored[0][1]
                # Promote a body estimate only to a head inside this body's box.
                if prefer_head and self.locked['aim_source'] != 'detected_head':
                    x1,y1,x2,y2 = target['row']['box']
                    heads = [item for item in candidates if item['aim_source']=='detected_head'
                             and x1<=item['point'][0]<=x2 and y1<=item['point'][1]<=y1+(y2-y1)*.4]
                    self.promotion_frames = self.promotion_frames+1 if heads else 0
                    if self.promotion_frames>=2:
                        target = min(heads,key=lambda item:_distance(item['point'],target['point']))
                        self.promotion_frames = 0
                        self.reset_output(preserve_camera=True)
                self._remember_engagement(candidates,target)
                return self._accept(target)
            self.reset_output(reset_prediction=not self.predictive)
            self.miss_reason=('ambiguous_target' if ambiguous else
                              'detection_missing' if not candidates else
                              'aim_source_missing' if not any(item['aim_source']==self.locked['aim_source'] for item in candidates)
                              else 'association_mismatch')
            self.lost_since = now if self.lost_since is None else self.lost_since
            if self.retain_target or now-self.lost_since < .15:
                self.state = 'fire_target_hold' if self.retain_target else 'target_hold'
                return None
            self.locked = None
            self.pending = None
            self.pending_frames = 0
        if prefer_head:
            heads = [item for item in candidates if item['aim_source']=='detected_head']
            if heads:
                candidates = heads
        if not candidates:
            self.pending = None
            self.pending_frames = 0
            self.state = 'no_target'
            self.miss_reason = 'detection_missing'
            return None
        center = (area['width']/2,area['height']/2)
        # During the two-frame confirmation, continue the pending candidate
        # rather than switching merely because another detection got closer.
        pending_matches=[]
        if self.pending is not None:
            pending_matches=[(_distance(item['point'],self.pending['point']),item)
                             for item in candidates if self._matches(item,self.pending,gain)]
            pending_matches.sort(key=lambda pair:(pair[0],candidate_key(pair[1])))
        pending_ambiguous=(len(pending_matches)>1 and
                           pending_matches[1][0]-pending_matches[0][0]<ambiguity_margin)
        if pending_ambiguous:
            self.pending=None;self.pending_frames=0
            self.state='target_hold';self.miss_reason='ambiguous_target'
            return None
        if pending_matches:
            target=pending_matches[0][1]
            self.pending_frames += 1
        else:
            target=min(candidates,key=lambda item:(_distance(item['point'],center),
                                                   -float(item['row']['confidence']),candidate_key(item)))
            self.pending_frames=1
        # Closely overlapping detections cannot be reliably distinguished even
        # during reacquisition. Wait for them to separate instead of guessing.
        if any(item is not target and _distance(item['point'],target['point'])<ambiguity_margin
               for item in candidates):
            self.pending=None;self.pending_frames=0
            self.state='target_hold';self.miss_reason='ambiguous_target'
            return None
        self.miss_reason=None
        self.pending = _copy(target)
        if self.pending_frames < 2:
            self.state = 'acquiring'
            return None
        self.reset_output(preserve_camera=True)
        self.association='confirmed_new_target'
        self.acquired_at = now
        self._remember_engagement(candidates,target)
        return self._accept(target)

    def _enter_radius(self, box):
        base=max(1.,min(3.,min(box[2]-box[0],box[3]-box[1])*.18))
        return max(base,self.tolerance_px) if self.soft_assist else base

    def _axis_radius(self, error, enter, axis):
        # Project the circular tolerance onto each axis. Independent full-radius
        # axis dead zones would silently enlarge it to a square on diagonals.
        distance=math.hypot(*error)
        return enter*abs(error[axis])/distance if self.soft_assist and distance else enter

    def _assistance_weight(self, distance, enter):
        if not self.soft_assist:return 1.
        # Ease back in outside the tolerance, including velocity feedforward.
        # Otherwise crossing the dead-zone edge repeatedly creates sharp pulses
        # when a moving target alternates between "centered" and "tracking".
        amount=max(0.,min(1.,(distance-enter)/max(12.,2*enter)))
        return amount*amount*(3.-2.*amount)

    def _soft_counts(self, dt, limit):
        # One vector budget in all directions. Clamp internal velocity as well
        # as output, so saturation cannot build an invisible high-rate state.
        budget=min(limit,limit*dt*60)
        speed=math.hypot(*self.velocity)
        scale=min(1.,budget/(speed*dt)) if speed else 1.
        self.velocity=[value*scale for value in self.velocity]
        exact=[self.velocity[a]*dt+self.remainder[a] for a in range(2)]
        length=math.hypot(*exact)
        scale=min(1.,budget/length) if length else 1.
        bounded=[value*scale for value in exact]
        # Toward-zero quantization cannot exceed the vector budget. Preserve
        # fractions for later fresh frames, never queue a precomputed movement.
        counts=tuple(math.trunc(value) for value in bounded)
        self.remainder=[bounded[a]-counts[a] for a in range(2)]
        self.last_delta=counts
        return counts

    def command(self, target, area, now, gain, limit, response_ms, chase_strength=1.):
        dt = now-self.last_time if self.last_time is not None else 1/60
        if dt<=0 or dt>.1:
            dt = 1/60
            self.velocity = [0.,0.]
            self.remainder = [0.,0.]
        dt = min(dt,1/30)
        self.last_time = now
        self.dt_ms = round(dt*1000,2)
        error = [target['point'][0]-area['width']/2,target['point'][1]-area['height']/2]
        distance = math.hypot(*error)
        box = target['row']['box']
        enter = self._enter_radius(box)
        hysteresis = min(2.,min(box[2]-box[0],box[3]-box[1])*.12)
        stop_radius = enter+hysteresis if self.centered else enter
        feedforward=[0.,0.]
        corrected=None
        if self.predictive:
            corrected,feedforward=self.predictor.control(target['point'],area,self.observed_at,now)
        if distance<=stop_radius and (self.soft_assist or math.hypot(*feedforward)<20.):
            self.centered = True
            self.velocity = [0.,0.]
            self.remainder = [0.,0.]
            self.last_delta = (0,0)
            self.previous_error = error
            self.pd.reset()
            return (0,0)
        self.centered = False
        # Extra acceleration applies away from the crosshair. Keep the familiar
        # 40 ms approach near the target and brake after a measured overshoot.
        blend = max(0.,min(1.,(distance-20)/60))
        blend = blend*blend*(3-2*blend)
        self.acceleration_multiplier = 1.+(chase_strength-1.)*blend
        effective_response = response_ms
        if chase_strength>1. or self.predictive:
            effective_response = max(response_ms,40.)*(1-blend)+response_ms*blend
            if self.previous_error is not None and any(
                    error[axis]*self.previous_error[axis]<0 and
                    abs(error[axis])>enter and abs(self.previous_error[axis])>enter
                    for axis in range(2)):
                self.brake_until = now+.12
            if now<self.brake_until:
                effective_response = max(effective_response,80.)
        self.previous_error = error
        self.effective_response_ms = round(effective_response,2)
        alpha = -math.expm1(-dt/(effective_response/1000))
        if chase_strength>1. and not self.predictive:
            # A newly captured game frame may still predate recent mouse input.
            # Reserve 50 ms of sent motion instead of repeatedly correcting the
            # same screen error at the stronger rate. Never predict a reversal.
            while self.recent_outputs and now-self.recent_outputs[0][0]>=.05-1e-9:
                self.recent_outputs.popleft()
            pending = [sum(item[1][axis] for item in self.recent_outputs)/gain for axis in range(2)]
            error = [math.copysign(max(0.,min(abs(value),
                         (value-pending[axis])*math.copysign(1.,value))),value)
                     for axis,value in enumerate(error)]
        # Gain converts pixels to counts; alpha controls how much to correct now.
        if corrected is not None:
            error=corrected
        assistance=self._assistance_weight(distance,enter)
        if self.control_algorithm=='pd':
            measured=[target['point'][axis]-area[key]/2 for axis,key in enumerate(('width','height'))]
            self.pd.observe(measured,self.observed_at or now,feedforward)
            rates=[gain*value*alpha/dt for value in error]
            rates=[self.pd.correction(a,rates[a],gain,self.pd_kp,self.pd_kd) for a in range(2)]
            desired = [assistance*(rates[axis]+gain*feedforward[axis]) for axis in range(2)]
        else:
            desired = [assistance*gain*(value*alpha/dt+feedforward[axis]) for axis,value in enumerate(error)]
        for axis in range(2):
            if abs(error[axis])<=self._axis_radius(error,enter,axis) and (self.soft_assist or abs(feedforward[axis])<20.):
                desired[axis] = 0.
                if not self.soft_assist:
                    self.velocity[axis] = self.remainder[axis] = 0.
            elif desired[axis]*self.velocity[axis]<0 and not self.soft_assist:
                # Brake before reversing instead of continuing the old direction.
                self.velocity[axis] = self.remainder[axis] = 0.
        change = [desired[axis]-self.velocity[axis] for axis in range(2)]
        length = math.hypot(*change)
        max_change = 120000*self.acceleration_multiplier*dt  # counts/s²
        scale = min(1.,max_change/length) if length else 1.
        self.velocity = [self.velocity[axis]+change[axis]*scale for axis in range(2)]
        if self.soft_assist:
            result=self._soft_counts(dt,limit)
            if chase_strength>1. and not self.predictive:
                self.recent_outputs.append((now,result))
            return result
        budget = min(limit,limit*dt*60)
        delta=[]
        for axis in range(2):
            exact = self.velocity[axis]*dt+self.remainder[axis]
            bounded = max(-budget,min(budget,exact))
            count = max(-limit,min(limit,round(bounded)))
            self.remainder[axis] = bounded-count if exact==bounded else 0.
            delta.append(count)
        self.last_delta = tuple(delta)
        if chase_strength>1. and not self.predictive:
            self.recent_outputs.append((now,self.last_delta))
        return self.last_delta


class StableAim(LegacyStableAim):
    """V3 compensated position filter and independent anticipatory axis braking.

    Association still uses raw detections. Missing/rejected/stale observations
    are stopped by the caller before this controller can produce any output.
    """
    near_response_ms=20.
    bootstrap_response_ms=28.

    def __init__(self,selector):
        from adaptive_filter import OneEuro2D
        self.position_filter=OneEuro2D()
        self.chest_position_filter=OneEuro2D(**CHEST_FILTER_CONFIG)
        self.position_filter_profile=None
        self.adaptive_filter=True
        super().__init__(selector)

    def reset_output(self,reset_prediction=True,preserve_camera=False):
        super().reset_output(reset_prediction,preserve_camera)
        self.position_filter.reset()
        self.chest_position_filter.reset()
        self.position_filter_profile=None
        self.filtered_point=None
        self.axis_response_ms=[0.,0.]
        self.axis_braking=[False,False]

    def _accept(self,target):
        self.locked=_copy(target)
        self.lost_since=None;self.pending=None;self.pending_frames=0
        self.state='tracking';self.miss_reason=None
        point=target['point']
        if self.predictive and self.control_algorithm=='lock':
            chest=target.get('aim_source')=='chest_box_estimate'
            config=(CHEST_FILTER_CONFIG if chest else dict(
                min_cutoff=self.position_filter.min_cutoff,beta=self.position_filter.beta,
                derivative_cutoff=self.position_filter.derivative_cutoff))
            self.predictor.velocity_filter_config=dict(config)
            self.position_filter_profile='lock_chest_velocity' if chest else 'lock_head_velocity'
            self.predictor.observe(point,self.observed_at)
            self.filtered_point=list(point)  # raw position; velocity filtered by model
            return target
        if self.predictive:
            if self.adaptive_filter:
                camera=self.predictor.camera_at(self.observed_at-self.predictor.lag)
                world=[point[axis]+camera[axis] for axis in range(2)]
                chest = target.get("aim_source") == "chest_box_estimate"
                position_filter = self.chest_position_filter if chest else self.position_filter
                self.position_filter_profile = "chest_damped" if chest else "original"
                filtered=position_filter.update(world,self.observed_at)
                point=tuple(filtered[axis]-camera[axis] for axis in range(2))
            # Preserve unfiltered response edges for delay identification and
            # association; use the filtered position only for velocity fitting.
            self.predictor.observe(target['point'],self.observed_at,velocity_point=point)
        self.filtered_point=list(point)
        return target

    def _lock_command(self,target,area,now,gain,limit,response_ms,chase_strength):
        # Direct discrete position servo: no acceleration/velocity tail and no
        # hidden near-field response floor. Only fresh detections reach here.
        if not self.predictive:
            raise ValueError('预测强锁需要启用预测跟踪；不会静默回退')
        dt=now-self.last_time if self.last_time is not None else 1/60
        if not 0 < dt <= .1:
            dt=1/60
            self.lock_servo.reset()
            self.remainder=[0.,0.]
        dt=min(dt,1/30)
        self.last_time=now
        self.dt_ms=round(dt*1000,2)
        raw_error=[target['point'][axis]-area[key]/2 for axis,key in enumerate(('width','height'))]
        box=target['row']['box']
        enter=self._enter_radius(box)
        hysteresis=min(2.,min(box[2]-box[0],box[3]-box[1])*.12)
        stop_radius=enter+hysteresis if self.centered else enter
        # Keep filtered observations for velocity estimation, but do not add
        # the position-filter's phase lag to this opt-in position correction.
        error,feedforward=self.predictor.control(target['point'],area,self.observed_at,now)
        self.centered=(math.hypot(*raw_error)<=stop_radius and
                       (self.soft_assist or math.hypot(*feedforward)<20.))
        stopped=tuple(self.centered or
            (abs(raw_error[axis])<=self._axis_radius(raw_error,enter,axis) and
             (self.soft_assist or abs(feedforward[axis])<20.)) for axis in range(2))
        assistance=self._assistance_weight(math.hypot(*raw_error),enter)
        # ManualBlend may attenuate the previous remainder downstream. Import
        # that actual state instead of resurrecting a private unattenuated copy.
        self.lock_servo.remainder=list(self.remainder)
        response_scale=getattr(self.predictor,'response_scale',1.)
        # This changes ACTUAL counts, not the saved/user gain. UI and diagnostics
        # expose this estimated conversion explicitly; it is not hardware calibration.
        effective_gain=gain/response_scale
        result=self.lock_servo.step(error,feedforward,dt,effective_gain,limit,response_ms,
                                   chase_strength,assistance,stopped)
        self.remainder=list(self.lock_servo.remainder)
        self.velocity=[value/dt for value in result]  # diagnostics, never fed back
        self.previous_error=raw_error
        self.last_delta=result
        self.pd.reset()
        self.axis_braking=list(stopped)
        self.effective_response_ms=self.lock_servo.time_constant_ms
        self.axis_response_ms=[self.effective_response_ms]*2
        self.acceleration_multiplier=self.lock_servo.chase_multiplier
        return result

    def command(self,target,area,now,gain,limit,response_ms,chase_strength=1.):
        if self.control_algorithm=='lock':
            return self._lock_command(target,area,now,gain,limit,response_ms,chase_strength)
        if not self.predictive:
            return super().command(target,area,now,gain,limit,response_ms,chase_strength)
        dt=now-self.last_time if self.last_time is not None else 1/60
        if dt<=0 or dt>.1:
            dt=1/60;self.velocity=[0.,0.];self.remainder=[0.,0.]
        dt=min(dt,1/30)
        self.last_time=now;self.dt_ms=round(dt*1000,2)
        raw_error=[target['point'][axis]-area[key]/2 for axis,key in enumerate(('width','height'))]
        box=target['row']['box']
        enter=self._enter_radius(box)
        hysteresis=min(2.,min(box[2]-box[0],box[3]-box[1])*.12)
        stop_radius=enter+hysteresis if self.centered else enter
        point=self.filtered_point if self.filtered_point is not None else target['point']
        error,feedforward=self.predictor.control(point,area,self.observed_at,now)
        if math.hypot(*raw_error)<=stop_radius and (self.soft_assist or math.hypot(*feedforward)<20.):
            self.centered=True;self.velocity=[0.,0.];self.remainder=[0.,0.]
            self.last_delta=(0,0);self.previous_error=raw_error
            self.axis_braking=[True,True]
            self.pd.reset()
            return self.last_delta
        self.centered=False
        blend=max(0.,min(1.,(math.hypot(*raw_error)-20)/60))
        blend=blend*blend*(3-2*blend)
        self.acceleration_multiplier=1.+(chase_strength-1.)*blend
        acceleration=120000*self.acceleration_multiplier
        desired_rates=[];stop_axes=[]
        assistance=self._assistance_weight(math.hypot(*raw_error),enter)
        if self.control_algorithm=='pd':
            self.pd.observe(raw_error,self.observed_at or now,feedforward)
        for axis in range(2):
            # Close-range response is shorter than V2's fixed 40 ms. The
            # remaining compensated error bounds speed before an overshoot.
            proximity=max(0.,min(1.,abs(raw_error[axis])/80))
            response=max(response_ms,self.near_response_ms)*(1-proximity)+response_ms*proximity
            if not self.predictor.ready:
                response=max(response,self.bootstrap_response_ms,dt*1000*1.25)
            self.axis_response_ms[axis]=round(response,2)
            alpha=-math.expm1(-dt/(response/1000))
            correction=gain*error[axis]*alpha/dt
            if self.control_algorithm=='pd':
                correction=self.pd.correction(axis,correction,gain,self.pd_kp,self.pd_kd)
            axis_enter=self._axis_radius(raw_error,enter,axis)
            braking_speed=math.sqrt(2*acceleration*max(0.,gain*(abs(error[axis])-axis_enter)))
            bounded=max(-braking_speed,min(braking_speed,correction))
            self.axis_braking[axis]=abs(bounded)<abs(correction)
            desired=assistance*(bounded+gain*feedforward[axis])
            stopped=abs(raw_error[axis])<=axis_enter and (self.soft_assist or abs(feedforward[axis])<20.)
            stop_axes.append(stopped)
            if stopped:
                desired=0.
                if not self.soft_assist:
                    self.velocity[axis]=self.remainder[axis]=0.
            if desired*self.velocity[axis]<0 and not self.soft_assist:
                # Never continue a stale direction while waiting to reverse.
                self.velocity[axis]=self.remainder[axis]=0.
            desired_rates.append(desired)
        changes=[desired_rates[axis]-self.velocity[axis] for axis in range(2)]
        length=math.hypot(*changes)
        scale=min(1.,acceleration*dt/length) if length else 1.
        self.velocity=[self.velocity[axis]+changes[axis]*scale for axis in range(2)]
        if self.soft_assist:
            self.previous_error=raw_error
            self.effective_response_ms=round(max(self.axis_response_ms),2)
            return self._soft_counts(dt,limit)
        delta=[]
        for axis in range(2):
            if stop_axes[axis]:delta.append(0);continue
            budget=min(limit,limit*dt*60)
            exact=self.velocity[axis]*dt+self.remainder[axis]
            bounded=max(-budget,min(budget,exact))
            count=max(-limit,min(limit,round(bounded)))
            self.remainder[axis]=bounded-count if exact==bounded else 0.
            delta.append(count)
        self.previous_error=raw_error
        self.effective_response_ms=round(max(self.axis_response_ms),2)
        self.last_delta=tuple(delta)
        return self.last_delta
