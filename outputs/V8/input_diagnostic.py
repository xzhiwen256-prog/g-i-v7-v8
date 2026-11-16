"""Passive input-path diagnostics. Does not send input or inspect game memory."""
import ctypes
from functools import lru_cache
from datetime import datetime
import json
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _foreground_apis():
    """Cache signatures, not foreground identities. Never send window messages."""
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetForegroundWindow.argtypes = []
    user.GetForegroundWindow.restype = ctypes.c_void_p
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user.GetWindowThreadProcessId.restype = ctypes.c_ulong
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
                                                ctypes.POINTER(ctypes.c_ulong)]
    kernel.QueryFullProcessImageNameW.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    return user, kernel


def foreground_identity():
    """Read executable identity without consulting the foreground window thread.

    GetWindowText on our own Tk window synchronously sends WM_GETTEXT. Calling
    it while owning the controller lock can deadlock against Tk's state poll.
    Titles are not authorization evidence and are deliberately never queried.
    """
    user, kernel = _foreground_apis()
    hwnd = user.GetForegroundWindow()
    pid = ctypes.c_ulong()
    if not hwnd or not user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        return dict(pid=0, title='', process='')
    process = ''
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if handle:
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_ulong(len(path))
            if kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                process = path.value
        finally:
            kernel.CloseHandle(handle)
    return dict(pid=pid.value, title='', process=process)


def foreground():
    # Compatibility shape for passive diagnostics; do not reinstate title reads.
    return foreground_identity()


def classify(before,after,raw,windows,error=None):
    delta=after.get('sent',0)-before.get('sent',0)
    if error:return 'diagnostic_error','诊断未完成，请查看 error 字段。'
    if before.get('backend')!='virtualhid' or after.get('backend')!='virtualhid':
        return 'different_backend','本次并非虚拟 HID 后端，需按实际输入方式排查。'
    if delta<=0:return 'no_output','采样期间未增加 HID 提交次数，请检查跟随开关、目标和状态。'
    if not raw or raw.get('events',0)<=0:
        return 'no_raw_input','HID 提交计数增加，但这次观察未收到对应虚拟鼠标 Raw Input。需排查设备传输和接收链路。'
    games=[w for w in windows if 'valorant' in (w.get('process','')+' '+w.get('title','')).lower()]
    if not games:return 'game_not_observed_foreground','Windows 收到了虚拟鼠标 Raw Input，但采样期间未观察到 VALORANT 在前台。'
    if len(games)<len(windows)*.9:return 'foreground_changed','Windows 收到了 Raw Input，但前台窗口在采样中发生变化，不能将全部输入归入游戏前台时段。'
    return 'windows_received_game_consumption_unknown','游戏保持前台且 Windows 收到虚拟鼠标 Raw Input。若准心仍不动，下一步排查游戏的设备选择/输入策略；本报告无法读取游戏是否实际消费输入。'


def collect(state_reader,seconds=5,stop=None):
    from replay_video_mouse import broker_command
    before=state_reader();windows=[];audit=False;raw=None;error=None
    began=time.monotonic()
    try:
        broker_command('audit-start');audit=True
        while time.monotonic()-began<seconds:
            if stop is not None and stop.is_set():break
            windows.append(foreground())
            time.sleep(.1)
        raw=json.loads(broker_command('audit-stop')[3:]);audit=False
    except Exception as exc:error=f'{type(exc).__name__}: {exc}'
    finally:
        if audit:
            try:raw=json.loads(broker_command('audit-stop')[3:])
            except Exception:pass
    after=state_reader()
    result,message=classify(before,after,raw,windows,error)
    report=dict(timestamp=datetime.now().astimezone().isoformat(),duration_seconds=round(time.monotonic()-began,2),
                before=before,after=after,hid_submissions_delta=after.get('sent',0)-before.get('sent',0),
                virtual_mouse_raw_input=raw,foreground_samples=windows,result=result,message=message,error=error,
                diagnostic_sent_input=False,game_consumption_verified=False)
    output=ROOT/'verification'/'game-input-diagnostic.json'
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report
