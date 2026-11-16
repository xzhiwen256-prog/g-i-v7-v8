"""Optional deterministic pacing for small relative corrections.

The V5 planner/gains/budgets are unchanged. An uninterrupted eligible command
is partitioned into at most four reports with the EXACT same integer sum.
Large acquisitions retain the original dispatch path. No background queue,
random movement, native driver modification or detection-evasion logic.
"""
import time
import copy
from cursor_control import CursorFollower


def split_counts(dx, dy, parts=4):
    if (type(dx) is not int or type(dy) is not int or type(parts) is not int
            or not 1 <= parts <= 4):
        raise ValueError('Integer relative counts and 1–4 parts required')
    previous = (0, 0)
    result = []
    for i in range(1, parts + 1):
        point = (round(dx*i/parts), round(dy*i/parts))
        result.append((point[0]-previous[0], point[1]-previous[1]))
        previous = point
    return tuple(result)


class PacedFollower(CursorFollower):
    def __init__(self, pointer, stop_event, clock=time.perf_counter, sleeper=time.sleep):
        super().__init__(pointer, stop_event, clock)
        self.pacing_enabled = False  # Preserve V5 unless the user opts in.
        self._pacing_sleep = sleeper
        self._pacing = dict(split_commands=0, complete_commands=0, interrupted_commands=0,
                            reports_acknowledged=0, scheduling_overruns=0, last=None)

    def set_pacing_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Pacing flag must be boolean')
        with self.lock:
            if self.enabled or self._inflight:
                raise ValueError('请先关闭跟随，再切换修正节奏')
            self.pacing_enabled = enabled

    def pacing_snapshot(self):
        with self.lock:
            result = dict(copy.deepcopy(self._pacing), enabled=self.pacing_enabled,
                          planned_window_ms=3., small_correction_range_counts=[4, 32],
                          scope='acknowledged reports only; not game/display response')
            if result['last'] is not None:
                result['last'] = dict(result['last'])
            return result

    def _dispatch_locked(self, move, args, captured_at, stale_limit):
        if (not self.pacing_enabled or self.mode != 'relative' or self._inflight
                or len(args) != 2 or any(type(x) is not int for x in args)
                or not 4 <= max(abs(x) for x in args) <= 32):
            return super()._dispatch_locked(move, args, captured_at, stale_limit)
        pieces = split_counts(*args)
        generation = self._generation
        start = self.clock()
        interval = .003 / (len(pieces)-1)
        acknowledged = [0, 0]
        reports = 0
        offsets = []
        overrun = False
        complete = False
        self._pacing['split_commands'] += 1

        def submit(dx, dy):
            nonlocal reports
            move(dx, dy)
            acknowledged[0] += dx
            acknowledged[1] += dy
            reports += 1
            offsets.append(round((self.clock()-start)*1000, 3))

        try:
            for i, piece in enumerate(pieces):
                final_tail = False
                if i:
                    deadline = start + interval*i
                    delay = max(0., deadline-self.clock())
                    if delay:
                        # Reserve this generation while releasing the UI/state lock.
                        # This is a short worker sleep, not a Tk sleep or queued tail.
                        self._inflight = True
                        self.lock.release()
                        try:
                            self._pacing_sleep(delay)
                        finally:
                            self.lock.acquire()
                            self._inflight = False
                    if self.clock()-deadline > interval:
                        # A slow backend/scheduler must not create a catch-up burst.
                        # Coalesce the unsent remainder once, with the same guards.
                        piece = tuple(args[a]-acknowledged[a] for a in range(2))
                        overrun = final_tail = True
                if (generation != self._generation or not self.enabled
                        or self._cancel_event.is_set() or self.stop_event.is_set()):
                    return False
                self._poll_hotkeys_locked(live=True)
                if (generation != self._generation or not self.enabled
                        or self._cancel_event.is_set() or self.stop_event.is_set()):
                    return False
                # Original foreground/manual-input/fire/staleness checks before
                # EACH report. A cancelled/stale tail is discarded, never replayed.
                if not super()._dispatch_locked(submit, piece, captured_at, stale_limit):
                    return False
                if final_tail:
                    break
            complete = tuple(acknowledged) == tuple(args)
            return complete
        finally:
            self._inflight = False
            self._pacing['complete_commands' if complete else 'interrupted_commands'] += 1
            self._pacing['reports_acknowledged'] += reports
            self._pacing['scheduling_overruns'] += int(overrun)
            self._pacing['last'] = dict(planned_counts=list(args), acknowledged_counts=list(acknowledged),
                reports=reports, acknowledgment_offsets_ms=offsets, complete=complete, overrun=overrun,
                elapsed_ms=round((self.clock()-start)*1000, 3))
