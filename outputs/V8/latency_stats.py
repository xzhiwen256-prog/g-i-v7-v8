"""Bounded application timings; never a measurement of game/display latency."""
from collections import deque
import math
import statistics
import threading


def summarize(values):
    values = sorted(v for v in values if isinstance(v, (int, float))
                    and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    if not values:
        return None
    return dict(p50=round(statistics.median(values), 3),
                p95=round(values[math.ceil(.95*len(values))-1], 3),
                max=round(values[-1], 3))


class LatencyStats:
    def __init__(self, capacity=240):
        self.samples = deque(maxlen=capacity)
        self.lock = threading.Lock()

    def add(self, sample):
        copied = dict(sample)
        if isinstance(copied.get('inference_stages'), dict):
            copied['inference_stages'] = dict(copied['inference_stages'])
        with self.lock:
            self.samples.append(copied)

    def snapshot(self):
        with self.lock:
            samples = list(self.samples)
        stages = {}
        for key in ('capture_ms','inference_ms','filter_ms','control_ms','render_ms',
                    'capture_to_submit_ms','frame_to_submit_ms','frame_age_at_capture_ms','process_ms','pacing_ms','loop_ms'):
            summary = summarize(s[key] for s in samples if key in s and
                (key not in ('inference_ms','capture_to_submit_ms','process_ms') or s.get('new_frame', True)))
            if summary:
                stages[key] = summary
        for source in ('control_ms','capture_to_submit_ms','frame_to_submit_ms'):
            summary = summarize(s[source] for s in samples if s.get('submitted') and source in s)
            if summary:
                stages['submitted_'+source] = summary
        paths = {}
        for s in samples:
            if s.get('inference_reused', False):
                continue
            path = s.get('inference_path', 'unknown')
            path = path if isinstance(path, str) and 0 < len(path) <= 80 else 'unknown'
            paths.setdefault(path, []).append(s)
        inference_paths = {}
        for path, group in paths.items():
            summary = summarize(s.get('inference_ms') for s in group)
            if summary:
                details = {}
                for key in ('forward_gpu_ms', 'fp32_guard_ms', 'total_ms'):
                    values = [s['inference_stages'].get(key) for s in group
                              if isinstance(s.get('inference_stages'), dict)]
                    detail = summarize(values)
                    if detail:
                        details[key] = detail
                inference_paths[path] = dict(samples=len(group), inference_ms=summary, stages=details)
        return dict(samples=len(samples), stages=stages, inference_paths=inference_paths,
                    fresh_frames=sum(bool(s.get('new_frame')) for s in samples),
                    inference_statistics='fresh frames only',
                    inference_path_statistics='executed inferences only, including parameter-triggered re-inference; cached repeats excluded',
                    processing_statistics='fresh frames only; submitted_* stages include actual submissions only',
                    submitted_frames=sum(s.get('submitted', False) for s in samples),
                    timing_scope='capture_request_to_control_return; frame_to_submit uses DXGI presentation QPC or MSS completion estimate; game input/display response excluded')
