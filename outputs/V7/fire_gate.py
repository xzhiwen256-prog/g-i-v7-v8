"""Passive firing guard. Does not emit, suppress or delay button events.

The release interval is a controller settling interval, not a weapon recovery
model. Raw press timestamps latch short clicks that polling alone may miss.
"""
import math
import threading


class FireGate:
    def __init__(self):
        self.lock = threading.Lock()
        self.was_down = False
        self.last_press = None
        self.resume_after = 0.
        self.state = 'ready'

    def observe(self, now, down, last_press=None, release_ms=180., captured_at=None):
        if isinstance(now,bool) or not math.isfinite(now) or not isinstance(down, bool):
            raise ValueError('Invalid fire observation')
        if isinstance(release_ms,bool) or not math.isfinite(release_ms) or not 0<=release_ms<=1000:
            raise ValueError('Invalid fire release interval')
        if captured_at is not None and (isinstance(captured_at,bool) or not math.isfinite(captured_at)
                or captured_at<0 or captured_at>now+.005):
            raise ValueError('Invalid captured frame timestamp')
        if last_press is not None and (isinstance(last_press, bool) or
                not math.isfinite(last_press) or last_press < 0 or last_press > now+.005):
            raise ValueError('Invalid physical press timestamp')
        with self.lock:
            fresh_press = last_press is not None and (self.last_press is None or last_press > self.last_press)
            if fresh_press:
                self.last_press = last_press
            if down:
                self.was_down = True
                self.state = 'firing_pause'
                return self.state
            if self.was_down or fresh_press:
                # Conservative: start recovery when release/short-click is seen.
                self.resume_after = max(self.resume_after, now+release_ms/1000.)
            self.was_down = False
            blocked = now < self.resume_after or (captured_at is not None and captured_at < self.resume_after)
            self.state = 'fire_cooldown' if blocked else 'ready'
            return None if self.state == 'ready' else self.state

    def snapshot(self):
        with self.lock:
            return dict(state=self.state, physical_left_down=self.was_down,
                        last_press=self.last_press, resume_after=self.resume_after)
