"""Start/reuse the local HID broker, then run the preview with HID selected."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from ui_runtime import atomic_json,atomic_text


class UiLaunchError(RuntimeError):pass


def run_preview(root,argv,*,unavailable=False):
    args=[sys.executable,str(root/'preview.py'),'--input-backend','virtualhid']
    if unavailable:args.append('--input-unavailable')
    if '--cpu' in argv:args+=['--device','cpu']
    # Capture import/Tk errors instead of losing them in a hidden console.
    try:
        with (root/'ui-startup-console.log').open('a',encoding='utf-8') as output:
            output.write('\n=== V7 startup '+time.strftime('%Y-%m-%d %H:%M:%S')+' ===\n');output.flush()
            result=subprocess.Popen(args,cwd=root,creationflags=subprocess.CREATE_NO_WINDOW,
                                    stdout=output,stderr=output).wait()
    except OSError as exc:raise UiLaunchError(str(exc)) from exc
    if result:raise UiLaunchError(f'V7界面退出代码 {result}；详情：{root/"ui-startup-console.log"}')
    return 0


def startup_failure(root, process_id):
    """Ignore old startup errors from previous host processes."""
    try:
        state = json.loads((root / 'hid' / 'app' / 'startup-state.json').read_text(encoding='utf-8'))
        if state.get('pid') == process_id and state.get('state') == 'failed':
            return state.get('error') or '虚拟 HID 设备初始化失败。'
    except (OSError, ValueError, AttributeError):
        pass
    return None


def main():
    root = Path(__file__).resolve().parent
    if not ctypes.windll.shell32.IsUserAnAdmin():
        execute = ctypes.windll.shell32.ShellExecuteW
        execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        execute.restype = ctypes.c_void_p
        args = [str(Path(__file__).resolve()), *sys.argv[1:]]
        result = execute(None, 'runas', sys.executable, subprocess.list2cmdline(args), str(root), 0)
        if not result or result <= 32:
            print('Windows did not approve the administrator launch.')
            return 1
        return 0

    from virtual_hid_backend import VirtualHidClient
    broker = None
    env = os.environ.copy()
    env['DOTNET_ROOT'] = str(root / 'hid' / 'runtime')
    command = [str(root / 'hid' / 'runtime' / 'dotnet.exe'),
               str(root / 'hid' / 'app' / 'VirtualKM.dll')]

    def probe():
        client = VirtualHidClient()
        try:
            return client.request('status')
        finally:
            client.close()

    try:
        try:
            status = probe()
        except OSError:
            # Keep initialization output instead of discarding the actual error.
            with (root / 'hid' / 'app' / 'startup-console.log').open('a', encoding='utf-8') as output:
                broker = subprocess.Popen([*command, 'host-mouse'], cwd=root, env=env,
                                          creationflags=subprocess.CREATE_NO_WINDOW,
                                          stdout=output, stderr=output)
            deadline = time.monotonic() + 60
            exited_at = None
            while True:
                failure = startup_failure(root, broker.pid)
                if failure:
                    raise OSError('虚拟 HID 设备创建失败：\n' + failure +
                                  '\n请退出游戏及鼠标测试窗口后重试；仍失败时保存工作并手动重启 Windows。')
                try:
                    status = probe()
                    break
                except OSError as exc:
                    if broker.poll() is not None:
                        # A concurrent launcher may own the mutex and be starting.
                        exited_at = exited_at or time.monotonic()
                        if time.monotonic() - exited_at > 6:
                            raise OSError('虚拟 HID 进程已退出，请查看 hid/app/startup-console.log。') from exc
                    if time.monotonic() > deadline:
                        raise OSError('等待虚拟 HID 就绪超时（60 秒）。请查看 hid/app/host.log；不要连续重复启动。')
                    time.sleep(.25)
        report = dict(connected=True, owned_broker=broker is not None,
                      broker_pid=broker.pid if broker is not None and broker.poll() is None else None,
                      lifetime='persistent_until_explicit_stop',
                      backend='virtualhid', control_enabled=False,
                      movement_sent=False, status=status, timestamp=time.time())
        (root / '.settings').mkdir(exist_ok=True)
        atomic_json(root / '.settings' / 'hid-launch.json',report)
        (root / 'hid-launch-error.log').unlink(missing_ok=True)
        if '--check' in sys.argv:
            return 0
        return run_preview(root,sys.argv)
    except UiLaunchError as exc:
        atomic_text(root/'ui-startup-error.log',str(exc))
        if '--check' not in sys.argv:ctypes.windll.user32.MessageBoxW(None,str(exc),'V7界面启动失败',0x10)
        return 1
    except Exception as exc:
        atomic_text(root / 'hid-launch-error.log',str(exc))
        (root / '.settings').mkdir(exist_ok=True)
        atomic_json(root / '.settings' / 'hid-launch.json',
            dict(connected=False,error=str(exc),timestamp=time.time(),ui_mode='input_unavailable'))
        if '--check' not in sys.argv:
            try:return run_preview(root,sys.argv,unavailable=True)
            except Exception as ui_error:
                atomic_text(root/'ui-startup-error.log',str(ui_error))
                ctypes.windll.user32.MessageBoxW(None,str(exc)+'\n\n'+str(ui_error),'V7启动失败',0x10)
        return 1
    # A healthy host stays available across preview restarts. Explicit stop is
    # separate because removing a keyboard/mouse while an app holds it can fail.

if __name__ == '__main__':
    raise SystemExit(main())
