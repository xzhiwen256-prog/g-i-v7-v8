"""UI heartbeat with optional event-only fail-stop: Python stacks only, no devices, Tk calls or locals."""
import sys
import threading
import time
import traceback


def python_stacks():
    frames = sys._current_frames()
    names = {t.ident: t.name for t in threading.enumerate()}
    return [dict(thread_id=ident, name=names.get(ident, 'unknown'),
                 stack=traceback.format_stack(frame, limit=24))
            for ident, frame in list(frames.items())[:24]]


class UiHeartbeat:
    def __init__(self, sink, *, clock=time.monotonic, stack_reader=python_stacks,
                 timeout=3., interval=.5, metadata=None, on_stall=None):
        if timeout <= 0 or interval <= 0:
            raise ValueError('Heartbeat intervals must be positive')
        self.sink, self.clock, self.stack_reader = sink, clock, stack_reader
        self.timeout, self.interval = timeout, interval
        self.metadata = dict(metadata or {})
        self.on_stall = on_stall
        self._lock = threading.Lock()
        self._pulse = clock()
        self._epoch = 0
        self._reported_epoch = None
        self._reports = 0
        self._last_error = None
        self._stop = threading.Event()
        self.thread = None

    def pulse(self):
        with self._lock:
            self._pulse = self.clock()
            self._epoch += 1

    def check_once(self):
        with self._lock:
            age = self.clock() - self._pulse
            if self._stop.is_set() or age < self.timeout or self._reported_epoch == self._epoch:
                return False
            self._reported_epoch = self._epoch
        # No heartbeat lock is held during stack formatting or disk I/O. In
        # particular, never ask the follower for a snapshot from this thread.
        try:
            if self.on_stall is not None:
                self.on_stall()  # Production callback only sets a threading.Event.
            report = dict(kind='ui_heartbeat_stall', ui_silent_seconds=round(age, 3),
                          recorded_unix=time.time(), threshold_seconds=self.timeout,
                          metadata=self.metadata, python_threads=self.stack_reader(),
                          captures_locals=False, memory_dump=False, device_query=False,
                          stop_requested=self.on_stall is not None)
            self.sink(report)
            with self._lock:
                self._reports += 1
                self._last_error = None
            return True
        except Exception as exc:
            with self._lock:
                self._last_error = type(exc).__name__ + ': ' + str(exc)
            return False

    def snapshot(self):
        with self._lock:
            return dict(reports=self._reports, last_error=self._last_error,
                        stopped=self._stop.is_set(), threshold_seconds=self.timeout)

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name='V7-UIHeartbeat', daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            self.check_once()

    def stop(self):
        # Never join from the UI thread, especially if a report sink is slow.
        self._stop.set()
