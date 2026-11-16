"""Pinned third-party DLL adapter, isolated from the preview in a helper process."""
from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parent
DLL_PATH = ROOT / 'driver' / 'logitech.driver.dll'
DLL_SHA256 = 'c4d46cb25edb0988c4e5635a8aed75f7950cba28347de9e85a168900d1964399'
PREFIX = 'FKDLL_JSON:'


def check_file():
    if not DLL_PATH.is_file() or hashlib.sha256(DLL_PATH.read_bytes()).hexdigest() != DLL_SHA256:
        raise OSError('罗技 DLL 缺失或文件校验不一致。')
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise OSError('此 DLL 需要 64 位 Python。')


class LogitechClient:
    def __init__(self):
        check_file()
        self.lock = threading.RLock()
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '--worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            ready = self._receive(5)
            if not ready.get('ok'):
                raise OSError(ready.get('error', '罗技驱动连接失败。'))
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

    def _receive(self, timeout):
        try:
            result = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise OSError('罗技 DLL 响应超时，控制已停止。') from None
        if result is None:
            raise OSError('罗技 DLL 子进程已退出，控制已停止。')
        return result

    def request(self, op, **values):
        with self.lock:
            if self.process.poll() is not None:
                raise OSError('罗技 DLL 连接已关闭。')
            try:
                self.process.stdin.write(json.dumps(dict(op=op, **values)) + '\n')
                self.process.stdin.flush()
                result = self._receive(.8)
            except (OSError, ValueError):
                self.close()
                raise
            if not result.get('ok'):
                raise OSError(result.get('error', '罗技 DLL 调用失败。'))
            return result

    def move_relative(self, dx, dy):
        return self.request('move', dx=int(dx), dy=int(dy))

    def close(self):
        with self.lock:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write('{"op":"close"}\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=.5)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    self.process.kill()
                    self.process.wait(timeout=2)
            if self.process.stdin:
                self.process.stdin.close()


def send(result):
    print(PREFIX + json.dumps(result, ensure_ascii=True), flush=True)


def worker():
    opened = False
    dll = None
    try:
        check_file()
        dll = ctypes.CDLL(str(DLL_PATH.resolve()), winmode=0x00000800)
        dll.device_open.argtypes = []
        dll.device_open.restype = ctypes.c_int
        dll.device_close.argtypes = []
        dll.device_close.restype = None
        dll.moveR.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_bool]
        dll.moveR.restype = None  # Upstream interface exposes no delivery acknowledgment.
        result = dll.device_open()
        opened = result == 1
        send({'ok': opened, 'device_open_result': result,
              'error': None if opened else 'DLL 已加载，但未能连接兼容的罗技驱动。'})
        if not opened:
            return
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command['op'] == 'close':
                    break
                if command['op'] != 'move':
                    raise ValueError('Unsupported operation')
                dx, dy = int(command['dx']), int(command['dy'])
                if abs(dx) > 127 or abs(dy) > 127:
                    raise ValueError('Movement exceeds adapter limit')
                dll.moveR(dx, dy, True)
                send({'ok': True, 'delivery': 'dll_called_unconfirmed'})
            except Exception as exc:
                send({'ok': False, 'error': str(exc)})
    except Exception as exc:
        send({'ok': False, 'error': f'{type(exc).__name__}: {exc}'})
    finally:
        if opened and dll is not None:
            dll.device_close()


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        client = LogitechClient()
        try:
            print(json.dumps({'connected': True, 'movement_sent': False, 'sha256': DLL_SHA256}))
        finally:
            client.close()
