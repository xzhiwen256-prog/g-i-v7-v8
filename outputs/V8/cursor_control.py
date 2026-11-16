"""Desktop and relative movement backends, including the local virtual HID broker."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import math
import threading
import time
from collections import deque
from stable_aim import StableAim
from fire_gate import FireGate
from fire_commitment import FireCommitment
from pd_control import validate_pd
from aim_region import (selection_policy, validate_region, AIM_REGION_POLICY_REVISION,
                        CHEST_HEIGHT_RATIO, CHEST_TUNING_REVISION)

F8 = 0x77
ESCAPE = 0x1B
LEFT_BUTTON = 0x01


class MouseInput(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class InputPayload(ctypes.Union):
    _fields_ = [('mi', MouseInput)]


class InputEvent(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('payload', InputPayload)]


def enable_physical_coordinates():
    """Call before creating Tk windows, so capture and cursor use physical pixels."""
    api = ctypes.WinDLL('user32', use_last_error=True)
    if hasattr(api, 'SetProcessDpiAwarenessContext'):
        api.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        api.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        # ERROR_ACCESS_DENIED means awareness was already configured by the host.
        api.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


class DesktopPointer:
    def __init__(self):
        self.backend = 'windows'
        self._logitech = None
        self._virtual_hid = None
        self._backend_lock = threading.RLock()
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        self.api.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.api.GetAsyncKeyState.restype = ctypes.c_short
        self.api.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.api.SetCursorPos.restype = wintypes.BOOL
        self.api.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.api.GetCursorPos.restype = wintypes.BOOL
        self.api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(InputEvent), ctypes.c_int]
        self.api.SendInput.restype = wintypes.UINT

    def key_down(self, key):
        return bool(self.api.GetAsyncKeyState(key) & 0x8000)

    def position(self):
        point = wintypes.POINT()
        if not self.api.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return point.x, point.y

    def move_to(self, x, y):
        if self.backend in ('logitech', 'virtualhid'):
            raise OSError('此输入后端只支持相对移动，请选择“视角相对移动”。')
        if not self.api.SetCursorPos(int(x), int(y)):
            raise ctypes.WinError(ctypes.get_last_error())

    def move_relative(self, dx, dy):
        if not dx and not dy:
            return
        if self.backend == 'virtualhid':
            with self._backend_lock:
                self._connect_virtual_hid().move_relative(dx, dy)
            return
        if self.backend == 'logitech':
            with self._backend_lock:
                self._connect_logitech().move_relative(dx, dy)
            return
        event = InputEvent()
        event.type = 0  # INPUT_MOUSE
        event.payload.mi = MouseInput(int(dx), int(dy), 0, 0x0001, 0, 0)
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(InputEvent)) != 1:
            raise OSError(ctypes.get_last_error(), 'Windows 未接收标准相对鼠标输入。')

    def _connect_logitech(self):
        if self._logitech is None:
            from logitech_backend import LogitechClient
            self._logitech = LogitechClient()
        return self._logitech

    @property
    def supports_trigger(self):
        return self.backend == 'virtualhid'

    def trigger_click(self, guard, expires_at):
        with self._backend_lock:
            if not self.supports_trigger:raise OSError('自动扳机仅支持现有虚拟 HID 后端，不回退到其他输入方式。')
            client=self._connect_virtual_hid()
            return client.trigger_click(guard,expires_at)

    def cancel_trigger(self):
        client=getattr(self,'_virtual_hid',None)
        if client is not None:client.cancel_trigger()

    def trigger_error(self):
        client=getattr(self,'_virtual_hid',None)
        return getattr(client,'_background_error',None)

    def set_backend(self, name):
        if name not in ('windows', 'logitech', 'virtualhid'):
            raise ValueError('Unknown mouse input backend')
        with self._backend_lock:
            self.close()
            self.backend = name

    def _connect_virtual_hid(self):
        if self._virtual_hid is not None and self._virtual_hid.process.poll() is not None:
            self._virtual_hid.close()
            self._virtual_hid = None
        if self._virtual_hid is None:
            from virtual_hid_backend import VirtualHidClient
            self._virtual_hid = VirtualHidClient()
        return self._virtual_hid

    def check_backend(self):
        with self._backend_lock:
            if self.backend == 'virtualhid':
                self._connect_virtual_hid().request('status')
                return '虚拟 HID 已连接 · 相对移动通过 HID 报告提交'
            if self.backend == 'logitech':
                self._connect_logitech()
                return '罗技 DLL 句柄已打开；鼠标移动尚未验证'
            return 'Windows 标准输入已选择'

    def close(self):
        with self._backend_lock:
            if self._virtual_hid is not None:
                self._virtual_hid.close()
                self._virtual_hid = None
            if self._logitech is not None:
                self._logitech.close()
                self._logitech = None


def select_target(rows, area, confidence, aim_height_ratio=.5, prefer_head=False, *, aim_region="original"):
    """Choose the visible aim point closest to the capture area's center.

    Height is measured down from the box top; .5 preserves center targeting.
    """
    rows, aim_height_ratio, prefer_head = selection_policy(rows, aim_region, aim_height_ratio, prefer_head)
    width, height = area['width'], area['height']
    if width <= 0 or height <= 0:
        return None
    candidates = []
    for row in rows:
        if (row.get('target_eligible') is False or row.get('visible') is False or row.get('occluded') is True or
                row.get('team') in ('ally','friendly','teammate') or
                row.get('label') in ('ally','friendly','teammate','ally_head','friendly_head','teammate_head')):
            continue
        try:
            x1, y1, x2, y2 = (float(v) for v in row['box'])
            score = float(row['confidence'])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2, score)):
            continue
        if score < confidence or x2 <= x1 or y2 <= y1:
            continue
        is_head = row.get('label') == 'enemy_head'
        ratio = .5 if prefer_head and is_head else aim_height_ratio
        cx, cy = (x1 + x2) / 2, y1 + (y2 - y1) * ratio
        if not (0 <= cx < width and 0 <= cy < height):
            continue
        distance = (cx - width / 2) ** 2 + (cy - height / 2) ** 2
        candidates.append((distance, -score, cx, cy, row, is_head))
    if not candidates:
        return None
    if prefer_head:
        heads = [value for value in candidates if value[5]]
        if heads:
            candidates = heads
    _, _, cx, cy, row, is_head = min(candidates, key=lambda value: value[:2])
    sx = area['left'] + min(width - 1, max(0, round(cx)))
    sy = area['top'] + min(height - 1, max(0, round(cy)))
    return {'screen': (sx, sy),
            'local': (sx - area['left'], sy - area['top']),
            'point': (cx, cy), 'row': row,
            'aim_source': ('chest_box_estimate' if aim_region == 'chest' else
                           'detected_head' if prefer_head and is_head else 'box_estimate')}


def relative_step(target, area, gain=.25, limit=30, deadzone=3, near_slowdown=False):
    """Convert image-center error to a bounded relative mouse step."""
    point = target.get('point')
    if point is None:
        x1, y1, x2, y2 = target['row']['box']
        point = ((x1 + x2) / 2, (y1 + y2) / 2)
    ex = point[0] - area['width'] / 2
    ey = point[1] - area['height'] / 2
    distance = math.hypot(ex, ey)
    if distance <= deadzone:
        return 0, 0
    if near_slowdown:
        # Continuous transition: full gain beyond 60 px, half gain near center.
        gain *= .5 + .5 * min(distance / 60, 1)
    dx = max(-limit, min(limit, round(ex * gain)))
    dy = max(-limit, min(limit, round(ey * gain)))
    if dx == 0 and dy == 0:
        if abs(ex) >= abs(ey):
            dx = 1 if ex > 0 else -1
        else:
            dy = 1 if ey > 0 else -1
    return dx, dy


class CursorFollower:
    def __init__(self, pointer, stop_event, clock=time.perf_counter):
        self.pointer = pointer
        self.stop_event = stop_event
        self.clock = clock
        self.lock = threading.Lock()
        self.enabled = False
        self._cancel_event = threading.Event()
        self._cancel_event.set()
        self._generation = 0
        self._inflight = False
        self._foreground_check = None
        self._f8_was_down = False
        self._f8_presses = 0
        self._last_f8_result = None
        self.mode = 'desktop'
        self.gain = .25
        self.step_limit = 30
        self.near_slowdown = False
        self.stabilization = False
        self.response_ms = 80
        self.chase_strength = 1.
        self.predictive = False
        self.aim_region = "original"
        self.stable = StableAim(self._select_target)
        self.aim_height_ratio = .5
        self.prefer_head = False
        self._aim_source = None
        self.last_status = '鼠标定位未启用'
        self._frames = 0
        self._detections = 0
        self._eligible_detections = 0
        self._filter_reasons = {}
        self._sent = 0
        self._stage = 'idle'
        self._frame_age_ms = 0
        self._last_output = None
        self._input_error = None
        self._target_error = None
        self._target_label = None
        self._trace = deque(maxlen=240)
        self.low_latency=False
        self.manual_monitor=None
        self.manual_blend=None
        self._manual_overflow_at_arm=0
        # Library defaults preserve legacy callers; V5 preview/profile explicitly
        # enables relaxed assistance and passive firing protection.
        self.control_algorithm="legacy"
        self.pd_kp=1.;self.pd_kd=.25
        self.soft_assist=False
        self.tolerance_px=8.
        self.manual_fire_priority=False
        self._manual_left_down=False
        self.pause_on_fire=False
        self.fire_release_ms=180.
        self.fire_gate=FireGate()
        self.fire_target_lock=False  # V7 UI opts in; library callers remain compatible.
        self.fire_commitment=FireCommitment()
        self._fire_observation_lock=threading.RLock()
        self._fire_error=None
        self._post_fire_reacquire=False
        self._post_fire_last_frame=None
        # V5 preview opts in. Legacy library callers keep their old behavior.
        # enabled remains the user's switch; focus only gates output.
        self.pause_on_focus_loss=False
        self._focus_paused=False
        self._focus_reacquire_after=None
        self._focus_last_frame=None
        self._focus_new_frames=0
        self._focus_pause_count=0
        self._focus_resume_count=0
        self._focus_events=deque(maxlen=32)

    def _select_target(self, rows, area, confidence, aim_height_ratio=.5, prefer_head=False):
        return select_target(rows, area, confidence, aim_height_ratio, prefer_head, aim_region=self.aim_region)

    def use_low_latency(self,manual_monitor=None):
        from aggressive_aim import AggressiveAim
        from raw_mouse import ManualBlend
        with self.lock:
            self._disarm_locked()
            self.low_latency=True
            self.stable=AggressiveAim(self._select_target)
            self.stable.predictive=self.predictive
            self.manual_monitor=manual_monitor
            self.manual_blend=ManualBlend()
            self._sync_assistance_locked()

    def _sync_assistance_locked(self):
        self.stable.control_algorithm=self.control_algorithm
        self.stable.pd_kp=self.pd_kp;self.stable.pd_kd=self.pd_kd
        self.stable.target_continuity_enabled=self.fire_target_lock
        self.stable.soft_assist=self.soft_assist
        self.stable.tolerance_px=self.tolerance_px
        if self.manual_blend is not None:
            self.manual_blend.soft=self.soft_assist
            self.manual_blend.fire_priority=self.manual_fire_priority

    def _manual_snapshot(self):
        return dict(monitor=self.manual_monitor.snapshot() if self.manual_monitor else None,
                    blend=self.manual_blend.snapshot() if self.manual_blend else None)

    def trace_snapshot(self):
        with self.lock:
            return list(self._trace)

    def _record_control(self):
        if self.stabilization:
            self._trace.append(dict(time=self.clock(),stage=self._stage,
                                    error_pixels=list(self._target_error) if self._target_error is not None else None,
                                    delta=list(self._last_output) if self._last_output is not None else None,
                                    source=self._aim_source,tracking=self.stable.state,
                                    focus_paused=self._focus_paused,
                                    focus_reacquiring=self._focus_reacquire_after is not None,
                                    frame_age_ms=self._frame_age_ms,dt_ms=self.stable.dt_ms,
                                    response_ms=self.response_ms,chase_strength=self.chase_strength,
                                    effective_response_ms=self.stable.effective_response_ms,
                                    predictive=self.predictive,prediction=self.stable.predictor.snapshot(),
                                    miss_reason=self.stable.miss_reason,association=self.stable.association,
                                    eligible_detections=self._eligible_detections,filter_reasons=self._filter_reasons,
                                    acceleration_multiplier=round(self.stable.acceleration_multiplier,3)))
            self._trace[-1].update(adaptive_filter=bool(getattr(self.stable,'adaptive_filter',False) and self.predictive),
                                   filtered_point=getattr(self.stable,'filtered_point',None),
                                   axis_response_ms=list(getattr(self.stable,'axis_response_ms',[])),
                                   axis_braking=list(getattr(self.stable,'axis_braking',[])))
            if self.low_latency:self._trace[-1]['manual_input']=self._manual_snapshot()
            self._trace[-1].update(soft_assist=self.soft_assist,tolerance_px=self.tolerance_px,
                                   pause_on_fire=self.pause_on_fire,fire_gate=self.fire_gate.snapshot(),
                                   fire_release_ms=self.fire_release_ms,fire_error=self._fire_error,
                                    control_algorithm=self.control_algorithm,pd_state=self.stable.pd.snapshot(),
                                    lock_state=self.stable.lock_servo.snapshot() if self.control_algorithm=='lock' else None,
                                    fire_target_lock=self.fire_target_lock,
                                    fire_commitment=self.fire_commitment.snapshot(self.clock()),
                                    target_continuity=dict(self.stable.engagement_state))

    def pipeline_snapshot(self):
        """Read-only per-loop health fields; full UI/diagnostic snapshot is unchanged."""
        with self.lock:
            return {'stage': self._stage, 'detections': self._detections,
                    'eligible_detections': self._eligible_detections,
                    'prediction': self.stable.predictor.snapshot(),
                    'effective_response_ms': self.stable.effective_response_ms,
                    'control_dt_ms': self.stable.dt_ms}

    def snapshot(self):
        with self.lock:
            return {'low_latency':self.low_latency,'manual_input':self._manual_snapshot(),
                    'soft_assist':self.soft_assist,'tolerance_px':self.tolerance_px,
                    'manual_fire_priority':self.manual_fire_priority,'physical_left_down':self._manual_left_down,
                    'pause_on_fire':self.pause_on_fire,'fire_release_ms':self.fire_release_ms,
                    'fire_gate':self.fire_gate.snapshot(),'fire_error':self._fire_error,
                    'fire_target_lock':self.fire_target_lock,
                    'fire_commitment':self.fire_commitment.snapshot(self.clock()),
                    'target_continuity':dict(self.stable.engagement_state),
                    'control_algorithm':self.control_algorithm,'pd_kp':self.pd_kp,'pd_kd':self.pd_kd,
                    'pd_state':self.stable.pd.snapshot(),
                    'lock_state':self.stable.lock_servo.snapshot() if self.control_algorithm=='lock' else None,
                    'focus_paused':self._focus_paused,
                    'focus_reacquiring':self._focus_reacquire_after is not None,
                    'focus_new_frames':self._focus_new_frames,
                    'focus_pause_count':self._focus_pause_count,'focus_resume_count':self._focus_resume_count,
                    'focus_events':[dict(event) for event in self._focus_events],
                    'last_status': self.last_status,
                    'mode': self.mode, 'gain': self.gain, 'enabled': self.enabled,
                    'step_limit': self.step_limit, 'near_slowdown': self.near_slowdown,
                    'stabilization': self.stabilization, 'response_ms': self.response_ms,
                    'chase_strength': self.chase_strength,
                    'predictive': self.predictive,'prediction': self.stable.predictor.snapshot(),
                    'miss_reason': self.stable.miss_reason,'association': self.stable.association,
                    'effective_response_ms': self.stable.effective_response_ms,
                    'tracking_state': self.stable.state, 'control_dt_ms': self.stable.dt_ms,
                    'adaptive_filter':bool(getattr(self.stable,'adaptive_filter',False) and self.predictive),
                    'filtered_point':getattr(self.stable,'filtered_point',None),
                    'axis_response_ms':list(getattr(self.stable,'axis_response_ms',[])),
                    'axis_braking':list(getattr(self.stable,'axis_braking',[])),
                    'aim_region': self.aim_region,
                    'aim_region_policy_revision': AIM_REGION_POLICY_REVISION,
                    'chest_tuning_revision': CHEST_TUNING_REVISION,
                    'active_aim_height_ratio': CHEST_HEIGHT_RATIO if self.aim_region == 'chest' else self.aim_height_ratio,
                    'position_filter_profile': getattr(self.stable, 'position_filter_profile', None),
                    'aim_height_ratio': self.aim_height_ratio,
                    'prefer_head': self.prefer_head, 'aim_source': self._aim_source,
                    'backend': getattr(self.pointer, 'backend', 'windows'),
                    'frames': self._frames, 'detections': self._detections,
                    'eligible_detections':self._eligible_detections,'filter_reasons':dict(self._filter_reasons),
                    'sent': self._sent, 'stage': self._stage, 'input_inflight': self._inflight,
                    'frame_age_ms': self._frame_age_ms, 'last_output': self._last_output,
                    'input_error': self._input_error, 'f8_presses': self._f8_presses,
                    'last_f8_result': self._last_f8_result,
                    'reference': 'frame_center' if self.mode == 'relative' else 'desktop',
                    'target_error_pixels': self._target_error,
                    'target_label': self._target_label}

    def set_foreground_check(self, check):
        """Install a fail-closed predicate; production preview supplies one."""
        if check is not None and not callable(check):
            raise ValueError('foreground check must be callable')
        with self.lock:
            self._foreground_check = check

    def _foreground_allowed(self):
        try:
            return self._foreground_check is None or self._foreground_check() is True
        except Exception:
            return False

    def _manual_state(self):
        if self.manual_monitor is None:
            return not self.manual_fire_priority, 0
        try:
            state = self.manual_monitor.snapshot()
            drops = state.get('dropped_reports', 0)
            if type(drops) is not int or drops < 0:
                return False, 0
            return state.get('available') is True and not state.get('error'), drops
        except Exception:
            return False, 0

    def _manual_lost_locked(self):
        self._disarm_locked('manual_unavailable', '手动鼠标监测不可用或丢帧 · 控制已关闭')

    def note_trigger_shot(self, generation):
        """Latch only a successfully submitted shot for the still-current lock."""
        with self.lock:
            if (generation!=self._generation or not self.enabled or self._focus_paused
                    or not self.fire_target_lock or self.stable.locked is None
                    or self.stop_event.is_set()):
                return False
            with self._fire_observation_lock:
                self.fire_commitment.shot(self.clock())
            return True

    def _reset_fire_commitment(self):
        # Lock order: follower -> observation -> commitment.
        # Dispatch polls release observation before reacquiring follower.
        with self._fire_observation_lock:
            self.fire_commitment.reset()

    def _hold_target_active(self):
        return self.fire_target_lock and self.fire_commitment.snapshot(self.clock())['active']

    def _reset_observation_gap(self, reason):
        self.stable.retain_target=self._hold_target_active()
        if self.stable.retain_target and self.stable.locked is not None:
            self.stable.pause_target(self.clock(),reason)
        else:
            self.stable.reset()

    def _fire_state(self, captured_at=None):
        # Serialize the clock read and button snapshot across UI/dispatch polls.
        with self._fire_observation_lock:
            return self._read_fire_state(captured_at)

    def _read_fire_state(self, captured_at=None):
        if (not self.pause_on_fire and not self.manual_fire_priority and not self.fire_target_lock) or self.mode != 'relative':
            self._manual_left_down=False
            return None
        try:
            down=self.pointer.key_down(LEFT_BUTTON)
            if type(down) is not bool:
                raise ValueError('Invalid physical button state')
            last_press=None
            if self.manual_monitor is not None:
                state=self.manual_monitor.snapshot()
                held=state.get('left_button_down',False)
                if type(held) is not bool:
                    raise ValueError('Invalid physical button state')
                down=down or held
                last_press=state.get('last_left_press')
            self._manual_left_down=down
            if self.fire_target_lock:
                self.fire_commitment.observe(self.clock(),down,last_press)
            if not self.pause_on_fire:
                self._fire_error=None
                return None
            result=self.fire_gate.observe(self.clock(),down,last_press,self.fire_release_ms,captured_at)
            self._fire_error=None
            return result
        except Exception as exc:
            self._fire_error=f'{type(exc).__name__}: {exc}'
            return 'fire_input_unavailable'

    def _pause_for_fire_locked(self, stage):
        if stage == 'fire_input_unavailable':
            self._disarm_locked(stage,'无法确认开火按键状态 · 控制已关闭')
        else:
            # Also invalidate a result arriving from an already in-flight send.
            # A submitted report cannot be recalled, but must not seed prediction.
            self._generation += 1
            self._reset_observation_gap('firing_pause')
            self._post_fire_reacquire=True
            self._post_fire_last_frame=None
            self._last_output=[0,0]
            self._stage=stage
            self.last_status=('左键按下：暂停辅助，射击由手动控制' if stage=='firing_pause'
                              else '松键恢复间隔：等待新画面后重新确认目标')
        self._target_error=self._target_label=self._aim_source=None
        self._record_control()

    def _disarm_locked(self, stage='disabled', message='定位已关闭 · 按 F8 开启'):
        self._cancel_event.set()
        self._generation += 1
        self.enabled = False
        self._reset_fire_commitment()
        self._focus_paused=False
        self._focus_reacquire_after=None
        self._focus_last_frame=None
        self._focus_new_frames=0
        self._manual_left_down=False
        if self.manual_blend is not None:self.manual_blend.reset_manual_priority()
        self.stable.reset()
        self._last_output = [0, 0]
        self._stage = stage
        self.last_status = message

    def _focus_lost_locked(self):
        if not self.pause_on_focus_loss or not self.enabled:
            self._disarm_locked('focus_lost', '目标不在前台或无法确认 · 控制已关闭')
            return
        if not self._focus_paused:
            self._cancel_event.set()
            self._generation+=1
            self._reset_fire_commitment()
            self.stable.reset()
            self._focus_paused=True
            self._focus_reacquire_after=None
            self._focus_last_frame=None
            self._focus_new_frames=0
            self._focus_pause_count+=1
            self._focus_events.append(dict(kind='paused',time=self.clock(),unix_time=time.time()))
        self._target_error=self._target_label=self._aim_source=None
        self._last_output=[0,0]
        self._stage='focus_paused'
        self.last_status='目标在后台 · 暂停输出，开关保持开启 · 切回继续，F8 关闭'
        self._record_control()

    def _resume_focus_locked(self):
        # Called only after focus, monitor, stop and in-flight checks.
        self.stable.reset()
        if self.manual_blend is not None:
            self.manual_blend.samples.clear()
            self.manual_blend.velocity=[0.,0.]
            self.manual_blend.last_activity=-math.inf
            self.manual_blend.reset_manual_priority()
        self._generation+=1
        self._focus_paused=False
        self._cancel_event.clear()
        self._focus_reacquire_after=self.clock()
        self._focus_last_frame=None
        self._focus_new_frames=0
        self._post_fire_reacquire=False
        self._post_fire_last_frame=None
        self._last_output=[0,0]
        self._target_error=self._target_label=self._aim_source=None
        self._stage='focus_reacquiring'
        self.last_status='已切回目标窗口 · 等待新画面重新确认目标'
        self._focus_resume_count+=1
        self._focus_events.append(dict(kind='resumed_waiting_for_frames',time=self.clock(),unix_time=time.time()))
        self._record_control()

    def note_input_error(self, exc):
        self._cancel_event.set()
        with self.lock:
            self._input_error = f'{type(exc).__name__}: {exc}'
            self._disarm_locked('input_error', '输入连接失败，控制已关闭；识别继续')

    def set_enabled(self, enabled):
        if not enabled:
            # Signal cancellation even if another short state operation owns the lock.
            self._cancel_event.set()
        with self.lock:
            if not enabled:
                self._disarm_locked('input_error' if self._input_error else 'disabled')
                return
            if self._inflight or self.stop_event.is_set():
                self.last_status = '输入仍在处理或采集已停止，暂不能重新开启'
                return
            if self.control_algorithm=='lock' and (not self.predictive or not self.stabilization or self.mode!='relative'):
                self._disarm_locked('invalid_configuration','预测强锁需要预测、稳定跟踪及相对位移；未开启输出')
                return
            manual_ok, drops = self._manual_state()
            if not manual_ok:
                self._manual_lost_locked()
                return
            self._manual_overflow_at_arm = drops
            if not self._foreground_allowed():
                self._focus_lost_locked()
                return
            if self._focus_paused:
                self._resume_focus_locked()
                return
            if not self.enabled:
                self._reset_fire_commitment()
                self.stable.reset()
                self._trace.clear()
                self._generation += 1
            self.enabled = True
            self._cancel_event.clear()
            self._input_error = None
            self.last_status = '定位已开启 · 再按 F8 关闭'

    def configure_motion(self, mode=None, gain=None, step_limit=None, near_slowdown=None,
                         aim_height_ratio=None, prefer_head=None, stabilization=None, response_ms=None,
                         chase_strength=None, predictive=None, soft_assist=None, tolerance_px=None,
                         pause_on_fire=None, fire_release_ms=None,
                         control_algorithm=None,pd_kp=None,pd_kd=None,manual_fire_priority=None,aim_region=None,fire_target_lock=None):
        validate_pd(control_algorithm,pd_kp,pd_kd)
        if aim_region is not None:
            validate_region(aim_region)
        if mode is not None and mode not in ('desktop', 'relative'):
            raise ValueError('Unknown mouse movement mode')
        if gain is not None and (not math.isfinite(gain) or not .05 <= gain <= 8):
            raise ValueError('Movement gain must be between 0.05 and 8')
        if step_limit is not None and (isinstance(step_limit, bool) or
                                      not isinstance(step_limit, int) or not 10 <= step_limit <= 480):
            raise ValueError('Step limit must be an integer between 10 and 480')
        if near_slowdown is not None and not isinstance(near_slowdown, bool):
            raise ValueError('Near slowdown must be a boolean')
        if aim_height_ratio is not None and (not math.isfinite(aim_height_ratio) or
                                            not .05 <= aim_height_ratio <= .5):
            raise ValueError('Aim height ratio must be between 0.05 and 0.5')
        if prefer_head is not None and not isinstance(prefer_head, bool):
            raise ValueError('Prefer head must be a boolean')
        if stabilization is not None and not isinstance(stabilization,bool):
            raise ValueError('Stabilization must be a boolean')
        if response_ms is not None and (not math.isfinite(response_ms) or not (6 if self.low_latency else 15)<=response_ms<=250):
            raise ValueError('Response time must be between 6 and 250 ms')
        if chase_strength is not None and (not math.isfinite(chase_strength) or not 1<=chase_strength<=(6 if self.low_latency else 3)):
            raise ValueError('Chase strength must be between 1 and 6')
        if predictive is not None and not isinstance(predictive,bool):
            raise ValueError('Predictive tracking must be a boolean')
        for name,value in (('soft_assist',soft_assist),('pause_on_fire',pause_on_fire),('manual_fire_priority',manual_fire_priority),('fire_target_lock',fire_target_lock)):
            if value is not None and type(value) is not bool:
                raise ValueError(name+' must be a boolean')
        for name,value,upper in (('tolerance_px',tolerance_px,20.),('fire_release_ms',fire_release_ms,1000.)):
            if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or
                    not math.isfinite(value) or not 0<=value<=upper):
                raise ValueError(name+' is outside the allowed range')
        with self.lock:
            next_algorithm=self.control_algorithm if control_algorithm is None else control_algorithm
            next_predictive=self.predictive if predictive is None else predictive
            if next_algorithm=='lock' and not next_predictive:
                raise ValueError('预测强锁需要启用预测跟踪；参数未应用')
            self._generation += 1
            if aim_region is not None and aim_region != self.aim_region:
                self._disarm_locked(message="定位部位已切换 · 请重新开启跟随")
                self.aim_region = aim_region
                self.stable.reset()
                self._target_error = self._target_label = self._aim_source = None
                self._last_output = [0, 0]
            if mode is not None and mode != self.mode:
                self._disarm_locked(message='控制方式已切换 · 按 F8 重新开启')
                self._input_error = None
                self.mode = mode
                self.stable.reset()
            if gain is not None:
                self.gain = gain
                self.stable.reset_output()
            if step_limit is not None:
                self.step_limit = step_limit
            if near_slowdown is not None:
                self.near_slowdown = near_slowdown
            if aim_height_ratio is not None:
                self.aim_height_ratio = aim_height_ratio
            if prefer_head is not None:
                self.prefer_head = prefer_head
            if stabilization is not None:
                self.stabilization = stabilization
                self.stable.reset()
            if response_ms is not None:
                self.response_ms = response_ms
                self.stable.reset_output()
            if chase_strength is not None:
                self.chase_strength = chase_strength
                self.stable.reset_output()
            if predictive is not None:
                self.predictive = predictive
                self.stable.predictive = predictive
                self.stable.reset()
            for name,value in (('soft_assist',soft_assist),('tolerance_px',tolerance_px),
                               ('pause_on_fire',pause_on_fire),('fire_release_ms',fire_release_ms)):
                if value is not None:
                    setattr(self,name,value)
                    self.stable.reset()
            if fire_target_lock is not None and fire_target_lock!=self.fire_target_lock:
                self._disarm_locked(message='开火保留目标设置已更新 · 请重新开启跟随')
                self.fire_target_lock=fire_target_lock
            if manual_fire_priority is not None:
                self._disarm_locked(message='开火手动优先设置已更新 · 请重新开启跟随')
                self.manual_fire_priority=manual_fire_priority
            if any(value is not None for value in (control_algorithm,pd_kp,pd_kd)):
                # Never change feedback law while output is armed.
                self._disarm_locked(message='控制算法参数已更新 · 重新开启后生效')
                if control_algorithm is not None:self.control_algorithm=control_algorithm
                if pd_kp is not None:self.pd_kp=float(pd_kp)
                if pd_kd is not None:self.pd_kd=float(pd_kd)
            self._sync_assistance_locked()
            if pause_on_fire is False:
                self._post_fire_reacquire=False
                self._post_fire_last_frame=None

    def poll_hotkeys(self, live=False):
        """Toggle on a new F8 press; holding a key never repeats the toggle."""
        with self.lock:
            enabled=self._poll_hotkeys_locked(live)
            if enabled and not self._focus_paused:
                stage=self._fire_state()
                if stage:self._pause_for_fire_locked(stage)
            return self.enabled

    def _poll_hotkeys_locked(self, live):
        invalid_lock=(self.control_algorithm=='lock' and
                      (not self.predictive or not self.stabilization or self.mode!='relative'))
        if invalid_lock and self.enabled:
            self._disarm_locked('invalid_configuration','预测强锁配置不完整，已停止输出')
        f8_down = self.pointer.key_down(F8)
        pressed = f8_down and not self._f8_was_down
        self._f8_was_down = f8_down
        if pressed:
            self._f8_presses += 1
            self._last_f8_result = 'not_live_or_stopped'
        # Turning OFF must work even while the target is in the background.
        if pressed and self.enabled and self.pause_on_focus_loss:
            self._disarm_locked()
            self._last_f8_result='disabled'
            return False
        if self._focus_paused and (not live or self.stop_event.is_set()):
            self._disarm_locked()
            return False
        if self.enabled and not self._foreground_allowed():
            self._focus_lost_locked()
            if pressed:
                self._last_f8_result = 'focus_lost'
            return self.enabled
        if self._focus_paused:
            manual_ok,drops=self._manual_state()
            if not manual_ok or drops!=self._manual_overflow_at_arm:
                self._manual_lost_locked()
            elif not self._inflight:
                self._resume_focus_locked()
        if pressed and live and not self.stop_event.is_set():
            if self.enabled:
                self._disarm_locked()
                self._last_f8_result = 'disabled'
            elif invalid_lock:
                self._disarm_locked('invalid_configuration','预测强锁需要预测、稳定跟踪及相对位移；未开启输出')
                self._last_f8_result = 'invalid_configuration'
            elif self._inflight:
                self.last_status = '上一条输入仍在处理，暂不能重新开启'
                self._last_f8_result = 'input_inflight'
            elif not self._foreground_allowed():
                self._focus_lost_locked()
                self._last_f8_result = 'focus_lost'
            elif not self._manual_state()[0]:
                self._manual_lost_locked()
                self._last_f8_result = 'manual_unavailable'
            else:
                self._manual_overflow_at_arm = self._manual_state()[1]
                self._reset_fire_commitment()
                self.stable.reset()
                self._trace.clear()
                self._input_error = None
                self._generation += 1
                self.enabled = True
                self._cancel_event.clear()
                self._last_f8_result = 'enabled'
                self.last_status = '定位已开启 · 再按 F8 关闭'
        return self.enabled

    def _dispatch_locked(self, move, args, captured_at, stale_limit):
        """Release the state lock during IO; submitted input cannot be recalled."""
        if self._inflight:
            self._stage = 'input_inflight'
            self.last_status = '上一条输入仍在处理，未发送新指令'
            return False
        token = self._generation
        self._inflight = True
        delivered = False
        error = None
        outcome = 'cancelled'
        self.lock.release()
        try:
            if not self._cancel_event.is_set() and not self.stop_event.is_set():
                if not self._foreground_allowed():
                    outcome = 'focus_lost'
                elif self.manual_monitor is not None and (not self._manual_state()[0] or
                        self._manual_state()[1] != self._manual_overflow_at_arm):
                    outcome = 'manual_unavailable'
                else:
                    current = self.clock()
                    if (not math.isfinite(captured_at) or captured_at > current+.005 or
                            current-captured_at > stale_limit):
                        outcome = 'stale'
                    elif (fire_state := self._fire_state(captured_at)) is not None:
                        outcome = fire_state
                    elif not self._cancel_event.is_set() and not self.stop_event.is_set():
                        # There is necessarily a race after this final check: an
                        # already dispatched device report cannot be cancelled.
                        move(*args)
                        delivered = True
                        outcome = 'sent'
        except OSError as exc:
            error = exc
        finally:
            self.lock.acquire()
            self._inflight = False
        if token != self._generation or not self.enabled or self._cancel_event.is_set():
            if delivered:
                self._sent += 1  # Retain an honest count of a late successful acknowledgement.
            if error is not None:
                self._input_error = f'{type(error).__name__}: {error}'
                self._disarm_locked('input_error', '停止期间输入失败，需重新检查连接')
            return False
        if error is not None:
            self._input_error = f'{type(error).__name__}: {error}'
            self._disarm_locked('input_error', '输入失败，控制已关闭；识别继续')
            raise error
        if outcome == 'focus_lost':
            self._focus_lost_locked()
            return False
        if outcome == 'manual_unavailable':
            self._manual_lost_locked()
            return False
        if outcome in ('firing_pause','fire_cooldown','fire_input_unavailable'):
            self._pause_for_fire_locked(outcome)
            return False
        if outcome == 'stale':
            self._stage = 'stale'
            self._reset_observation_gap('stale')
            self._last_output = [0, 0]
            self._frame_age_ms = round((self.clock()-captured_at)*1000, 1)
            self.last_status = '发送前画面过期，跳过输入'
            self._record_control()
            return False
        return delivered

    def update(self, rows, area, captured_at, confidence=.3, live=True, new_frame=True):
        with self.lock:
            if self.manual_monitor is not None:
                try:
                    events=self.manual_monitor.drain()
                    manual_ok, drops=self._manual_state()
                except Exception:
                    events=[]
                    manual_ok, drops=False, 0
                if self.enabled and (not manual_ok or drops != self._manual_overflow_at_arm):
                    self._manual_lost_locked()
                    self._record_control()
                    return None
                if self._focus_reacquire_after is not None:
                    events=[event for event in events if event[0]>=self._focus_reacquire_after]
                self.manual_blend.observe(events,self.clock())
                if events and self.control_algorithm=='pd':self.stable.pd.reset()
                if self.enabled and not self._focus_paused and self.predictive:self.stable.predictor.external(events,self.gain)
            if self.control_algorithm=='lock':
                # Safety/hotkeys must run even if every capture is rejected.
                self._poll_hotkeys_locked(live)
                if not self.enabled or self._focus_paused or self.stop_event.is_set() or not live:
                    return None
                # A claimed 'new frame' must have a distinct finite capture
                # timestamp. Never turn a repeated/future observation into an
                # extra strong command, including during acquisition.
                now=self.clock()
                invalid=(isinstance(captured_at,bool) or not isinstance(captured_at,(int,float))
                         or not math.isfinite(captured_at) or captured_at>now+.005)
                repeated=(not new_frame or (self.stable.lock_frame_at is not None and
                                            captured_at<=self.stable.lock_frame_at)) if not invalid else False
                if invalid or repeated:
                    if invalid:
                        self.stable.reset_output(preserve_camera=True)
                    self._last_output=[0,0]
                    self._stage='invalid_frame' if invalid else 'repeat_frame'
                    self.last_status='强锁等待有效的新画面，不重复或预先发送'
                    self._record_control()
                    return None
            self._frames += 1
            self._detections = len(rows)
            filtered_rows = rows
            if self.aim_region == 'chest':
                # Resolve head/body evidence BEFORE StableAim splits rows into
                # one-row selector calls. Keep the raw filter annotations intact.
                rows, _, _ = selection_policy(rows, 'chest', self.aim_height_ratio, self.prefer_head)
            self._eligible_detections=sum(row.get('target_eligible') is not False for row in rows)
            self._filter_reasons={}
            for row in filtered_rows:
                if row.get('target_eligible') is False:
                    reason=row.get('filter_reason','rejected')
                    self._filter_reasons[reason]=self._filter_reasons.get(reason,0)+1
            self._frame_age_ms = round((self.clock() - captured_at) * 1000, 1)
            self._poll_hotkeys_locked(live)
            if not self.enabled or self._focus_paused or self.stop_event.is_set() or not live:
                if self._stage not in ('focus_lost','focus_paused', 'manual_unavailable','fire_input_unavailable'):
                    self._stage = 'input_error' if self._input_error else 'disabled'
                return None
            fire_state=self._fire_state(captured_at)
            if fire_state is not None:
                self._pause_for_fire_locked(fire_state)
                return None
            if self._inflight:
                self._stage = 'input_inflight'
                return None
            now = self.clock()
            stale_limit = .045 if self.low_latency else .12 if self.stabilization else .35
            if now - captured_at > stale_limit:
                if self._focus_reacquire_after is not None:self._focus_new_frames=0
                self._target_error = None
                self._target_label = None
                self._aim_source = None
                self._stage = 'stale'
                self._reset_observation_gap('stale')
                self._last_output = [0,0]
                self.last_status = f'当前画面超过 {stale_limit*1000:.0f} 毫秒，跳过定位'
                self._record_control()
                return None
            if rows and not self._eligible_detections:
                if self._focus_reacquire_after is not None:self._focus_new_frames=0
                self._reset_observation_gap('target_rejected')
                self._target_error = self._target_label = self._aim_source = None
                self._last_output = [0,0]
                self._stage = 'target_rejected'
                self.last_status = '目标未通过敌方校验，暂停输出'
                self._record_control()
                return None
            if self.control_algorithm=='lock':
                self.stable.lock_frame_at=captured_at
            if new_frame:
                self.stable.waiting_for_feedback = False
            if not new_frame and self.stabilization and self.stable.waiting_for_feedback:
                self._stage = 'repeat_frame'
                self._last_output = [0,0]
                self.last_status = '等待新游戏画面，不重复修正同一帧'
                self._record_control()
                return None
            if self._focus_reacquire_after is not None:
                if (not new_frame or not math.isfinite(captured_at) or captured_at>now+.005 or
                        captured_at<=self._focus_reacquire_after or
                        (self._focus_last_frame is not None and captured_at<=self._focus_last_frame)):
                    self._stage='focus_reacquiring'
                    self._last_output=[0,0]
                    self.last_status='切回后等待不同的新画面，不使用切出前或重复的画面'
                    self._record_control()
                    return None
                self._focus_last_frame=captured_at
                self._focus_new_frames+=1
            if self._post_fire_reacquire:
                if not new_frame or (self._post_fire_last_frame is not None and
                                     captured_at<=self._post_fire_last_frame):
                    self._stage='fire_reacquiring'
                    self._last_output=[0,0]
                    self.last_status='开火后等待不同的新画面确认目标，不重复使用同一帧'
                    self._record_control()
                    return None
                self._post_fire_last_frame=captured_at
            if self.stabilization and self.mode=='relative':
                self.stable.retain_target=self._hold_target_active()
                target = self.stable.choose(rows,area,confidence,self.aim_height_ratio,self.prefer_head,now,self.gain,
                                            observed_at=captured_at)
                if self.control_algorithm=='lock':
                    # Area changes reset target state inside choose; retain the
                    # processed-frame watermark so that frame cannot confirm twice.
                    self.stable.lock_frame_at=captured_at
            else:
                target = self._select_target(rows, area, confidence, self.aim_height_ratio, self.prefer_head)
            if target is None:
                if self._focus_reacquire_after is not None and self.stable.state!='acquiring':
                    self._focus_new_frames=0
                self._target_error = None
                self._target_label = None
                self._aim_source = None
                self._last_output = [0, 0]
                self._stage = self.stable.state if self.stabilization and self.mode=='relative' else 'no_target'
                self.last_status = ('多个目标难以区分，暂停输出' if self.stable.miss_reason=='ambiguous_target'
                                    else {'acquiring':'正在确认目标，暂不移动',
                                          'target_hold':'原目标短暂丢失，暂停输出并保持目标',
                                          'fire_target_hold':'开火保留原目标：身份未确认，暂停修正，不自动换敌；松键350ms后恢复重选'}.get(self._stage,'当前画面没有可定位的目标'))
                if self.aim_region == 'chest' and not selection_policy(rows, 'chest', self.aim_height_ratio, self.prefer_head)[0]:
                    self.last_status = '未检测到联合验证通过的敌人身体框，胸部定位暂停'
                self._record_control()
                return None
            if self._focus_reacquire_after is not None:
                if self._focus_new_frames<2:
                    self._stage='focus_reacquiring'
                    self._last_output=[0,0]
                    self.last_status='切回后正在确认新目标 · 第一帧不输出'
                    self._record_control()
                    return None
                self._focus_reacquire_after=None
            self._post_fire_reacquire=False
            # A new F8 press also pauses before dispatch if the UI poll has not
            # observed it yet. The same shared edge state prevents double toggles.
            self._poll_hotkeys_locked(live)
            if not self.enabled or self._focus_paused or self.stop_event.is_set():
                return None
            self._aim_source = target['aim_source']
            if self.mode == 'relative':
                px, py = target['point']
                self._target_error = [round(px-area['width']/2, 2),
                                      round(py-area['height']/2, 2)]
                self._target_label = target['row']['label']
                target['error'] = self._target_error
                if self.stabilization:
                    delta = self.stable.command(target,area,now,self.gain,self.step_limit,self.response_ms,self.chase_strength)
                else:
                    delta = relative_step(target, area, self.gain, self.step_limit,
                                          near_slowdown=self.near_slowdown)
                if self.manual_blend is not None:
                    original=delta
                    delta=self.manual_blend.mix(delta,now,fire_down=self._manual_left_down)
                    if delta!=original or (self.manual_fire_priority and any(w<1. for w in self.manual_blend.weights)):
                        # Keep controller state consistent with what was sent;
                        # otherwise reduced output accumulates an unseen push.
                        self.stable.velocity=[v*w for v,w in zip(self.stable.velocity,self.manual_blend.weights)]
                        self.stable.remainder=[v*w for v,w in zip(self.stable.remainder,self.manual_blend.weights)]
                        self.stable.last_delta=delta
                target['delta'] = delta
                if delta == (0, 0):
                    self._last_output = [0, 0]
                    self._stage = ('manual_yield' if self.manual_fire_priority and self.manual_blend is not None and
                                   any(self.manual_blend.manual_yielded) else
                                   'settling' if self.stabilization and not self.stable.centered else 'centered')
                    self.last_status = ('手动输入优先：已让出操作轴，辅助未发送移动' if self._stage=='manual_yield' else
                                        '细调累计不足一个计数，暂不发送' if self._stage=='settling' else
                                        f'已进入 {self.tolerance_px:g}px 容差区，停止细拉 · F8 关闭' if self.soft_assist else
                                        '目标已在中心范围 · F8 关闭')
                    self._record_control()
                    return target
                if not self._dispatch_locked(self.pointer.move_relative, delta, captured_at, stale_limit):
                    return None
                self._last_output = list(delta)
                if self.stabilization:
                    if self.predictive:
                        self.stable.predictor.sent(self.clock(),delta,self.gain)
                    self.stable.waiting_for_feedback = True
                self.last_status = f'准心修正 {delta[0]}, {delta[1]} · 下一帧重新测量 · F8 关闭'
            else:
                if not self._dispatch_locked(self.pointer.move_to, target['screen'], captured_at, stale_limit):
                    return None
                self._last_output = list(target['screen'])
                self.last_status = f"正在定位 {target['row']['label']} · F8 关闭"
            self._stage = 'sent'
            self._sent += 1
            self._record_control()
            return target
