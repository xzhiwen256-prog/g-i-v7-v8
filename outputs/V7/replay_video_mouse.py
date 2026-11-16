"""Replay offline dx/dy through the real virtual HID mouse, with a stop path."""
from __future__ import annotations
import argparse
import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

ROOT=Path(__file__).resolve().parent


def latest_data():
    paths=list((ROOT/'offline-results').glob('*/mouse-data.jsonl'))
    return max(paths,key=lambda p:p.stat().st_mtime) if paths else None


def load_data(path):
    frames=[]
    for number,line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(),1):
        if not line.strip():continue
        row=json.loads(line)
        if set(row)-{'dx','dy','dt_ms'}:
            raise ValueError(f'第 {number} 行只允许 dx/dy/dt_ms。')
        dx,dy,dt=row['dx'],row['dy'],row.get('dt_ms',0)
        if any(type(x) is not int for x in (dx,dy,dt)) or abs(dx)>32767 or abs(dy)>32767 or not 0<=dt<=60000:
            raise ValueError(f'第 {number} 行位移或时间无效。')
        frames.append(dict(dx=dx,dy=dy,dt_ms=dt))
    if not frames:raise ValueError('没有可回放的位移数据。')
    return frames


def broker_command(*args):
    env=os.environ.copy();env['DOTNET_ROOT']=str(ROOT/'hid'/'runtime')
    p=subprocess.run([str(ROOT/'hid'/'runtime'/'dotnet.exe'),str(ROOT/'hid'/'app'/'VirtualKM.dll'),*args],
                     env=env,capture_output=True,text=True,encoding='utf-8',timeout=5,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode:raise OSError(p.stderr.strip() or p.stdout.strip())
    return p.stdout.strip()


def replay(path,stop=None,notify=None,countdown=3):
    from virtual_hid_backend import VirtualHidClient
    path=Path(path).resolve();frames=load_data(path)
    stop=stop if stop is not None else threading.Event()
    getkey=ctypes.WinDLL('user32').GetAsyncKeyState
    getkey.argtypes=[ctypes.c_int];getkey.restype=ctypes.c_short
    def cancelled():return stop.is_set() or bool(getkey(0x77)&0x8000)
    def note(kind,*values):
        if notify:notify(kind,*values)
    def wait_until(deadline):
        while time.perf_counter()<deadline:
            if cancelled():return False
            stop.wait(min(.01,max(0,deadline-time.perf_counter())))
        return not cancelled()
    client=None;audit=False;sent=0;sx=sy=0;raw=None;error=None;interrupted=False
    began=time.perf_counter()
    try:
        try:client=VirtualHidClient()
        except OSError:
            note('status','正在启动虚拟 HID；若出现 UAC 提示请选择“是”…')
            subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'hid'/'Start.ps1')],creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.perf_counter()+20
            while client is None:
                if cancelled():interrupted=True;return None
                try:client=VirtualHidClient()
                except OSError:
                    if time.perf_counter()>deadline:raise OSError('虚拟 HID 未就绪，请先运行 hid/Start.cmd。')
                    stop.wait(.25)
        for remaining in range(countdown,0,-1):
            note('status',f'{remaining} 秒后实际驱动鼠标；F8 或停止按钮取消')
            if not wait_until(time.perf_counter()+1):interrupted=True;return None
        broker_command('audit-start');audit=True
        began=time.perf_counter();deadline=began
        for index,frame in enumerate(frames):
            deadline+=frame['dt_ms']/1000
            if not wait_until(deadline):interrupted=True;break
            client.move_relative(frame['dx'],frame['dy'])
            sent+=1;sx+=frame['dx'];sy+=frame['dy']
            note('frame',index,frame,sent,len(frames))
        # Let the broker's UI drain the final Raw Input messages before auditing.
        stop.wait(.15)
        raw=json.loads(broker_command('audit-stop')[3:]);audit=False
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}'
    finally:
        if audit:
            try:raw=json.loads(broker_command('audit-stop')[3:])
            except Exception:pass
        if client is not None:client.close()
        report=dict(status='error' if error else 'stopped' if interrupted else 'complete',
                    source=str(path),frames_sent=sent,total_frames=len(frames),
                    input_sent=sent>0,sent_dx=sx,sent_dy=sy,raw_input=raw,
                    raw_input_matches=bool(raw and raw['dx']==sx and raw['dy']==sy and (sent==0 or raw['events']>0)),
                    elapsed_seconds=round(time.perf_counter()-began,3),error=error,
                    timestamp=datetime.now().astimezone().isoformat())
        reports=ROOT/'verification';reports.mkdir(exist_ok=True)
        (reports/'video-mouse-replay.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        note('done',report)
    return report


def gui(initial=None):
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    import cv2
    from PIL import Image,ImageTk
    root=tk.Tk();root.title('视频轨迹 → 实际虚拟 HID 鼠标');root.geometry('900x850')
    selected=tk.StringVar(value=str(initial or latest_data() or ''))
    status=tk.StringVar(value='点击回放后，桌面鼠标将随已计算的 dx/dy 实际移动。不会点击。')
    panel=ttk.Frame(root,padding=15);panel.pack(fill='both',expand=True)
    ttk.Label(panel,text='驱动鼠标回放 · F8 随时停止',font=('Microsoft YaHei UI',17)).pack(anchor='w',pady=8)
    bar=ttk.Frame(panel);bar.pack(fill='x')
    ttk.Entry(bar,textvariable=selected).pack(side='left',fill='x',expand=True)
    def choose():
        p=filedialog.askopenfilename(title='选择离线结果中的 mouse-data.jsonl',filetypes=[('轨迹','*.jsonl')])
        if p:selected.set(p)
    ttk.Button(bar,text='选择轨迹',command=choose).pack(side='left',padx=5)
    controls=ttk.Frame(panel);controls.pack(fill='x',pady=12)
    messages=queue.Queue();stop=threading.Event();worker=None;cap=None;photo=None
    def start():
        nonlocal worker,cap
        if worker is not None and worker.is_alive():return
        try:load_data(selected.get())
        except Exception as exc:messagebox.showerror('轨迹错误',str(exc));return
        if cap is not None:cap.release()
        cap=cv2.VideoCapture(str(Path(selected.get()).parent/'annotated.mp4'))
        stop.clear();play.configure(state='disabled');path=selected.get()
        worker=threading.Thread(target=lambda:replay(path,stop,lambda *a:messages.put(a)),daemon=True)
        worker.start()
    play=ttk.Button(controls,text='倒计时 3 秒 → 驱动鼠标回放',command=start);play.pack(side='left')
    ttk.Button(controls,text='立即停止 / F8',command=stop.set).pack(side='left',padx=12)
    ttk.Label(panel,textvariable=status,wraplength=850).pack(anchor='w',pady=8)
    canvas=tk.Label(panel,bg='#10151c');canvas.pack(fill='both',expand=True)
    def poll():
        nonlocal photo
        try:
            while True:
                item=messages.get_nowait()
                if item[0]=='status':status.set(item[1])
                elif item[0]=='frame':
                    _,index,frame,sent,total=item
                    status.set(f"实际 HID 输出 {sent}/{total} · dx={frame['dx']} dy={frame['dy']} · F8 停止")
                    if cap is not None and cap.isOpened():
                        ok,image=cap.read()
                        if ok:
                            image=Image.fromarray(cv2.cvtColor(image,cv2.COLOR_BGR2RGB));image.thumbnail((700,680));photo=ImageTk.PhotoImage(image);canvas.configure(image=photo)
                else:
                    r=item[1];play.configure(state='normal')
                    status.set(f"{r['status']} · 已输出 {r['frames_sent']} 帧 · Raw Input 累计匹配={r['raw_input_matches']}"+(f" · {r['error']}" if r['error'] else ''))
        except queue.Empty:pass
        root.after(20,poll)
    def close():
        stop.set()
        if worker is not None and worker.is_alive():root.after(100,close)
        else:
            if cap is not None:cap.release()
            root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);root.after(20,poll);root.mainloop()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--data');p.add_argument('--no-ui',action='store_true');p.add_argument('--countdown',type=int,default=3)
    args=p.parse_args()
    if not 0<=args.countdown<=10:p.error('countdown must be 0..10')
    if args.no_ui:
        result=replay(args.data or latest_data(),countdown=args.countdown)
        print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(1 if result and result['error'] else 0)
    else:gui(args.data)
