"""UI-only concurrency utilities. No Tk, capture, model or device imports."""
from collections import deque
from pathlib import Path
import json
import os
import queue
import shutil
import threading
import time
import uuid


class MailboxOverflow(queue.Full):
    pass


class UiMailbox:
    """Reliable FIFO events + one replaceable statistics slot, per run.

    Producers never block for UI consumption. A critical-event overload is an
    explicit fault, not silent eviction: retain accepted events, expose a sticky
    error notice and reject further messages until a new generation is begun.
    """
    def __init__(self, capacity=64):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('Positive event capacity required')
        self.capacity = capacity
        self._lock = threading.Lock()
        self._events = deque()
        self._stats = None
        self._generation = 0
        self._terminal = self._closed = self._overloaded = False
        self._overflow_notice = False
        self._replaced = self._rejected = self._overflows = 0

    @property
    def generation(self):
        with self._lock:
            return self._generation

    def reset(self, generation):
        if type(generation) is not int or generation < 0:
            raise ValueError('Nonnegative generation required')
        with self._lock:
            if self._closed:
                raise RuntimeError('Mailbox is closed')
            if generation <= self._generation:
                raise ValueError('A new run must advance the generation')
            self._generation = generation
            self._events.clear()
            self._stats = None
            self._terminal = self._overloaded = self._overflow_notice = False

    def put_nowait(self, message, generation=None):
        if not isinstance(message, tuple) or not message or not isinstance(message[0], str):
            raise ValueError('A named tuple message is required')
        with self._lock:
            if self._closed or (generation is not None and generation != self._generation):
                self._rejected += 1
                return False
            if self._overloaded:
                self._rejected += 1
                raise MailboxOverflow('UI event channel is already overloaded')
            if message[0] == 'stats':
                if self._terminal:
                    self._rejected += 1
                    return False
                self._replaced += self._stats is not None
                self._stats = message
                return True
            if len(self._events) >= self.capacity:
                self._overloaded = self._overflow_notice = self._terminal = True
                self._overflows += 1
                self._stats = None
                raise MailboxOverflow('UI critical-event capacity exceeded')
            if message[0] in ('error', 'stopped'):
                self._terminal = True
                self._stats = None
            self._events.append(message)
            return True

    def put(self, message):
        return self.put_nowait(message)

    def get_nowait(self):
        with self._lock:
            if self._overflow_notice:
                self._overflow_notice = False
                return ('error', '界面关键消息积压，已请求安全停止；请停止后重新开始')
            if self._events:
                return self._events.popleft()
            if self._stats is not None:
                item, self._stats = self._stats, None
                return item
            raise queue.Empty

    def close(self):
        with self._lock:
            self._closed = True
            self._events.clear()
            self._stats = None
            self._overflow_notice = False

    def snapshot(self):
        with self._lock:
            return dict(generation=self._generation, events=len(self._events),
                        pending_stats=self._stats is not None, capacity=self.capacity,
                        closed=self._closed, overloaded=self._overloaded,
                        stats_replaced=self._replaced, messages_rejected=self._rejected,
                        overloads=self._overflows)


def atomic_text(path, text):
    """Back up an existing local document, then atomically replace it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.parent/'.backups'
        backup.mkdir(exist_ok=True)
        shutil.copy2(path, backup/(path.name+f'.{time.time_ns()}.{uuid.uuid4().hex}.bak'))
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2)+'\n')


class BackgroundShutdown:
    """Wait and release ONLY in a background thread, without forced teardown.

    A slow/hung worker remains a visible waiting phase. Never release its device
    underneath an in-flight operation, kill it, or treat elapsed time as success.
    All callbacks supplied here must be Tk-free.
    """
    def __init__(self, before_wait, workers, after_wait, clock=time.monotonic, finalizer=None):
        self.before_wait = before_wait
        self.workers = tuple(workers)
        self.after_wait = tuple(after_wait)
        self.clock = clock
        self.finalizer = finalizer
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._phase = 'pending'
        self._errors = []
        self._started_at = None
        self.thread = None

    def start(self):
        with self._lock:
            if self.thread is not None:
                return False
            self._started_at = self.clock()
            self.thread = threading.Thread(target=self._run, name='V8Shutdown', daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.thread = None
                self._phase = 'start_failed'
                raise
        return True

    def _step(self, name, callback):
        with self._lock:
            self._phase = name
        try:
            callback()
        except Exception as exc:
            with self._lock:
                self._errors.append(dict(phase=name, error=f'{type(exc).__name__}: {exc}'))

    def _run(self):
        try:
            self._step('disarming', self.before_wait)
            for worker in self.workers:
                if worker is None:
                    continue
                with self._lock:
                    self._phase = 'waiting_for_worker'
                # No timeout-based "success": resources stay owned until exit.
                while worker.is_alive():
                    worker.join(.05)
            for name, callback in self.after_wait:
                self._step(name, callback)
        except Exception as exc:
            # In particular, a failed join must NOT fall through to device close.
            with self._lock:
                self._errors.append(dict(phase=self._phase, error=f'{type(exc).__name__}: {exc}'))
        finally:
            if self.finalizer is not None:
                self._step('recording_errors', self.finalizer)
            with self._lock:
                self._phase = 'done'
            self.done.set()

    def snapshot(self):
        with self._lock:
            return dict(phase=self._phase, done=self.done.is_set(),
                        started=self.thread is not None,
                        elapsed_seconds=max(0., self.clock()-self._started_at) if self._started_at is not None else 0.,
                        errors=[dict(error) for error in self._errors])
