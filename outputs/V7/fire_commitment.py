"""Passive fire-episode commitment clock; never emits or suppresses input.

Held fire protects the currently tracked target. Release or an observed short
click extends protection for 350 ms. The owner decides what to do when that
same target cannot be reliably associated; this object does NOT identify a
player, infer a hit/kill, select a target, or modify movement strength.
"""
import math
import threading


def _time(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0.):
        raise ValueError(name + ' must be a finite nonnegative timestamp')
    return float(value)


class FireCommitment:
    release_guard_ms = 350.
    revision = 'fire-target-commitment-20261111'

    def __init__(self):
        self.lock = threading.Lock()
        self.last_press = None
        self.held = False
        self.protected_until = 0.
        self.last_update = None
        self.source = 'none'

    def _check_order(self, now):
        if self.last_update is not None and now < self.last_update:
            raise ValueError('Fire timestamp must be monotonic')

    def observe(self, now, down, last_press=None):
        now = _time(now, 'Fire observation')
        if type(down) is not bool:
            raise ValueError('Fire button state must be boolean')
        if last_press is not None:
            last_press = _time(last_press, 'Physical press')
            if last_press > now + .005:
                raise ValueError('Physical press cannot be in the future')
        with self.lock:
            self._check_order(now)
            fresh = last_press is not None and (self.last_press is None or last_press > self.last_press)
            deadline = self.protected_until
            source = self.source
            if down:
                deadline = max(deadline, now + self.release_guard_ms / 1000.)
                source = 'physical_held'
            elif self.held:
                deadline = max(deadline, now + self.release_guard_ms / 1000.)
                source = 'physical_release'
            elif fresh:
                # Use the event's true timestamp. A late first poll must not
                # revive an old click after F8, foreground changes or startup.
                deadline = max(deadline, last_press + self.release_guard_ms / 1000.)
                source = 'physical_short_click'
            self.held = down
            self.protected_until = deadline
            self.source = source
            self.last_update = now
            if fresh:
                self.last_press = last_press
            return self.held or now < self.protected_until

    def shot(self, now):
        """Latch an acknowledged automatic press without pretending it is held."""
        now = _time(now, 'Submitted shot')
        with self.lock:
            self._check_order(now)
            self.protected_until = max(self.protected_until, now + self.release_guard_ms / 1000.)
            self.last_update = now
            self.source = 'automatic_shot'
            return True

    def reset(self, last_press=None):
        """Release commitment, preserving the raw-event watermark by default.

        An explicit raw last_press can advance the baseline at arm/disarm. It
        cannot rewind it and turn an already consumed short click into a new one.
        The owner must only pass a known passive-monitor timestamp, not infer it.
        """
        if last_press is not None:
            last_press = _time(last_press, 'Physical press baseline')
        with self.lock:
            if last_press is not None and (self.last_press is None or last_press > self.last_press):
                self.last_press = last_press
            self.held = False
            self.protected_until = 0.
            self.last_update = None
            self.source = 'none'

    def snapshot(self, now):
        """Return detached diagnostics without advancing the observation clock.

        UI snapshots may have been timestamped before a concurrently completed
        observation. They are read-only and do not invalidate valid input data.
        While held, remaining_ms denotes the minimum post-release protection,
        not a promised automatic release of the commitment.
        """
        now = _time(now, 'Snapshot')
        with self.lock:
            remaining = (self.release_guard_ms if self.held else
                         max(0., self.protected_until - now) * 1000.)
            return dict(active=self.held or now < self.protected_until,
                        remaining_ms=round(remaining, 3), held=self.held,
                        release_guard_ms=self.release_guard_ms,
                        protected_until=self.protected_until,
                        last_press=self.last_press, last_update=self.last_update,
                        source=self.source, revision=self.revision,
                        input_emitted=False, identity_or_kill_detection=False)
