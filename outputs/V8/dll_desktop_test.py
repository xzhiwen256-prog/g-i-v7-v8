"""Interactive comparison: user starts bounded movements inside this test window."""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from cursor_control import DesktopPointer, enable_physical_coordinates

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / 'verification' / 'logitech-desktop-comparison.json'


class DesktopTest:
    def __init__(self, root):
        self.root = root
        self.standard = DesktopPointer()
        self.logitech = DesktopPointer()
        self.logitech.set_backend('logitech')
        self.api = self.standard.api
        self.api.GetForegroundWindow.restype = ctypes.c_void_p
        self.api.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.api.GetAncestor.restype = ctypes.c_void_p
        self.active = False
        self.run_id = 0
        self.original = None
        self.report = {}
        root.title('罗技 DLL 桌面移动对比测试')
        root.geometry('650x430+80+80')
        root.resizable(False, False)
        root.attributes('-topmost', True)
        frame = ttk.Frame(root, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='桌面移动对比测试', font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='点击开始后请松开鼠标，倒计时 3 秒后自动测试。\n在本窗口内各测试一次小幅向右移动，不点击，完成后恢复原位置。',
                  font=('Microsoft YaHei UI', 11), wraplength=600).pack(anchor='w', pady=12)
        self.canvas = tk.Canvas(frame, width=600, height=110, background='#111827', highlightthickness=0)
        self.canvas.pack(fill='x')
        self.canvas.create_line(292, 55, 308, 55, fill='#93c5fd', width=2)
        self.canvas.create_line(300, 47, 300, 63, fill='#93c5fd', width=2)
        self.canvas.create_text(300, 90, text='光标测试区域', fill='#cbd5e1', font=('Microsoft YaHei UI', 10))
        self.status = tk.StringVar(value='等待你点击开始。测试不会自动启动。')
        ttk.Label(frame, textvariable=self.status, wraplength=600, font=('Microsoft YaHei UI', 11)).pack(anchor='w', pady=14)
        bar = ttk.Frame(frame)
        bar.pack(fill='x')
        self.button = ttk.Button(bar, text='开始测试', command=self.begin)
        self.button.pack(side='left')
        ttk.Button(bar, text='取消 / 停止', command=self.cancel).pack(side='left', padx=12)
        ttk.Label(bar, text='F8 也可停止').pack(side='left')
        root.bind('<F8>', lambda event: self.cancel())
        root.protocol('WM_DELETE_WINDOW', self.close)

    def foreground(self):
        return self.api.GetForegroundWindow() == self.api.GetAncestor(self.root.winfo_id(), 2)

    def save(self):
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding='utf-8')

    def begin(self):
        if self.active:
            return
        self.report = {'started_utc': datetime.now(timezone.utc).isoformat(),
                       'completed': False, 'game_tested': False, 'requested_delta': [8, 0]}
        if not self.foreground():
            self.status.set('请点击本窗口的“开始测试”，取得前台后再试。尚未发送移动。')
            return
        self.original = self.standard.position()
        self.active = True
        self.run_id += 1
        self.button.configure(state='disabled')
        self.countdown(3)

    def later(self, delay, callback):
        run_id = self.run_id
        def guarded():
            if not self.active or self.run_id != run_id:
                return
            try:
                callback()
            except Exception as exc:
                self.fail(f'{type(exc).__name__}: {exc}')
        self.root.after(delay, guarded)

    def countdown(self, seconds):
        if seconds:
            self.status.set(f'{seconds} 秒后开始，请松开鼠标并保持本窗口在前台…')
            self.later(1000, lambda: self.countdown(seconds - 1))
        else:
            self.measure('windows', self.standard, self.after_windows)

    def measure(self, name, pointer, next_step):
        if not self.active:
            return
        try:
            if not self.foreground():
                raise OSError('测试窗口失去前台，已停止发送。')
            start = (self.canvas.winfo_rootx() + 300, self.canvas.winfo_rooty() + 55)
            self.standard.move_to(*start)
            before = self.standard.position()
            if before != start:
                raise OSError('桌面光标被限制，未发送测试移动。')
            self.status.set('正在测试 ' + ('Windows 标准输入…' if name == 'windows' else '罗技 DLL…'))
            samples = []
            def settle():
                if self.standard.position() != before:
                    raise OSError('发出测试指令前检测到额外光标移动，请松开鼠标后重试。')
                if not self.foreground():
                    raise OSError('测试窗口失去前台，未发送移动。')
                pointer.move_relative(8, 0)
                self.later(20, collect)
            def collect():
                after = self.standard.position()
                delta = [after[0] - before[0], after[1] - before[1]]
                samples.append(delta)
                if not self.foreground():
                    self.fail('测试窗口失去前台，本次结果不作结论。')
                    return
                if len(samples) < 5:
                    self.later(20, collect)
                    return
                self.report[name] = {'before': before, 'after': after, 'samples': samples,
                    'observed_delta': delta,
                    'rightward_movement_observed': any(dx > 0 and abs(dy) <= 1 for dx, dy in samples),
                    'unexpected_direction': any(dx < 0 or abs(dy) > 1 for dx, dy in samples),
                    'foreground_retained': True}
                next_step()
            self.later(120, settle)
        except Exception as exc:
            self.fail(f'{type(exc).__name__}: {exc}')

    def after_windows(self):
        if not self.active:
            return
        try:
            self.report['connection'] = self.logitech.check_backend()
            self.later(100, lambda: self.measure('logitech', self.logitech, self.finish))
        except Exception as exc:
            self.fail(f'{type(exc).__name__}: {exc}')

    def finish(self):
        windows = self.report['windows']['rightward_movement_observed']
        logitech = self.report['logitech']['rightward_movement_observed']
        unexpected = any(self.report[name]['unexpected_direction'] for name in ('windows', 'logitech'))
        if unexpected:
            self.report['conclusion'] = 'unexpected_movement_inconclusive'
            text = '出现额外方向的光标移动，本次不能判定。请松开鼠标并停止其他鼠标控制程序后重试。'
        elif logitech:
            self.report['conclusion'] = 'dll_desktop_movement_observed'
            text = '已观察到罗技 DLL 移动桌面光标。游戏是否接收这种输入仍需另行确认。'
        elif windows:
            self.report['conclusion'] = 'windows_moves_but_dll_does_not'
            text = 'Windows 标准输入能移动，但未观察到罗技 DLL 移动。连接成功不代表移动有效。'
        else:
            self.report['conclusion'] = 'no_movement_observed'
            text = '两种方式都未观察到向右移动，本次不能确定游戏问题所在。'
        self.report['completed'] = True
        self.end(text)

    def fail(self, message):
        self.report['error'] = message
        self.end('测试未完成：' + message)

    def end(self, text):
        self.active = False
        self.run_id += 1
        try:
            if self.original is not None:
                self.standard.move_to(*self.original)
                self.report['cursor_restored'] = self.standard.position() == self.original
        except OSError as exc:
            self.report['restore_error'] = str(exc)
        finally:
            self.logitech.close()
            self.original = None
            self.save()
            self.status.set(text)
            self.button.configure(state='normal')

    def cancel(self):
        if self.active:
            self.report['cancelled'] = True
            self.end('测试已停止，结果已保存。')

    def close(self):
        self.cancel()
        self.logitech.close()
        self.root.destroy()


if __name__ == '__main__':
    enable_physical_coordinates()
    root = tk.Tk()
    DesktopTest(root)
    root.mainloop()
