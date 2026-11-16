"""Offline video -> detections -> relative deltas -> HID-byte preview.

This module never constructs DesktopPointer or connects to the HID broker.
Video remains an open-loop recording; calculated deltas do not change later frames.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import struct
import threading
import time

ROOT = Path(__file__).resolve().parent


def analyze(video, output=None, confidence=.3, gain=.25, sample_fps=10,
            stop=None, callback=None, detector=None):
    import cv2
    import numpy as np
    from preview import Detector, annotate, MODEL_HASH
    from cursor_control import select_target, relative_step
    if not .05 <= gain <= 2 or not .1 <= confidence <= .95 or not 1 <= sample_fps <= 120:
        raise ValueError('Invalid confidence/gain/sample FPS')
    video = Path(video).resolve()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise OSError('无法打开视频，请检查路径或视频编码。')
    fps = cap.get(cv2.CAP_PROP_FPS)
    fps_fallback = not math.isfinite(fps) or fps <= 0
    if fps_fallback:
        fps = 30.
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width, height = int(cap.get(3)), int(cap.get(4))
    stride = max(1, round(fps/sample_fps))
    out_fps = fps/stride
    output = Path(output) if output else ROOT/'offline-results'/datetime.now().strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    area = dict(left=(width-min(640,width))//2, top=(height-min(640,height))//2,
                width=min(640,width), height=min(640,height))
    writer = None
    seen = sampled = targets = moving = 0
    sum_x = sum_y = 0
    infer_times = []
    thumbs = {}
    stopped = False
    started = time.perf_counter()
    summary = dict(source=str(video),source_size=[width,height],source_fps=fps,
                   source_frame_count=total,stride=stride,analysis_fps=out_fps,
                   crop=area,confidence=confidence,gain=gain,per_axis_limit=30,
                   deadzone_pixels=3,model_sha256=MODEL_HASH,fps_fallback=fps_fallback,
                   mode='offline-open-loop',hid_connected=False,input_sent=False,
                   audio_included=False,timestamp_method='source_frame_index / nominal_fps')
    try:
        detector = detector if detector is not None else Detector()
        # Even encoded dimensions for broadly compatible MPEG-4 playback.
        encoded_size=(area['width']//2*2,area['height']//2*2)
        writer=cv2.VideoWriter(str(output/'annotated.mp4'),cv2.VideoWriter_fourcc(*'mp4v'),out_fps,encoded_size)
        if not writer.isOpened():
            raise OSError('无法创建标注视频。')
        with (output/'frames.jsonl').open('w',encoding='utf-8') as details, \
             (output/'mouse-data.jsonl').open('w',encoding='utf-8') as moves, \
             (output/'trajectory.csv').open('w',encoding='utf-8-sig',newline='') as table:
            columns=['frame','time_s','detections','target_x','target_y','target_confidence','dx','dy','dt_ms','hid_hex','inference_ms']
            csvout=csv.DictWriter(table,fieldnames=columns)
            csvout.writeheader()
            previous_ms=0
            while True:
                if stop is not None and stop.is_set():
                    stopped=True
                    break
                if not cap.grab():
                    break
                frame_index=seen
                seen+=1
                if frame_index%stride:
                    continue
                ok,image=cap.retrieve()
                if not ok:
                    raise OSError(f'无法解码视频第 {frame_index} 帧。')
                a=area
                crop=image[a['top']:a['top']+a['height'],a['left']:a['left']+a['width']]
                rows,ms=detector.infer(crop,confidence)
                local_area=dict(left=0,top=0,width=a['width'],height=a['height'])
                target=select_target(rows,local_area,confidence)
                dx,dy=relative_step(target,local_area,gain) if target else (0,0)
                current_ms=round(frame_index/fps*1000)
                dt=current_ms-previous_ms if sampled else 0
                previous_ms=current_ms
                hid=struct.pack('<Bhhbb',0,dx,dy,0,0).hex(' ').upper()
                row=dict(frame=frame_index,time_s=round(frame_index/fps,6),detections=len(rows),
                         target_x=target['local'][0] if target else '',
                         target_y=target['local'][1] if target else '',
                         target_confidence=round(target['row']['confidence'],6) if target else '',
                         dx=dx,dy=dy,dt_ms=dt,hid_hex=hid,inference_ms=round(ms,3))
                csvout.writerow(row)
                details.write(json.dumps(dict(row,boxes=rows),ensure_ascii=False)+'\n')
                moves.write(json.dumps(dict(dx=dx,dy=dy,dt_ms=dt))+'\n')
                sampled+=1
                targets+=target is not None
                moving+=(dx,dy)!=(0,0)
                sum_x+=dx;sum_y+=dy;infer_times.append(ms)
                rendered=annotate(crop,rows,target)
                center=(a['width']//2,a['height']//2)
                cv2.drawMarker(rendered,center,(255,200,0),cv2.MARKER_CROSS,20,1)
                if dx or dy:
                    cv2.arrowedLine(rendered,center,(center[0]+dx*3,center[1]+dy*3),(255,0,255),2,tipLength=.2)
                cv2.rectangle(rendered,(0,0),(a['width'],62),(16,20,25),-1)
                cv2.putText(rendered,f"OFFLINE {frame_index/fps:.2f}s | detections {len(rows)} | delta {dx:+d},{dy:+d}",(10,23),cv2.FONT_HERSHEY_SIMPLEX,.54,(255,255,255),1,cv2.LINE_AA)
                cv2.putText(rendered,'HID preview: '+hid,(10,47),cv2.FONT_HERSHEY_SIMPLEX,.46,(210,220,230),1,cv2.LINE_AA)
                writer.write(rendered[:encoded_size[1],:encoded_size[0]])
                bucket=min(7,int(frame_index/max(total,1)*8))
                score=target['row']['confidence'] if target else -1
                if bucket not in thumbs or score>thumbs[bucket][0]:
                    thumbs[bucket]=(score,cv2.resize(rendered,(320,320)))
                if sampled%20==0 or sampled==1:
                    progress=dict(processed=sampled,source_frame=frame_index,total_frames=total,
                                  source_time_s=round(frame_index/fps,2),target_frames=targets)
                    (output/'progress.json').write_text(json.dumps(progress),encoding='utf-8')
                    if callback:
                        callback('frame',rendered,progress)
                    else:
                        print(json.dumps(progress),flush=True)
        if not sampled:
            raise ValueError('视频没有可分析的帧。')
        if thumbs:
            tiles=[thumbs[k][1] for k in sorted(thumbs)]
            while len(tiles)<8:
                tiles.append(np.zeros((320,320,3),np.uint8))
            sheet=np.vstack([np.hstack(tiles[:4]),np.hstack(tiles[4:8])])
            cv2.imencode('.jpg',sheet,[cv2.IMWRITE_JPEG_QUALITY,90])[1].tofile(str(output/'review.jpg'))
        summary.update(status='stopped' if stopped else 'complete',frames_read=seen,analyzed_frames=sampled,
                       frames_with_target=targets,frames_with_movement=moving,
                       hypothetical_sum_dx=sum_x,hypothetical_sum_dy=sum_y,
                       mean_inference_ms=round(sum(infer_times)/len(infer_times),2),
                       processing_seconds=round(time.perf_counter()-started,2),output=str(output.resolve()))
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        return summary
    except Exception as exc:
        summary.update(status='error',error=f'{type(exc).__name__}: {exc}',analyzed_frames=sampled)
        (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        raise
    finally:
        cap.release()
        if writer is not None:
            writer.release()


def gui(initial=''):
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    from PIL import Image,ImageTk
    import cv2
    root=tk.Tk();root.title('FK 离线视频验证 · 仅分析和报告预览');root.geometry('900x850')
    file=tk.StringVar(value=initial);status=tk.StringVar(value='选择视频后点击开始。离线分析不发送鼠标输入。')
    conf=tk.DoubleVar(value=.3);gain=tk.DoubleVar(value=.25);rate=tk.IntVar(value=10)
    messages=queue.Queue();stop=threading.Event();worker=None;latest=None;photo=None
    frame=ttk.Frame(root,padding=15);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='离线视频：识别 → 相对位移 → HID 字节预览',font=('Microsoft YaHei UI',16)).pack(anchor='w')
    bar=ttk.Frame(frame);bar.pack(fill='x',pady=12)
    ttk.Entry(bar,textvariable=file).pack(side='left',fill='x',expand=True)
    def choose():
        path=filedialog.askopenfilename(filetypes=[('视频','*.mp4 *.mkv *.avi *.mov'),('所有文件','*.*')])
        if path:file.set(path)
    ttk.Button(bar,text='选择视频',command=choose).pack(side='left',padx=5)
    options=ttk.Frame(frame);options.pack(fill='x',pady=8)
    for name,var in [('置信度',conf),('移动幅度',gain),('分析 FPS',rate)]:
        ttk.Label(options,text=name).pack(side='left',padx=5)
        ttk.Entry(options,textvariable=var,width=8).pack(side='left')
    buttons=ttk.Frame(frame);buttons.pack(fill='x',pady=8)
    def start():
        nonlocal worker
        if worker is not None and worker.is_alive():return
        try: values=(conf.get(),gain.get(),rate.get())
        except Exception:messagebox.showerror('参数错误','请填写有效数字');return
        if not Path(file.get()).is_file():messagebox.showerror('视频未找到','请选择存在的视频');return
        stop.clear();start_button.configure(state='disabled');status.set('加载模型并分析，请稍候…')
        path=file.get()
        def run():
            try:messages.put(('done',analyze(path,confidence=values[0],gain=values[1],sample_fps=values[2],stop=stop,callback=lambda *a:messages.put(a))))
            except Exception as exc:messages.put(('error',str(exc)))
        worker=threading.Thread(target=run,daemon=True);worker.start()
    start_button=ttk.Button(buttons,text='开始离线验证',command=start);start_button.pack(side='left')
    ttk.Button(buttons,text='停止分析',command=stop.set).pack(side='left',padx=8)
    ttk.Button(buttons,text='打开结果目录',command=lambda:os.startfile(str(latest or ROOT/'offline-results'))).pack(side='left')
    def replay_result():
        import subprocess
        if worker is not None and worker.is_alive():
            messagebox.showinfo('请稍候','请先完成或停止离线分析。');return
        args=[os.sys.executable,str(ROOT/'replay_video_mouse.py')]
        if latest:args+=['--data',str(latest/'mouse-data.jsonl')]
        subprocess.Popen(args,creationflags=subprocess.CREATE_NO_WINDOW)
    ttk.Button(buttons,text='驱动鼠标回放',command=replay_result).pack(side='left',padx=8)
    ttk.Label(frame,textvariable=status,wraplength=850).pack(anchor='w',pady=8)
    canvas=tk.Label(frame,bg='#10151c');canvas.pack(fill='both',expand=True)
    def poll():
        nonlocal photo,latest
        try:
            while True:
                item=messages.get_nowait()
                if item[0]=='frame':
                    image=Image.fromarray(cv2.cvtColor(item[1],cv2.COLOR_BGR2RGB));image.thumbnail((700,650));photo=ImageTk.PhotoImage(image);canvas.configure(image=photo)
                    p=item[2];status.set(f"已分析 {p['processed']} 帧 · 视频 {p['source_time_s']:.1f} 秒 · 有目标 {p['target_frames']} 帧")
                elif item[0]=='done':
                    latest=Path(item[1]['output']);status.set(f"分析结束：{item[1]['analyzed_frames']} 帧，有目标 {item[1]['frames_with_target']} 帧。结果：{latest}");start_button.configure(state='normal')
                else:status.set('分析失败：'+item[1]);start_button.configure(state='normal')
        except queue.Empty:pass
        root.after(100,poll)
    def close():
        if worker is not None and worker.is_alive():
            stop.set();status.set('正在完成当前帧并保存，请稍候…');root.after(150,close)
        else:root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);root.after(100,poll);root.mainloop()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--video',default='');parser.add_argument('--output')
    parser.add_argument('--no-ui',action='store_true')
    parser.add_argument('--confidence',type=float,default=.3);parser.add_argument('--gain',type=float,default=.25)
    parser.add_argument('--sample-fps',type=float,default=10)
    args=parser.parse_args()
    if args.no_ui:
        if not args.video:parser.error('--video is required with --no-ui')
        print(json.dumps(analyze(args.video,args.output,args.confidence,args.gain,args.sample_fps),ensure_ascii=False,indent=2))
    else:gui(args.video)
