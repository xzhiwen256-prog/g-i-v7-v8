"""Bounded passive pipeline counters. No target selection or movement logic."""
from collections import Counter, deque
import math
import threading
import time


class PipelineHealth:
    def __init__(self, seconds=60, clock=time.monotonic):
        if type(seconds) is not int or not 1 <= seconds <= 300:
            raise ValueError('Health window must be 1..300 seconds')
        self.seconds, self.clock = seconds, clock
        self._buckets = deque(maxlen=seconds)
        self._last_active = None
        self._lock = threading.Lock()

    def observe(self, state, performance):
        now = self.clock()
        second = int(now)
        stage = str(state.get('stage', 'unknown'))[:48]
        detections = state.get('detections', 0)
        eligible = state.get('eligible_detections', 0)
        has_detection = isinstance(detections, (int, float)) and detections > 0
        has_eligible = isinstance(eligible, (int, float)) and eligible > 0
        guard = performance.get('inference_path') == 'fp32_boundary_guard'
        prediction = state.get('prediction') or {}
        age = performance.get('frame_to_submit_ms')
        age = age if isinstance(age, (int, float)) and math.isfinite(age) and age >= 0 else 0.
        with self._lock:
            if not self._buckets or self._buckets[-1]['second'] != second:
                self._buckets.append(dict(second=second, stages=Counter(), counters=Counter(), max_age=0.))
            b = self._buckets[-1]
            b['stages'][stage] += 1
            b['counters'].update(frames=1, detected_frames=int(has_detection), eligible_frames=int(has_eligible),
                                 submitted_frames=int(stage == 'sent'), fp32_guard_frames=int(guard),
                                 stale_frames=int(stage == 'stale'),
                                 fp32_guard_stale_frames=int(guard and stage == 'stale'),
                                 eligible_prediction_unready_frames=int(has_eligible and prediction.get('ready') is not True))
            b['max_age'] = max(b['max_age'], age)
            if has_eligible:
                self._last_active = dict(observed_monotonic=now, stage=stage, detections=detections,
                                         eligible_detections=eligible, frame_to_submit_ms=age,
                                         inference_path=performance.get('inference_path'),
                                         effective_response_ms=state.get('effective_response_ms'),
                                         control_dt_ms=state.get('control_dt_ms'),
                                         prediction_ready=prediction.get('ready') is True,
                                         estimated_feedback_ms=prediction.get('estimated_feedback_ms'))

    def snapshot(self):
        now = self.clock()
        cutoff = int(now) - self.seconds + 1
        with self._lock:
            counts, stages = Counter(), Counter()
            valid = [b for b in self._buckets if b['second'] >= cutoff]
            for b in valid:
                counts.update(b['counters']); stages.update(b['stages'])
            return dict(window_seconds=self.seconds, counters=dict(counts), stages=dict(stages),
                        maximum_frame_to_submit_ms=max((b['max_age'] for b in valid), default=0.),
                        last_eligible_sample=dict(self._last_active) if self._last_active else None,
                        seconds_since_last_eligible=max(0., round(now-self._last_active['observed_monotonic'], 3)) if self._last_active else None,
                        samples_are_pipeline_loops=True, measured_game_accuracy=False)
