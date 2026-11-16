"""Screen/image detection with optional F8-toggle desktop cursor positioning."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from cursor_control import DesktopPointer, enable_physical_coordinates
from paced_control import PacedFollower as CursorFollower
from game_calibration import load_calibration
from screen_source import ScreenSource
from latest_capture import LatestScreenSource,LatestRenderer
from raw_mouse import RawMouseMonitor
from enemy_filter import filter_targets
from outline_settings import normalize_enemy_color
from friend_guard_settings import load_friend_guard
from recognition_diagnostics import format_recognition_report
from latency_stats import LatencyStats
from strength_profiles import (CUSTOM_STRENGTH,STRENGTH_CHOICES,get_strength_profile,
                               profile_from_label,identify_strength,describe_strength,strength_guide)

ROOT = Path(__file__).resolve().parent
os.environ['YOLOv5_AUTOINSTALL'] = 'false'
os.environ['YOLO_AUTOINSTALL'] = 'false'
os.environ['YOLO_CONFIG_DIR'] = str(ROOT / '.settings')
os.environ['MPLCONFIGDIR'] = str(ROOT / '.settings' / 'matplotlib')
sys.path.insert(0, str(ROOT / 'vendor' / 'yolov5'))
MODEL_HASH = '66ff4acd68aed070f48dc0841d53e1c8d28a88263c2184cb7413a1a88aa41b54'
LIVE_MAX_FPS = 240
DEFAULT_AIM_HEIGHT = .12
CONTROL_ALGORITHMS={'原控制（兼容）':'legacy','PD 阻尼（实验）':'pd','预测强锁（实验）':'lock'}
FOLLOW_ARM_SECONDS=5.
CAPTURE_SCOPES = {'fullscreen': '所选显示器全屏', 'center': '中央 640×640'}
INFERENCE_SIZES = (640, 960, 1280)
MOTION_PRESETS = {'稳定': {'gain': .4, 'step_limit': 60},
                  '快速': {'gain': .8, 'step_limit': 120},
                  '低灵敏度': {'gain': 2., 'step_limit': 240}}
GAME_CALIBRATION = load_calibration(ROOT / '.settings' / 'game-calibration.json')
if GAME_CALIBRATION:
    MOTION_PRESETS[GAME_CALIBRATION['preset_name']] = {
        'gain': GAME_CALIBRATION['gain'], 'step_limit': GAME_CALIBRATION['step_limit']}


def wait_for_frame(stop_event, deadline, clock=time.perf_counter, sleeper=time.sleep):
    # Windows Event.wait rounds short waits to coarse timer ticks. Python 3.12
    # sleep uses a high-resolution timer. Check stop at least every 5 ms.
    while not stop_event.is_set():
        remaining = deadline - clock()
        if remaining <= 0:
            return
        sleeper(min(remaining, .005))


class TorchDetector:
    def __init__(self, device='auto', half=False):
        import torch
        from ultralytics.utils import SETTINGS
        SETTINGS.update({'sync': False, 'hub': False})
        from models import common, yolo
        from models.common import AutoShape
        self.torch = torch
        if device not in ('auto', 'cpu', 'cuda'):
            raise ValueError('Unknown inference device')
        self.device = torch.device('cuda:0' if device != 'cpu' and torch.cuda.is_available() else 'cpu')
        if device == 'cuda' and self.device.type != 'cuda':
            raise RuntimeError('CUDA 不可用，请检查 GPU 环境与驱动。')
        self.fallback_reason = None
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        weights = ROOT / 'weights' / 'best.pt'
        if hashlib.sha256(weights.read_bytes()).hexdigest() != MODEL_HASH:
            raise ValueError('模型文件校验失败，请恢复随附的 best.pt。')
        # Explicitly permit only known layer classes from the pinned checkpoint.
        allowed = [set, Path, type(Path('.')), yolo.DetectionModel, yolo.Detect,
                   common.Conv, common.C3, common.Bottleneck, common.SPPF, common.Concat,
                   torch.nn.Sequential, torch.nn.ModuleList, torch.nn.Conv2d,
                   torch.nn.BatchNorm2d, torch.nn.SiLU, torch.nn.MaxPool2d, torch.nn.Upsample]
        with torch.serialization.safe_globals(allowed):
            ckpt = torch.load(weights, map_location='cpu', weights_only=True)
        core = (ckpt.get('ema') or ckpt['model']).float().eval().fuse()
        self.names = core.names
        self.model = AutoShape(core, verbose=False)
        self.model.max_det = 100
        try:
            self.model.to(self.device)
            if self.device.type == 'cuda':
                torch.backends.cudnn.benchmark = True
                if half:
                    self.model.half()
                # Compile/initialize GPU kernels before accepting live frames.
                import numpy as np
                with torch.inference_mode():
                    for _ in range(3):
                        self.model(np.zeros((640, 640, 3), dtype=np.uint8), size=640)
                torch.cuda.synchronize(self.device)
        except RuntimeError as exc:
            if device != 'auto' or self.device.type != 'cuda':
                raise
            self.fallback_reason = str(exc)
            self.device = torch.device('cpu')
            # Reload from CPU storage even when a CUDA context cannot recover.
            with torch.serialization.safe_globals(allowed):
                ckpt = torch.load(weights, map_location='cpu', weights_only=True)
            core = (ckpt.get('ema') or ckpt['model']).float().eval().fuse()
            self.model = AutoShape(core, verbose=False)
            self.model.max_det = 100
        self.info = {'device': str(self.device), 'torch': torch.__version__,
                     'precision': str(next(self.model.parameters()).dtype).replace('torch.', ''),
                     'gpu_name': torch.cuda.get_device_name(self.device) if self.device.type == 'cuda' else None,
                     'fallback_reason': self.fallback_reason}

    def infer(self, bgr, confidence=0.3, input_size=640):
        if type(input_size) is not int or input_size not in INFERENCE_SIZES:
            raise ValueError('Unsupported inference size')
        self.model.conf = float(confidence)
        if self.device.type == 'cuda':
            self.torch.cuda.synchronize(self.device)
        start = time.perf_counter()
        # Keep upstream wwqy.py's BGR input convention for faithful reproduction.
        with self.torch.inference_mode():
            result = self.model(bgr, size=input_size)
        rows = []
        for x1, y1, x2, y2, conf, cls in result.xyxy[0].cpu().tolist():
            index = int(cls)
            rows.append({'box': [x1, y1, x2, y2], 'confidence': conf,
                         'class_id': index, 'label': self.names[index]})
        if self.device.type == 'cuda':
            self.torch.cuda.synchronize(self.device)
        ms = (time.perf_counter() - start) * 1000
        self.timings = dict(preprocess_ms=round(result.t[0],3), forward_ms=round(result.t[1],3),
                            postprocess_ms=round(result.t[2],3),total_ms=round(ms,3),
                            breakdown_available=True)
        return rows, ms


class Detector:
    """Use approved TensorRT shapes, retaining the original PyTorch fallback."""
    def __init__(self, device='auto', half=False, backend=None):
        self.requested_device, self.half = device, half
        self.backend = backend or os.environ.get('FK_INFERENCE_BACKEND', 'auto')
        if self.backend not in ('auto', 'torch', 'tensorrt'):
            raise ValueError('Unknown inference backend')
        self.reference = None
        self.fast = None
        self.fallback_reason = None
        self.timings = {}
        if self.backend != 'torch' and device != 'cpu':
            try:
                from fast_inference import TensorRTDetector
                self.fast = TensorRTDetector(ROOT, MODEL_HASH)
                self.device, self.names, self.info = self.fast.device, self.fast.names, self.fast.info
            except Exception as exc:
                if self.backend == 'tensorrt':
                    raise
                self.fallback_reason = f'{type(exc).__name__}: {exc}'
        if self.fast is None:
            self._reference()
        else:
            # Warm the compatibility model during startup, so a borderline
            # score never causes a model load in the live control path.
            reference=self._reference()
            self.fast.reference=reference
            self.info=self.fast.info

    def _reference(self):
        if self.reference is None:
            self.reference = TorchDetector(device=self.requested_device, half=self.half)
        self.device, self.names = self.reference.device, self.reference.names
        self.info = dict(self.reference.info, backend='pytorch', fallback_reason=self.fallback_reason)
        return self.reference

    def infer(self, bgr, confidence=.3, input_size=640):
        if type(input_size) is not int or input_size not in INFERENCE_SIZES:
            raise ValueError('Unsupported inference size')
        if self.fast is not None:
            from fast_inference import UnsupportedShape
            try:
                result = self.fast.infer(bgr, confidence, input_size)
                self.info, self.timings = self.fast.info, dict(self.fast.timings)
                return result
            except UnsupportedShape as exc:
                if self.backend == 'tensorrt':
                    raise
                self.fallback_reason = str(exc)
        reference = self._reference()
        rows, ms = reference.infer(bgr, confidence, input_size)
        self.timings = dict(reference.timings)
        return rows, ms

    def warm(self,bgr,confidence=.3,input_size=640):
        # Warm both paths for the actual aspect ratio before live timestamps.
        if self.reference is not None:
            self.reference.infer(bgr,confidence,input_size)
        return self.infer(bgr,confidence,input_size)


def annotate(bgr, rows, target=None, feedback=None):
    import cv2
    out = bgr.copy()
    for row in rows:
        x1, y1, x2, y2 = (int(v) for v in row['box'])
        color = (80, 210, 255) if row['class_id'] == 0 else (140, 230, 110)
        if row.get('target_eligible') is False:
            color=(110,110,110)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = f"{row['label']} {row['confidence']:.2f}"
        if row.get('target_eligible') is False:
            label+=' SUPPORT' if row.get('filter_reason')=='body_support_only' else ' BLOCKED'
        cv2.putText(out, label, (x1, max(18, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, .55, color, 2)
    if target:
        cv2.drawMarker(out, target['local'], (255, 255, 255), cv2.MARKER_CROSS, 18, 2)
        if feedback is not None:
            aim_text = ('HEAD detected' if target.get('aim_source') == 'detected_head' else
                        f"HEAD estimate top {feedback['aim_height_ratio']:.0%}")
            cv2.putText(out, aim_text,
                        (target['local'][0] + 8, max(16, target['local'][1] - 10)),
                        cv2.FONT_HERSHEY_SIMPLEX, .45, (255, 255, 255), 1, cv2.LINE_AA)
    if feedback is not None and feedback.get('mode') == 'relative':
        center=(out.shape[1]//2,out.shape[0]//2)
        cv2.drawMarker(out,center,(255,200,0),cv2.MARKER_CROSS,22,2)
        if target:
            cv2.line(out,center,target['local'],(255,0,255),1)
        error=feedback.get('target_error_pixels')
        delta=feedback.get('last_output')
        text=f"LIVE crosshair error={error} HID delta={delta}"
        cv2.putText(out,text,(8,22),cv2.FONT_HERSHEY_SIMPLEX,.45,(255,255,255),1,cv2.LINE_AA)
    return out


def capture_area(sct, monitor_index, scope='center'):
    if not 1 <= monitor_index < len(sct.monitors):
        raise ValueError('所选显示器已断开，请重新选择。')
    if scope not in CAPTURE_SCOPES:
        raise ValueError('Unknown capture scope')
    mon = sct.monitors[monitor_index]
    if scope == 'fullscreen':
        return {key: mon[key] for key in ('left', 'top', 'width', 'height')}
    width, height = min(640, mon['width']), min(640, mon['height'])
    return {'left': mon['left'] + (mon['width'] - width) // 2,
            'top': mon['top'] + (mon['height'] - height) // 2,
            'width': width, 'height': height}


def capture(sct, monitor_index, with_area=False, scope='center'):
    import cv2
    import numpy as np
    area = capture_area(sct, monitor_index, scope)
    image = cv2.cvtColor(np.asarray(sct.grab(area)), cv2.COLOR_BGRA2BGR)
    return (image, area) if with_area else image


def read_image(path):
    import cv2
    import numpy as np
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError('无法读取这张图片。')
    return image


class PreviewApp:
    def __init__(self, root, smoke=False, pointer=None, input_backend='windows', motion_settings_path=None,
                 inference_device='auto', capture_settings_path=None):
        import tkinter as tk
        from tkinter import ttk
        from mss import mss
        self.root = root
        self.session_id = uuid.uuid4().hex
        self.smoke = smoke
        self.queue = queue.Queue(maxsize=2)
        self.latency = LatencyStats()
        self.live_fps_limit = 60
        self.stop_event = threading.Event()
        self.pointer = pointer if pointer is not None else DesktopPointer()
        self.native_pointer = pointer is None
        if self.native_pointer:
            self.pointer.set_backend(input_backend)
        self.is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        self.follower = CursorFollower(self.pointer, self.stop_event)
        self.follower.pause_on_focus_loss=True
        if self.native_pointer:
            from foreground_guard import load_foreground_guard
            self.foreground_guard = load_foreground_guard()
            self.follower.set_foreground_check(self.foreground_guard.allowed)
        self.manual_monitor=RawMouseMonitor() if self.native_pointer and input_backend=='virtualhid' else None
        if self.manual_monitor is not None:self.manual_monitor.start()
        self.follower.use_low_latency(self.manual_monitor)
        self.motion_settings_path = Path(motion_settings_path) if motion_settings_path is not None else ROOT / '.settings' / 'motion-profile.json'
        self._motion_save_job = None
        self._follow_arm_deadline=None
        self._follow_arm_f8_presses=0
        self.friend_guard,self._friend_guard_load_error=load_friend_guard(self.motion_settings_path.parent/'friend-guard.json')
        self.enemy_filter = True
        self.enemy_color = 'red'
        self._enemy_color_load_error = ''
        self.target_filter_report = dict(strict=True,enemy_color='red',friend_guard=self.friend_guard,friend_guard_active=self.friend_guard,raw=0,eligible=0,rejected_reasons={},
                                         geometry_visibility_verified=False)
        profile = {}
        try:
            profile = json.loads(self.motion_settings_path.read_text(encoding='utf-8'))
            if not isinstance(profile, dict):
                raise ValueError('Invalid motion profile document')
            if not isinstance(profile.get('enemy_filter',True),bool):
                raise ValueError('Invalid enemy filter setting')
            self.follower.configure_motion(gain=profile['gain'], step_limit=profile['step_limit'], near_slowdown=True,
                                          aim_height_ratio=profile.get('aim_height_ratio', DEFAULT_AIM_HEIGHT),
                                          response_ms=profile.get('response_ms',80),
                                          chase_strength=profile.get('chase_strength',1.),
                                          predictive=profile.get('predictive',True),
                                          soft_assist=profile.get('soft_assist',True),
                                          tolerance_px=profile.get('tolerance_px',8.),
                                          pause_on_fire=profile.get('pause_on_fire',True),
                                          fire_release_ms=profile.get('fire_release_ms',180.),
                                          control_algorithm=profile.get('control_algorithm','legacy'),
                                          pd_kp=profile.get('pd_kp',1.),pd_kd=profile.get('pd_kd',.25),
                                          manual_fire_priority=profile.get('manual_fire_priority',self.manual_monitor is not None),
                                          fire_target_lock=profile.get('fire_target_lock',False))
            self.enemy_filter=profile.get('enemy_filter',True)
        except (OSError, ValueError, TypeError, KeyError):
            self.follower.configure_motion(gain=GAME_CALIBRATION['gain'] if GAME_CALIBRATION else .8,
                                          near_slowdown=True,
                                          aim_height_ratio=DEFAULT_AIM_HEIGHT,predictive=True,
                                          pause_on_fire=True,fire_release_ms=180.,control_algorithm='legacy',
                                          pd_kp=1.,pd_kd=.25,manual_fire_priority=self.manual_monitor is not None,fire_target_lock=False,
                                          **get_strength_profile(2).motion())
        # Parse color separately: an invalid new field must not reset saved strength.
        self._load_outline_profile(profile)
        self.follower.configure_motion(prefer_head=True,stabilization=True)
        if self.native_pointer:
            self.follower.configure_motion(mode='relative')
        self._connection_check = None
        self._input_diagnostic_worker = None
        self._f9_was_down = False
        self.live_mode = False
        self.worker = None
        self.detector = None
        self.inference_device = inference_device
        self.capture_settings_path = Path(capture_settings_path) if capture_settings_path is not None else ROOT / '.settings' / 'capture-profile.json'
        self.capture_scope = 'fullscreen'
        self.inference_size = 1280
        try:
            capture_profile = json.loads(self.capture_settings_path.read_text(encoding='utf-8'))
            if (capture_profile['scope'] not in CAPTURE_SCOPES or
                    type(capture_profile['input_size']) is not int or capture_profile['input_size'] not in INFERENCE_SIZES):
                raise ValueError('Invalid capture profile')
            self.capture_scope = capture_profile['scope']
            self.inference_size = capture_profile['input_size']
        except (OSError, ValueError, TypeError, KeyError):
            pass
        self.current_capture_area = None
        self.capture_info = dict(backend='pending',fallback_reason=None)
        self.performance = {}
        self.confidence = .3
        self.processed = 0
        self.capture_black = False
        self._next_diagnostic_write = 0
        root.title('FK HID V8 · 简洁控制台 · ' + ('管理员' if self.is_admin else '普通权限'))
        with mss() as sct:
            self.monitors = [f"显示器 {i} · {m['width']} × {m['height']}"
                             for i, m in enumerate(sct.monitors[1:], 1)]
        self.last_image = None
        from ui_layout import build_ui
        build_ui(self,root,dict(capture_scopes=CAPTURE_SCOPES,inference_sizes=INFERENCE_SIZES,
                                motion_presets=MOTION_PRESETS,strength_choices=STRENGTH_CHOICES,
                                algorithms=CONTROL_ALGORITHMS,calibration=GAME_CALIBRATION))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(80, self.poll)
        root.after(20, self.poll_hotkeys)
        if smoke:
            root.after(400, self.start_sample)
            root.after(90000, self.smoke_timeout)

    def set_confidence(self, value):
        self.confidence = float(value)
        self.threshold_label.configure(text=f'置信度 {self.confidence:.2f}')

    def change_capture_settings(self,event=None):
        if self.worker is not None and self.worker.is_alive():
            self.capture_choice.set(CAPTURE_SCOPES[self.capture_scope])
            self.inference_choice.set(str(self.inference_size))
            return
        self.disarm()
        self.capture_scope=next(key for key,value in CAPTURE_SCOPES.items() if value==self.capture_choice.get())
        self.inference_size=int(self.inference_choice.get())
        self.current_capture_area=None
        self.save_capture_profile()

    def save_capture_profile(self):
        try:
            self.capture_settings_path.parent.mkdir(parents=True,exist_ok=True)
            self.capture_settings_path.write_text(json.dumps(dict(scope=self.capture_scope,input_size=self.inference_size),indent=2),encoding='utf-8')
            self.capture_settings_status.set('范围与识别尺寸已保存 · 全屏建议 1280；640 更快')
        except OSError:
            self.capture_settings_status.set('范围已生效；参数保存失败')

    def toggle_follow(self):
        if not self.follow_var.get():
            self.disarm()
            self.control_status.set(self.follower.last_status)
            return
        running=self.worker is not None and self.worker.is_alive()
        self.disarm()
        if not self.live_mode or not running or self.stop_event.is_set():
            self.follower.last_status='未开启：请先开始实时识别，再点击跟随开关或按 F8'
            self.control_status.set(self.follower.last_status)
            return
        # Clicking this window necessarily removes game focus. Arm once, only
        # after returning to an allowed foreground; never weaken the guard.
        self._follow_arm_deadline=time.monotonic()+FOLLOW_ARM_SECONDS
        self._follow_arm_f8_presses=self.follower.snapshot()['f8_presses']
        self._sync_follow_state(running)

    def _sync_follow_state(self,running,now=None):
        now=time.monotonic() if now is None else now
        deadline=getattr(self,'_follow_arm_deadline',None)
        if deadline is not None:
            state=self.follower.snapshot()
            if not self.live_mode or not running or self.stop_event.is_set():
                self.disarm()
                self.follower.last_status='待开启已取消：实时识别已停止'
            elif state['f8_presses']!=self._follow_arm_f8_presses or state['enabled']:
                # F8 owns its result; a pending UI request must not re-enable it.
                self._follow_arm_deadline=None
            elif now>=deadline:
                self.disarm()
                self.follower.last_status='待开启已超时：请重新点击，5 秒内切回游戏；也可在游戏内按 F8'
            elif self.follower._foreground_allowed():
                self._follow_arm_deadline=None
                self.follower.set_enabled(True)  # Rechecks focus, monitor and in-flight input.
            else:
                self.follow_var.set(True)
                self.control_status.set(f'待开启：{max(1,int(deadline-now+.999))} 秒内切回游戏 · 再点取消 · 尚未移动')
                return
        self.follow_var.set(self.follower.enabled)
        self.control_status.set(self.follower.last_status)

    def change_control_algorithm(self,event=None):
        algorithm=CONTROL_ALGORITHMS.get(self.algorithm_choice.get())
        if algorithm is None:return
        self.disarm()
        self.follower.configure_motion(control_algorithm=algorithm)
        self.schedule_motion_save()

    def change_pd_gain(self,name,value):
        if name not in ('pd_kp','pd_kd'):raise ValueError('Unknown PD setting')
        self.disarm()
        number=round(float(value),2)
        self.follower.configure_motion(**{name:number})
        label=getattr(self,name+'_label',None)
        if label is not None:label.configure(text=('P 倍率 ' if name=='pd_kp' else 'D 阻尼 ')+f'{number:.2f}')
        self.schedule_motion_save()

    def change_motion_mode(self, event=None):
        self.disarm()
        self.follower.configure_motion(mode='relative' if self.motion_mode.current() == 1 else 'desktop')
        self._refresh_strength_profile()

    def change_motion_gain(self, value):
        gain = round(float(value), 2)
        self.follower.configure_motion(gain=gain)
        self.gain_label.configure(text=f'位移增益 {gain:.2f}')
        self.motion_preset.set('自定义')
        self.schedule_motion_save()

    def change_motion_limit(self, value):
        limit = round(float(value))
        self.follower.configure_motion(step_limit=limit)
        self.limit_label.configure(text=self._limit_text(limit))
        self.motion_preset.set('自定义')
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def change_aim_height(self, value):
        ratio = round(float(value)) / 100
        self.follower.configure_motion(aim_height_ratio=ratio)
        self.aim_label.configure(text=f'头部未检出时：距框顶 {ratio:.0%}')
        self.schedule_motion_save()

    def change_response(self,value):
        response=round(float(value))
        self.follower.configure_motion(response_ms=response)
        self.response_label.configure(text=f'稳定响应 {response} ms')
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def change_chase_strength(self,value):
        strength=round(float(value),1)
        self.follower.configure_motion(chase_strength=strength)
        self.chase_label.configure(text=f'追赶强度 {strength:.1f}×')
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def change_prediction(self):
        self.disarm()
        self.follower.configure_motion(predictive=self.predictive_var.get())
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def change_enemy_filter(self):
        self.disarm()
        self.enemy_filter=self.enemy_filter_var.get()
        self.aim_slider.configure(state='disabled' if self.enemy_filter else 'normal')
        self.schedule_motion_save()

    def apply_strong_follow(self):
        self.disarm()
        self.follower.configure_motion(soft_assist=False)
        self.soft_assist_var.set(False)
        self.response_slider.set(6)
        self.chase_slider.set(6)
        self.limit_slider.set(480)
        self.change_response('6')
        self.change_chase_strength('6')
        self.change_motion_limit('480')

    def _limit_text(self, limit):
        kind='向量' if self.follower.soft_assist else '每轴'
        return f'每帧{kind}上限 {limit} 计数'

    def _refresh_strength_profile(self):
        # Scale callbacks can run before the selector exists during UI setup.
        if not hasattr(self,'strength_choice') or not hasattr(self,'strength_description'):return
        state=self.follower.snapshot()
        level=identify_strength(state)
        self.strength_choice.set(get_strength_profile(level).label if level is not None else CUSTOM_STRENGTH)
        self.strength_description.set(describe_strength(state))

    def change_strength_level(self,event=None):
        try:profile=profile_from_label(self.strength_choice.get())
        except ValueError:
            self._refresh_strength_profile()
            return
        if profile is None:
            # Selecting "custom" does not silently replace the actual settings.
            self._refresh_strength_profile()
            return
        self.apply_strength_level(profile.level)

    def apply_strength_level(self,level):
        profile=get_strength_profile(level)
        self.disarm()
        self.follower.configure_motion(**profile.motion())
        self.soft_assist_var.set(True)
        # Match all actual fields first; subsequent Scale callbacks cannot label
        # an intermediate mixture of settings as a different strength level.
        for slider,value,callback in ((self.response_slider,profile.response_ms,self.change_response),
                                      (self.chase_slider,profile.chase_strength,self.change_chase_strength),
                                      (self.limit_slider,profile.step_limit,self.change_motion_limit),
                                      (self.tolerance_slider,profile.tolerance_px,self.change_tolerance)):
            slider.set(value)
            callback(str(value))
        self._refresh_strength_profile()

    def show_strength_guide(self):
        from tkinter import messagebox
        messagebox.showinfo('V8 · 五档强度与取舍',strength_guide(),parent=self.root)

    def change_assistance_options(self):
        self.disarm()
        self.follower.configure_motion(soft_assist=self.soft_assist_var.get(),
                                       pause_on_fire=self.pause_on_fire_var.get())
        self.limit_label.configure(text=self._limit_text(self.follower.step_limit))
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def change_manual_fire_priority(self):
        self.disarm()
        self.follower.configure_motion(manual_fire_priority=self.manual_fire_priority_var.get())
        self.schedule_motion_save()

    def change_tolerance(self,value):
        tolerance=round(float(value),1)
        self.follower.configure_motion(tolerance_px=tolerance)
        self.tolerance_label.configure(text=f'容差 {tolerance:g} px')
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def change_fire_release(self,value):
        delay=round(float(value))
        self.follower.configure_motion(fire_release_ms=delay)
        self.fire_release_label.configure(text=f'松键后等待 {delay} ms')
        self.schedule_motion_save()

    def apply_soft_follow(self):
        # Compatibility with the former button: its firing defaults are kept.
        # The new level selector itself does not modify firing settings.
        self.apply_strength_level(2)
        self.follower.configure_motion(pause_on_fire=True,fire_release_ms=180.)
        self.pause_on_fire_var.set(True)
        self.fire_release_slider.set(180)
        self.change_fire_release('180')

    def change_motion_preset(self, event=None):
        name = self.motion_preset.get()
        if name not in MOTION_PRESETS:
            return
        values = MOTION_PRESETS[name]
        self.follower.configure_motion(**values, near_slowdown=True)
        self.gain_slider.set(values['gain'])
        self.limit_slider.set(values['step_limit'])
        self.gain_label.configure(text=f"位移增益 {values['gain']:.2f}")
        self.limit_label.configure(text=self._limit_text(values['step_limit']))
        self.motion_preset.set(name)
        self._refresh_strength_profile()
        self.schedule_motion_save()

    def schedule_motion_save(self):
        if self._motion_save_job is not None:
            self.root.after_cancel(self._motion_save_job)
        self._motion_save_job = self.root.after(300, self.save_motion_profile)

    def _load_outline_profile(self, profile):
        value = profile.get('enemy_color', 'red') if isinstance(profile, dict) else 'red'
        try:
            self.enemy_color = normalize_enemy_color(value)
            self._enemy_color_load_error = ''
        except ValueError:
            self.enemy_color = 'invalid'
            self._enemy_color_load_error = '保存的轮廓颜色无效；请在高级设置重新选择并应用。跟随参数未因此重置。'
        self.target_filter_report = dict(strict=self.enemy_filter, enemy_color=self.enemy_color,friend_guard=getattr(self,'friend_guard',False),friend_guard_active=getattr(self,'friend_guard',False) and self.enemy_filter,
            raw=0, eligible=0, rejected_reasons={}, geometry_visibility_verified=False,
            class_counts={}, eligible_class_counts={})

    def save_motion_profile(self):
        if self._motion_save_job is not None:
            self.root.after_cancel(self._motion_save_job)
            self._motion_save_job = None
        state = self.follower.snapshot()
        state['enemy_filter']=self.enemy_filter
        state['enemy_color']=getattr(self, 'enemy_color', 'red')
        try:
            self.motion_settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary=self.motion_settings_path.with_name(self.motion_settings_path.name+'.'+uuid.uuid4().hex+'.tmp')
            try:
                temporary.write_text(json.dumps(
                    {key: state[key] for key in ('gain','step_limit','aim_height_ratio','response_ms','chase_strength',
                                                'predictive','enemy_filter','enemy_color','soft_assist','tolerance_px',
                                                'pause_on_fire','fire_release_ms','control_algorithm','pd_kp','pd_kd','manual_fire_priority','fire_target_lock')},indent=2),encoding='utf-8')
                os.replace(temporary,self.motion_settings_path)
            finally:
                try:temporary.unlink(missing_ok=True)
                except OSError:pass
            self.motion_save_status.set('接近目标自动减速 · 参数已保存')
        except OSError:
            self.motion_save_status.set('本次调节已生效；参数保存失败')

    def change_backend(self, event=None):
        self.disarm()
        name = ('windows', 'logitech', 'virtualhid')[self.backend_choice.current()]
        self.pointer.set_backend(name)
        if name in ('logitech', 'virtualhid'):
            self.motion_mode.current(1)
            self.follower.configure_motion(mode='relative')
            self.motion_mode.configure(state='disabled')
        else:
            self.motion_mode.configure(state='readonly')
        self.backend_status.set('输入方式已切换，请检查连接')
        self._refresh_strength_profile()

    def check_connection(self):
        if self._connection_check is not None and self._connection_check.is_alive():
            return
        self.connection_button.configure(state='disabled')
        self.backend_status.set('正在检查连接…')
        def check():
            try:
                message = self.pointer.check_backend()
                self.post(('backend_ready', message))
            except OSError as exc:
                self.follower.note_input_error(exc)
                self.post(('backend_ready', f'连接失败：{exc}'))
        self._connection_check = threading.Thread(target=check, daemon=True)
        self._connection_check.start()

    def diagnose_input(self):
        if self._input_diagnostic_worker is not None and self._input_diagnostic_worker.is_alive():return
        self.input_diagnostic_status.set('正在记录 5 秒：保持游戏前台并复现问题…')
        def run():
            try:
                from input_diagnostic import collect
                report=collect(self.follower.snapshot,seconds=5)
                self.post(('input_diagnostic',report['message']))
            except Exception as exc:self.post(('input_diagnostic','诊断失败：'+str(exc)))
        self._input_diagnostic_worker=threading.Thread(target=run,daemon=True)
        self._input_diagnostic_worker.start()

    def disarm(self):
        self._follow_arm_deadline=None
        self.follower.set_enabled(False)
        self.follow_var.set(False)

    def post(self, message):
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(message)

    def begin(self, image_path=None):
        if self.worker is not None and self.worker.is_alive():
            return
        self.live_mode = image_path is None
        if not self.live_mode:
            self.disarm()
        self.stop_event.clear()
        self.monitor.configure(state='disabled')
        self.capture_choice.configure(state='disabled')
        self.inference_choice.configure(state='disabled')
        for button in (self.start_button, self.image_button, self.sample_button):
            button.configure(state='disabled')
        self.status.set('正在加载模型…' if self.detector is None else '正在识别…')
        index = self.monitor.current() + 1
        self.worker = threading.Thread(target=self.run, args=(image_path, index), daemon=True)
        self.worker.start()

    def run(self, image_path, index):
        source=None
        renderer=None
        try:
            from mss import mss
            import numpy as np
            if self.detector is None:
                self.detector = Detector(device=self.inference_device)
            if image_path:
                image = read_image(image_path)
                rows, ms = self.detector.infer(image, self.confidence)
                self.post(('frame', annotate(image, rows), rows, ms, None))
            else:
                if hasattr(self.pointer, 'check_backend'):
                    try:
                        self.post(('backend_ready', self.pointer.check_backend()))
                    except OSError as exc:
                        self.follower.note_input_error(exc)
                        self.post(('backend_ready', f'连接失败：{exc}'))
                with mss() as sct:
                    self.current_capture_area=capture_area(sct,index,self.capture_scope)
                    monitor={key:sct.monitors[index][key] for key in ('left','top','width','height')}
                    source=LatestScreenSource(monitor,self.current_capture_area,
                                        lambda:capture(sct,index,with_area=True,scope=self.capture_scope))
                    self.capture_info=dict(source.info)
                    # Warm the selected aspect ratio and resolution before any
                    # controller timestamps; the first CUDA shape can be slow.
                    warm_image=None
                    warm_deadline=time.perf_counter()+5
                    while warm_image is None and not self.stop_event.is_set():
                        warm_image,_=source.grab()
                        if time.perf_counter()>warm_deadline:raise RuntimeError('等待首个截图超时')
                    if warm_image is None:return
                    self.detector.warm(warm_image,self.confidence,input_size=self.inference_size)
                    def render(payload):
                        import cv2
                        frame,render_rows,render_target,feedback,inference_ms,render_fps=payload
                        self.capture_black=bool(frame.max()<8 and frame.mean()<1)
                        annotated=annotate(frame,render_rows,render_target,feedback=feedback)
                        if annotated.shape[1]>960:
                            scale=960/annotated.shape[1]
                            annotated=cv2.resize(annotated,(960,max(1,round(annotated.shape[0]*scale))),interpolation=cv2.INTER_AREA)
                        self.post(('frame',annotated,render_rows,inference_ms,render_fps))
                    renderer=LatestRenderer(render)
                    next_preview = 0
                    fresh_times=deque(maxlen=120)
                    previous_frame_id=None
                    previous_image=None
                    last_target=None
                    previous_area=None
                    previous_confidence=None
                    while not self.stop_event.is_set():
                        start = time.perf_counter()
                        image, area = source.grab()
                        if image is None:
                            self.follower.poll_hotkeys(live=True)
                            continue
                        self.capture_info=dict(source.info)
                        self.current_capture_area=area
                        captured = time.perf_counter()
                        observed_at=source.observed_at
                        if source.frame_id is not None:
                            new_frame=source.frame_id!=previous_frame_id
                            previous_frame_id=source.frame_id
                        else:
                            new_frame = (previous_image is None or area!=previous_area or
                                         not np.array_equal(image,previous_image))
                        if new_frame or self.confidence!=previous_confidence:
                            raw_rows, ms = self.detector.infer(image, self.confidence,input_size=self.inference_size)
                            previous_image=image
                            previous_area=area
                            previous_confidence=self.confidence
                        else:
                            ms=0.
                        inferred = time.perf_counter()
                        rows,self.target_filter_report=filter_targets(image,raw_rows,self.confidence,strict=self.enemy_filter,
                            enemy_color=getattr(self, 'enemy_color', 'red'),friend_guard=getattr(self,'friend_guard',False))
                        filtered=time.perf_counter()
                        try:
                            target = self.follower.update(rows, area, observed_at, self.confidence,new_frame=new_frame)
                            if new_frame or target is not None:
                                last_target=target
                            elif self.follower.enabled and self.follower.snapshot()['stage']=='repeat_frame':
                                target=last_target
                        except OSError:
                            # Input failure must not stop capture/inference or hide diagnostics.
                            target = None
                            detail = traceback.format_exc()
                            (ROOT / 'input-error.log').write_text(
                                f'mode={self.follower.mode}\n{detail}', encoding='utf-8')
                        controlled = time.perf_counter()
                        render_preview = controlled >= next_preview
                        # Annotation, resize and black-frame diagnostics run
                        # outside the control thread. Mailbox capacity is one.
                        render_finished = time.perf_counter()
                        elapsed = render_finished - start
                        self.live_fps_limit = (LIVE_MAX_FPS if self.detector.info.get('backend')=='tensorrt' else 60)
                        wait_for_frame(self.stop_event, start + 1 / self.live_fps_limit)
                        fps = 1 / max(time.perf_counter() - start, .001)
                        if new_frame:fresh_times.append(captured)
                        fresh_fps=(len(fresh_times)-1)/(fresh_times[-1]-fresh_times[0]) if len(fresh_times)>1 else 0.
                        self.performance = dict(capture_ms=round((captured-start)*1000,2),
                                                inference_ms=round(ms,2),
                                                filter_ms=round((filtered-inferred)*1000,2),
                                                control_ms=round((controlled-filtered)*1000,2),
                                                process_ms=round(elapsed*1000,2),
                                                capture_width=area['width'],capture_height=area['height'],
                                                input_size=self.inference_size,
                                                new_frame=new_frame,
                                                loop_fps=round(fps,2))
                        self.performance.update(render_ms=round((render_finished-controlled)*1000,3),
                                                capture_to_submit_ms=round((controlled-start)*1000,3),
                                                pacing_ms=round((time.perf_counter()-render_finished)*1000,3),
                                                loop_ms=round(1000/fps,3),
                                                inference_reused=not new_frame,
                                                submitted=self.follower.snapshot()['stage']=='sent',
                                                inference_stages=dict(getattr(self.detector,'timings',{})))
                        self.performance.update(frame_age_at_capture_ms=round(max(0.,captured-observed_at)*1000,3),
                                                frame_to_submit_ms=round(max(0.,controlled-observed_at)*1000,3),
                                                fresh_frame_fps=round(fresh_fps,2),
                                                timestamp_source=self.capture_info.get('timestamp_source'),
                                                renderer_error=renderer.error)
                        self.latency.add(self.performance)
                        if render_preview:
                            renderer.submit((image,rows,target,self.follower.snapshot(),ms,fresh_fps))
                            next_preview=controlled+.1
        except Exception:
            self.follower.set_enabled(False)
            detail = traceback.format_exc()
            (ROOT / 'preview-error.log').write_text(detail, encoding='utf-8')
            self.post(('error', detail.splitlines()[-1]))
        finally:
            if source is not None:
                source.close()
            if renderer is not None:renderer.close()

    def poll_hotkeys(self):
        running = self.worker is not None and self.worker.is_alive()
        f9_down=self.pointer.key_down(0x78)
        if f9_down and not self._f9_was_down:self.diagnose_input()
        self._f9_was_down=f9_down
        self.follower.poll_hotkeys(live=self.live_mode and running)
        if self.stop_event.is_set() and self.follower.enabled:
            self.disarm()
        self._sync_follow_state(running)
        state = self.follower.snapshot()
        filtering=self.target_filter_report
        self.target_filter_status.set(format_recognition_report(filtering, state.get('aim_region', 'original')))
        prediction=state['prediction']
        if not state['predictive']:
            self.prediction_status.set('位置预测已关闭，使用兼容控制')
        elif prediction['ready'] and state['enabled'] and state['tracking_state']=='tracking':
            self.prediction_status.set(f"目标速度 {prediction['target_velocity_px_s']} px/s · 反馈估计 {prediction['estimated_feedback_ms']:.0f} ms")
        else:
            self.prediction_status.set('确认目标后估计运动 · 目标丢失时暂停移动')
        via_logitech = state['backend'] == 'logitech'
        stages = {'idle': '尚无截图', 'disabled': '控制关闭', 'stale': '画面超时，未发送',
                  'no_target': '无可用目标，未发送', 'centered': '目标已居中，未发送',
                  'focus_lost': '目标不在前台或无法确认，控制已关闭',
                  'focus_paused':'目标在后台：暂停输出，开关仍开启',
                  'focus_reacquiring':'已切回目标窗口：用新画面重新确认目标',
                  'manual_unavailable': '手动鼠标监测不可用或丢帧，控制已关闭',
                  'firing_pause':'左键按下，暂停辅助',
                  'manual_yield':'手动输入优先，辅助已让出操作轴',
                  'fire_cooldown':'松键后等待新画面并重新确认目标',
                  'fire_reacquiring':'开火后等待不同的新画面确认目标',
                  'fire_input_unavailable':'开火按键状态不可用，控制已关闭',
                  'input_inflight': '上一条输入仍在处理，未发送新指令',
                  'target_rejected':'未通过敌方校验，未发送',
                  'acquiring':'确认目标中，未发送','target_hold':'目标短暂丢失，暂停移动',
                  'repeat_frame':'相同画面，未重复修正','settling':'细调等待，未发送',
                  'sent': 'Windows 已接收移动', 'input_error': '输入失败，识别继续'}
        if state.get('miss_reason') == 'ambiguous_target':
            stages['target_hold'] = '多个目标难以区分，暂停移动'
        if via_logitech:
            stages['sent'] = 'DLL 调用完成，设备接收未确认'
        if state['backend'] == 'virtualhid':
            stages['sent'] = 'HID 报告已提交'
        mode_name = '实时准心跟随' if state['mode'] == 'relative' else '桌面光标定位'
        activity = '截图中' if running and self.live_mode else '未实时截图'
        backend_name = {'logitech': '罗技 DLL', 'windows': 'Windows', 'virtualhid': '虚拟 HID'}[state['backend']]
        text = (f"诊断：{backend_name} · {mode_name} · {activity} · {state['detections']} 个目标 · "
                f"{stages[state['stage']]} · 算法 {state['control_algorithm']} · 调用 {state['sent']} 次")
        if state.get('target_error_pixels') is not None:
            text += f" · 准心偏差(px) {state['target_error_pixels']} · HID修正 {state['last_output']}"
            text += ' · 检测头部' if state.get('aim_source') == 'detected_head' else ' · 框内估计位置'
        if self.capture_black and self.live_mode:
            text += ' · 当前采集画面几乎全黑'
        if state['input_error']:
            text += ' · ' + state['input_error']
        manual=(state.get('manual_input') or {}).get('monitor')
        if manual:
            text+=(f" · 手动补偿已接通 · 物理 {manual['physical_reports']} / HID 排除 {manual['virtual_reports_ignored']}"
                   if manual['available'] else ' · 手动补偿不可用：'+str(manual['error']))
        if state['manual_fire_priority'] and manual:
            axes=(state.get('manual_input') or {}).get('blend',{}).get('manual_yielded_axes',[])
            if any(axes):text+=' · 手动让控轴 '+','.join(axis for axis,on in zip(('X','Y'),axes) if on)
        self.diagnostic_status.set(text)
        if time.monotonic() >= self._next_diagnostic_write:
            self.write_diagnostic(state, running)
            self._next_diagnostic_write = time.monotonic() + 1
        self.root.after(20, self.poll_hotkeys)

    def write_diagnostic(self, state=None, running=False):
        data = state if state is not None else self.follower.snapshot()
        data = dict(data, recorded_unix=time.time(), process_id=os.getpid(), session_id=self.session_id,
                    version='V8',hotkeys=dict(toggle_follow='F8',escape_action='none'),live_max_fps=self.live_fps_limit,
                    enemy_filter=self.enemy_filter,enemy_color=getattr(self, 'enemy_color', 'red'),friend_guard=getattr(self,'friend_guard',False),
                    target_filter=dict(self.target_filter_report),
                    capture_scope=self.capture_scope,inference_size=self.inference_size,
                    capture_area=self.current_capture_area,
                    capture_backend=self.capture_info['backend'],capture_backend_info=self.capture_info,
                    inference=getattr(self.detector, 'info', None), performance=self.performance,
                    latency_window=self.latency.snapshot(),
                    game_calibration=GAME_CALIBRATION,
                    is_admin=self.is_admin, running=bool(running), live_mode=self.live_mode,
                    capture_black=self.capture_black)
        path = ROOT / '.settings' / 'runtime-diagnostic.json'
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            trace=self.follower.trace_snapshot()
            if trace:
                (ROOT/'verification'/'control-trace.json').write_text(
                    json.dumps([dict(item,session_id=self.session_id) for item in trace],ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:
            pass  # A diagnostic-file problem must not stop the UI.

    def poll(self):
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == 'backend_ready':
                self.backend_status.set(item[1])
                self.connection_button.configure(state='normal')
                continue
            if item[0] == 'input_diagnostic':
                self.input_diagnostic_status.set(item[1]+' · verification/game-input-diagnostic.json')
                continue
            if item[0] == 'error':
                self.status.set('识别失败：' + item[1] + '；详情见 preview-error.log')
                if self.smoke:
                    print('UI_SMOKE_ERROR', item[1], flush=True)
                    self.close()
                    return
            else:
                _, self.last_image, rows, ms, fps = item
                self.processed += 1
                suffix = f' · 新帧处理 {fps:.1f} 帧/秒（上限 {self.live_fps_limit}）' if fps else ''
                inference_text = (f'推理 {ms:.0f} 毫秒' if ms else '相同画面，复用检测结果')
                self.status.set(f'检测到 {len(rows)} 个目标 · {inference_text}' + suffix)
                info = getattr(self.detector, 'info', {})
                device = info.get('gpu_name') or info.get('device', '未知')
                message = f"推理设备：{device} · {info.get('backend','pytorch')} · {info.get('precision', '')}"
                if self.performance:
                    message += (f" · 截图 {self.performance['capture_ms']:.1f} ms"
                                f" · 控制 {self.performance['control_ms']:.1f} ms")
                    message += (f" · {CAPTURE_SCOPES[self.capture_scope]} "
                                f"{self.performance['capture_width']}×{self.performance['capture_height']}"
                                f" · 识别长边 {self.inference_size}")
                    message += ' · 截图 '+self.capture_info['backend'].upper()
                    latency=self.latency.snapshot()['stages'].get('capture_to_submit_ms')
                    if latency:
                        message += f" · 处理 P50/P95 {latency['p50']:.1f}/{latency['p95']:.1f} ms"
                if info.get('fallback_reason'):
                    message += ' · 使用兼容推理路径'
                if info.get('last_path')=='fp32_boundary_guard':
                    message += ' · 临界分数已复核'
                self.performance_status.set(message)
                self.results.set('  |  '.join(f"{r['label']} {r['confidence']:.0%}" for r in rows[:8]) or '当前画面没有达到置信度阈值的目标。')
                self.redraw()
                if self.smoke:
                    print(f'UI_SMOKE_OK detections={len(rows)} inference_ms={ms:.1f}', flush=True)
                    self.root.after(500, self.close)
        if self.worker is not None and not self.worker.is_alive():
            self.monitor.configure(state='readonly')
            self.capture_choice.configure(state='readonly')
            self.inference_choice.configure(state='readonly')
            for button in (self.start_button, self.image_button, self.sample_button):
                button.configure(state='normal')
        self.root.after(80, self.poll)

    def redraw(self):
        if self.last_image is None:
            return
        import cv2
        from PIL import Image, ImageTk
        image = Image.fromarray(cv2.cvtColor(self.last_image, cv2.COLOR_BGR2RGB))
        image.thumbnail((max(1, self.canvas.winfo_width() - 16), max(1, self.canvas.winfo_height() - 16)))
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.delete('all')
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.photo)

    def start_live(self):
        self.begin()

    def start_sample(self):
        self.begin(ROOT / 'samples' / 'validation-tile.jpg')

    def open_image(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title='选择要识别的图片', filetypes=[('图片', '*.png *.jpg *.jpeg *.bmp'), ('所有文件', '*.*')])
        if path:
            self.begin(path)

    def open_offline_video(self):
        import subprocess
        # Disarm this live preview before opening the independent analyzer.
        self.stop()
        subprocess.Popen([sys.executable, str(ROOT / 'video_offline.py')],
                         cwd=str(ROOT), creationflags=subprocess.CREATE_NO_WINDOW)

    def stop(self):
        self.stop_event.set()
        self.disarm()
        self.status.set('已请求停止，正在完成当前帧…')

    def smoke_timeout(self):
        if self.smoke and not self.processed:
            print('UI_SMOKE_TIMEOUT', flush=True)
            self.close()

    def close(self):
        self.stop_event.set()
        self.disarm()
        if self._motion_save_job is not None:
            self.save_motion_profile()
        self.write_diagnostic(running=False)
        if hasattr(self.pointer, 'close'):
            self.pointer.close()
        if self.manual_monitor is not None:self.manual_monitor.close()
        self.root.destroy()


def self_test(screen=False, device='auto'):
    import cv2
    import numpy as np
    import torch
    detector = Detector(device=device)
    report = {'torch': torch.__version__, 'device': str(detector.device), 'inference': detector.info, 'model_sha256': MODEL_HASH,
              'model_classes': detector.names, 'cases': []}
    for name, image in [('blank', np.zeros((640, 640, 3), np.uint8)),
                        ('upstream_readme', read_image(ROOT / 'samples' / 'upstream.png')),
                        ('validation_tile', read_image(ROOT / 'samples' / 'validation-tile.jpg'))]:
        timings = []
        for _ in range(3):
            rows, ms = detector.infer(image)
            timings.append(round(ms, 2))
        if name == 'blank' and rows:
            raise AssertionError('空白图检测出了目标。')
        if name == 'validation_tile' and not rows:
            raise AssertionError('验证切片未检测到目标。')
        if name == 'validation_tile':
            cv2.imwrite(str(ROOT / 'verification' / 'sample-detections.png'), annotate(image, rows))
        report['cases'].append({'name': name, 'detections': rows, 'inference_ms': timings})
    if screen:
        from mss import mss
        with mss() as sct:
            image = capture(sct, 1)
            rows, ms = detector.infer(image)
            report['cases'].append({'name': 'live_screen', 'shape': list(image.shape),
                                    'detection_count': len(rows), 'inference_ms': round(ms, 2),
                                    'screen_pixels_saved': False})
    target = ROOT / 'verification' / 'report.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    enable_physical_coordinates()
    parser = argparse.ArgumentParser()
    parser.add_argument('--self-test', action='store_true')
    parser.add_argument('--screen-test', action='store_true')
    parser.add_argument('--ui-smoke', action='store_true')
    parser.add_argument('--input-backend', choices=['windows', 'logitech', 'virtualhid'], default='virtualhid')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    parser.add_argument('--start-live', action='store_true', help='Start recognition with movement disabled')
    args = parser.parse_args()
    if args.self_test:
        self_test(args.screen_test, args.device)
    else:
        import tkinter as tk
        root = tk.Tk()
        app = PreviewApp(root, args.ui_smoke, input_backend=args.input_backend, inference_device=args.device)
        if args.start_live:
            root.after(200, app.start_live)
        root.mainloop()
        if args.ui_smoke:
            raise SystemExit(0 if app.processed else 1)
