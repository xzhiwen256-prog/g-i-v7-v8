"""Customer-confirmed elevated helper. No input self-tests or silent trust changes."""
from pathlib import Path
import argparse
import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from usb_support import atomic_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--nonce', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{32}', args.nonce):
        raise ValueError('Invalid launch nonce')
    request = json.loads(args.request.read_text('utf-8'))
    if request.get('nonce') != args.nonce or Path(request.get('install_root','')).resolve() != ROOT:
        raise ValueError('Launch request mismatch')
    state_path = args.request.with_suffix('.state.json')
    def state(kind, **values):
        atomic_json(state_path, dict(nonce=args.nonce, state=kind, recorded_unix=time.time(), **values))
    kernel = None
    mutex = None
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise PermissionError('未获得管理员权限，未安装输入组件')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None, False, r'Local\V7USB.Client.Control.v1')
        if not mutex:
            raise OSError('无法建立本机单实例保护')
        if ctypes.get_last_error() == 183:
            state('already_running', game_input_verified=False)
            return 0
        component = ROOT/'app/hid/app/HIDMaestro.Core.dll'
        with component.open('rb') as stream:
            component_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        if request.get('driver_component_sha256') != component_hash or request.get('explicit_install_consent') is not True:
            raise PermissionError('缺少此输入组件的明确安装同意；已停止')
        state('configuring_input')
        sys.path.insert(0, str(ROOT/'app'))
        import launch_windows_admin
        old_args = sys.argv
        try:
            sys.argv = [str(ROOT/'app/launch_windows_admin.py'), '--check']
            code = launch_windows_admin.main()
        finally:
            sys.argv = old_args
        if code:
            error = ROOT/'app/hid-launch-error.log'
            raise RuntimeError(error.read_text('utf-8') if error.exists() else '输入组件配置失败，未启动识别')
        info = json.loads((ROOT/'app/.settings/hid-launch.json').read_text('utf-8'))
        if info.get('connected') is not True:
            raise RuntimeError('输入后台未确认连接，未启动识别')
        env = os.environ.copy()
        for key in ('PYTHONHOME','PYTHONPATH'):
            env.pop(key, None)
        env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', YOLOv5_AUTOINSTALL='false',
                   YOLO_OFFLINE='true', ULTRALYTICS_OFFLINE='true')
        mode = request.get('mode', 'auto')
        if mode not in ('auto','torch','cpu'):
            raise ValueError('Unsupported launch mode')
        if mode in ('torch','cpu'):
            env['FK_INFERENCE_BACKEND'] = 'torch'
        else:
            env.pop('FK_INFERENCE_BACKEND', None)
        command = [str(ROOT/'runtime/pythonw.exe'), '-B', '-E', '-s', str(ROOT/'app_entry.py'),
                   '--input-backend','virtualhid','--device', 'cpu' if mode == 'cpu' else 'auto']
        latest = json.loads(args.request.read_text('utf-8'))
        if latest.get('cancelled') is True:
            raise InterruptedError('启动已取消，未开启 V7 窗口')
        child = subprocess.Popen(command, cwd=ROOT/'app', env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        state('launched', pid=child.pid, input_backend_connected=True, game_input_verified=False,
              automatic_recognition=False, automatic_follow=False)
        # Briefly detect immediate interpreter/UI failures; never start a model.
        time.sleep(2)
        if child.poll() is not None and child.returncode:
            state('failed', error='V7 窗口启动后立即退出，请查看本机日志', exit_code=child.returncode)
            return 1
        child.wait()  # Retain the controller mutex for the entire V7 lifetime.
        return child.returncode
    except Exception as exc:
        state('failed', error=str(exc), detail=traceback.format_exc(), game_input_verified=False)
        return 1
    finally:
        if kernel is not None and mutex:
            kernel.CloseHandle(mutex)


if __name__ == '__main__':
    raise SystemExit(main())
