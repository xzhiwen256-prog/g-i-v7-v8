"""Serialized complete button states with background release; no native APIs.

The single broker still serializes short report exchanges. We never call its
blocking click primitive, queue movement, or sleep for a hold on the caller.
"""
import json
import threading
import time


class ButtonReports:
    def __init__(self,sender,clock=time.perf_counter):
        self.sender=sender;self.clock=clock;self.lock=threading.RLock()
        self.held=False;self.deadline=0.;self.closed=False;self.error=None
        self.wake=threading.Event();self.thread=None

    def _report(self,dx=0,dy=0,expires_at=None):
        command=['mouse',json.dumps(dict(dx=dx,dy=dy,buttons=1 if self.held else 0))]
        if expires_at is None:return self.sender(command)
        return self.sender(command,expires_at=expires_at)

    def _release(self):
        # Set desired state to UP before I/O: failed releases must never allow a
        # following move to reassert DOWN. UP is the only idempotent retry.
        self.held=False;self.deadline=0.
        try:
            return self._report()
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}'
            try:self._report()
            except Exception:pass
            raise

    def check_error(self):
        if self.error:raise OSError('后台松键失败：'+self.error)

    def start(self):
        with self.lock:
            if self.thread is None:
                t=threading.Thread(target=self._run,name='V7ButtonRelease',daemon=True)
                t.start();self.thread=t

    def press(self,expires_at):
        self.start()  # A thread start failure must occur BEFORE any DOWN.
        with self.lock:
            self.check_error()
            if self.closed:raise OSError('Button scheduler closed')
            if self.held:return False
            self.held=True;self.deadline=self.clock()+.040
            try:
                self._report(expires_at=expires_at)
                self.deadline=self.clock()+.040  # Target hold starts at DOWN acknowledgement.
            except Exception:
                # Delivery may be uncertain; try UP, never retry DOWN.
                self._release()
                raise
            finally:self.wake.set()
            return True

    def move(self,dx,dy):
        with self.lock:
            self.check_error()
            if self.closed:raise OSError('Button scheduler closed')
            # A busy stream cannot starve release behind more movement frames.
            if self.held and self.clock()>=self.deadline:self._release()
            return self._report(dx,dy)

    def release(self):
        with self.lock:
            if self.held or self.error:self._release()
            self.wake.set()

    def service(self):
        with self.lock:
            if self.held and self.clock()>=self.deadline:self._release()

    def _run(self):
        while True:
            try:
                with self.lock:
                    if self.closed:return
                    active=self.held
                # Sleep only this release thread; short Event.wait timeouts can
                # have coarse scheduling. Neither path is a real-time promise.
                if active:time.sleep(.001)
                else:self.wake.wait()
                self.wake.clear()
                self.service()
            except Exception:
                # No unsolicited response may corrupt the request/reply FIFO.
                # Stored error is surfaced by the next request. UP was retried.
                return

    def close(self):
        try:self.release()
        finally:
            with self.lock:self.closed=True;self.wake.set();t=self.thread
            if t is not None and t is not threading.current_thread():t.join()
