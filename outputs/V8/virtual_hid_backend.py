"""Relative movement through the local VirtualKM HID broker.

Blocking pipe I/O runs in a disposable helper so a failed broker cannot hang
the preview. Each request has its own connection; other local clients remain
able to check status, audit or stop the broker between frames.
"""
from __future__ import annotations
from functools import lru_cache
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import ctypes

PIPE = r'\\.\pipe\Colin.VirtualKM.Local.v1'
PREFIX = 'FKHID_JSON:'


class TriggerExpired(OSError):
    """An unsent stale click is a cancellation, not a broken input device."""


class VirtualHidClient:
    def __init__(self):
        self.lock = threading.RLock()
        self._cancel_event=threading.Event();self._cancel_stop=threading.Event()
        self._cancel_thread=None;self._background_error=None
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            ready = self._receive(3)
            if not ready.get('ok'):
                raise OSError(ready.get('error', '虚拟 HID 未就绪。'))
            t=threading.Thread(target=self._cancel_loop,name='V7CancelButton',daemon=True)
            t.start();self._cancel_thread=t
        except Exception:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            if line.startswith(PREFIX):
                try:
                    self.messages.put(json.loads(line[len(PREFIX):]))
                except ValueError:
                    pass
        self.messages.put(None)
        if not self._cancel_stop.is_set():self._background_error='虚拟 HID 辅助进程已退出'

    def _receive(self, timeout):
        try:
            result = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise OSError('虚拟 HID 响应超时，控制已停止。') from None
        if result is None:
            raise OSError('虚拟 HID 连接已退出，请重新检查连接。')
        return result

    def request(self, op, **values):
        with self.lock:
            if self.process.poll() is not None:
                raise OSError('虚拟 HID 连接已关闭，请重新检查连接。')
            try:
                self.process.stdin.write(json.dumps(dict(op=op, **values)) + '\n')
                self.process.stdin.flush()
                result = self._receive(.8)
                if not result.get('ok'):
                    raise OSError(result.get('error', '虚拟 HID 请求失败。'))
                return result
            except (OSError, ValueError):
                self._terminate()
                raise

    def move_relative(self, dx, dy):
        dx, dy = int(dx), int(dy)
        if abs(dx) > 32767 or abs(dy) > 32767:
            raise OSError('虚拟 HID 每帧位移超出范围。')
        return self.request('move', dx=dx, dy=dy)

    def trigger_click(self, guard, expires_at):
        with self.lock:
            event=getattr(self,'_cancel_event',None)
            if event is not None and event.is_set():
                event.clear();self.request('trigger_release')
            if guard() is not True:return False
            result=self.request('trigger_click', expires_at=expires_at)
            return result.get('delivery')=='hid_submitted'

    def cancel_trigger(self):
        self._cancel_event.set()  # No pipe/lock wait on the Tk caller.

    def _cancel_loop(self):
        while True:
            self._cancel_event.wait()
            if self._cancel_stop.is_set():return
            try:
                with self.lock:
                    if self._cancel_stop.is_set():return
                    if self._cancel_event.is_set():
                        self._cancel_event.clear();self.request('trigger_release')
            except Exception as exc:
                self._background_error=f'松键请求失败：{exc}';return

    def close(self):
        self._cancel_stop.set();self._cancel_event.set()
        t=self._cancel_thread
        if t is not None and t is not threading.current_thread():t.join()
        with self.lock:
            try:
                if self.process.poll() is None:self.request('trigger_release')
            finally:self._terminate()

    def _terminate(self):
        with self.lock:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write('{"op":"close"}\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=.3)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdin:
                self.process.stdin.close()


@lru_cache(maxsize=1)
def _wait_pipe_api():
    """Cache only the immutable API binding, never connection or input state."""
    wait_pipe = ctypes.WinDLL('kernel32', use_last_error=True).WaitNamedPipeW
    wait_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    wait_pipe.restype = ctypes.c_int
    return wait_pipe

def exchange(command, expires_at=None):
    # FileIO supports Windows named pipes. The parent enforces a timeout
    # and terminates this helper if connect/write/read ever blocks.
    deadline = time.monotonic() + .4
    wait_pipe = _wait_pipe_api()
    while True:
        if expires_at is not None and time.perf_counter()>=expires_at:
            raise TriggerExpired('自动扳机帧已过期，未提交。')
        try:
            connection = open(PIPE, 'r+b', buffering=0)
            break
        except OSError as exc:
            # The single-instance broker closes and recreates its pipe
            # between requests. Retry only before writing, never after a send.
            if exc.errno not in (2, 22) or time.monotonic() >= deadline:
                raise
            wait_pipe(PIPE,1)
            time.sleep(.00025)
    with connection as pipe:
        if expires_at is not None and time.perf_counter()>=expires_at:
            raise TriggerExpired('自动扳机帧已过期，未提交。')
        payload = (json.dumps(command) + '\n').encode('utf-8')
        sent = 0
        while sent < len(payload):
            n = pipe.write(payload[sent:])
            if not n:
                raise OSError('虚拟 HID 管道已断开。')
            sent += n
        # Buffer only the reply; keep request writes immediate and unbuffered.
        # This connection carries one exchange and is discarded afterward.
        with io.BufferedReader(pipe, buffer_size=4096) as reader:
            reply = reader.readline(4096).decode('utf-8').strip()
    if not reply.startswith('OK'):
        raise OSError(reply or '虚拟 HID 未返回确认。')
    return reply


def send(value):
    print(PREFIX + json.dumps(value, ensure_ascii=True), flush=True)


def worker():
    from button_reports import ButtonReports
    reports=ButtonReports(exchange)
    try:
        status = exchange(['status'])
        send({'ok': True, 'status': status})
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if request['op'] == 'close':
                    return
                if request['op'] == 'status':
                    reports.check_error()
                    reply = exchange(['status'])
                elif request['op'] == 'move':
                    dx, dy = int(request['dx']), int(request['dy'])
                    if abs(dx) > 32767 or abs(dy) > 32767:
                        raise ValueError('Movement exceeds HID report range')
                    reply=reports.move(dx,dy)
                elif request['op']=='trigger_release':
                    reports.release();reply='OK: release processed'
                elif request['op'] == 'trigger_click':
                    expiry=float(request['expires_at'])
                    import math
                    if not math.isfinite(expiry) or expiry-time.perf_counter()>.045:
                        raise ValueError('Invalid trigger deadline')
                    if expiry<=time.perf_counter():raise TriggerExpired('Unsent trigger expired')
                    if not reports.press(expiry):
                        send({'ok':True,'status':'pulse already active','delivery':'cancelled'});continue
                    reply='OK: button down submitted; release scheduled'
                else:
                    raise ValueError('Unsupported operation')
                send({'ok': True, 'status': reply, 'delivery': 'hid_submitted'})
            except TriggerExpired as exc:
                send({'ok':True,'status':str(exc),'delivery':'cancelled'})
            except Exception as exc:
                send({'ok': False, 'error': str(exc)})
    except Exception as exc:
        send({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
    finally:
        try:reports.close()
        except Exception:pass


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        client = VirtualHidClient()
        try:
            print(json.dumps(client.request('status'), ensure_ascii=False))
        finally:
            client.close()
