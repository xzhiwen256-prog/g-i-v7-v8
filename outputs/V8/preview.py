"""V8 compact console on the preserved V5 controller/inference implementation."""
from pathlib import Path
import argparse
import json
import os
import uuid
import queue
import shutil
import threading
import time
import traceback
from copy import deepcopy
from collections import deque
from ui_runtime import UiMailbox, MailboxOverflow, BackgroundShutdown, atomic_text, atomic_json

from ui_watchdog import UiHeartbeat
from pipeline_health import PipelineHealth
from ui_settings import UiSettingsMixin, format_lock_model_status, format_fire_lock_status
from aim_region import validate_region
from recognition_diagnostics import format_recognition_report
from strength_costs import parameter_cost_changes
from ui_presenter import format_timing_status
from trigger_control import AutoTrigger
from diagnostic_writer import DiagnosticWriter, status_text as diagnostic_writer_status
import v5_runtime as core
from adaptation import DEFAULT_PROFILE, REFERENCE, calculate, load_profile, profile, save_profile, number
from strength_profiles import (CUSTOM_STRENGTH, get_strength_profile, identify_strength,
                               describe_strength, profile_from_label)

ROOT = Path(__file__).resolve().parent
ENGINEERING_REVISION = 'desktop-freeze-20261107'


class PreviewApp(UiSettingsMixin, core.PreviewApp):
    def __init__(self, root, smoke=False, pointer=None, input_backend='windows', motion_settings_path=None,
                 inference_device='auto', capture_settings_path=None, adaptation_settings_path=None):
        if smoke:
            raise ValueError('V8 has no image-preview smoke mode; use the isolated UI fixture')
        self._closing = False
        self._ensure_runtime()
        self.adaptation_settings_path = (Path(adaptation_settings_path) if adaptation_settings_path is not None
                                         else ROOT / '.settings' / 'adaptation-profile.json')
        self.adaptation_result = None
        self._initial_adaptation_notice = ''
        super().__init__(root, False, pointer, input_backend, motion_settings_path,
                         inference_device, capture_settings_path)
        self.queue = self._messages
        self.follower.configure_motion(mode='relative')
        self._refresh_strength_profile()
        self._ui_watchdog = UiHeartbeat(
            lambda report: atomic_json(self.motion_settings_path.parent / 'ui-hang-snapshot.json', report),
            metadata=dict(version='V8', revision=ENGINEERING_REVISION, process_id=os.getpid()),
            on_stall=self.stop_event.set)
        self._ui_watchdog.start()
        self._ui_heartbeat_job = self.root.after(250, self._pulse_ui_heartbeat)
        self.root.bind('<Destroy>', self._heartbeat_window_destroyed, add='+')

    def _pulse_ui_heartbeat(self):
        if self._destroyed:
            return
        self._ui_watchdog.pulse()
        self._ui_heartbeat_job = self.root.after(250, self._pulse_ui_heartbeat)

    def _heartbeat_window_destroyed(self, event):
        if event.widget is self.root:
            self._ui_watchdog.stop()

    def _ensure_runtime(self):
        # Also supports the headless __new__ fixtures without constructing Tk.
        if not hasattr(self, '_messages'):
            self._messages = UiMailbox()
            self._run_generation = 0
            self._worker_local = threading.local()
            self._run_error = None
            self._stopping = False
            self._shutdown = None
            self._shutdown_poll_job = None
            self._destroyed = False
        self.queue = self._messages
        if not hasattr(self, 'enemy_color'):
            self.enemy_color = 'red'
        if not hasattr(self, '_enemy_color_load_error'):
            self._enemy_color_load_error = ''
        if not hasattr(self, '_trigger'):
            self._trigger = AutoTrigger()
        if not hasattr(self, '_follow_ui_events'):
            self._follow_ui_events = deque(maxlen=32)
            self._follow_ui_signature = None
            self._follow_ui_message = ''
        if not hasattr(self, '_pipeline_health'):
            self._pipeline_health = PipelineHealth()
        if not hasattr(self, '_diagnostic_writer'):
            self._diagnostic_writer = DiagnosticWriter()
            self._diagnostic_submit_error = None

    def post(self, message, generation=None):
        self._ensure_runtime()
        if generation is None:
            generation = getattr(self._worker_local, 'generation', self._run_generation)
        try:
            return self._messages.put_nowait(message, generation)
        except MailboxOverflow:
            # Visible fault + fail-stop, without waiting on a controller/device lock.
            self.stop_event.set()
            return False

    def toggle_follow(self):
        self._ensure_runtime()
        self._follow_ui_events.append(dict(time=time.monotonic(), kind='checkbox_request',
                                           requested=bool(self.follow_var.get())))
        if self._closing or getattr(self, '_stopping', False):
            self.follow_var.set(False)
            return
        return super().toggle_follow()

    def toggle_trigger(self):
        self._ensure_runtime()
        enabled=self.trigger_var.get() is True
        unsupported=self._closing or not getattr(self.pointer,'supports_trigger',False)
        if unsupported:
            enabled=False
            self.trigger_status.set('不可开启：只支持虚拟 HID；不会切换后端')
        self._trigger.set_enabled(enabled)
        if not enabled:self._cancel_trigger_input()
        if unsupported:
            with self._trigger.lock:self._trigger.status='不可开启：只支持虚拟 HID；不会切换后端'
        self.trigger_var.set(enabled)

    def disarm(self):
        self._ensure_runtime()
        self._trigger.set_enabled(False)
        self._cancel_trigger_input()
        return super().disarm()

    def _cancel_trigger_input(self):
        cancel=getattr(getattr(self,'pointer',None),'cancel_trigger',None)
        if cancel is not None:cancel()  # Event only; no native/pipe wait on UI.

    def _trigger_gate(self):
        state=self.follower.snapshot()
        pending=(state.get('prediction') or {}).get('pending_camera_pixels',[0,0])
        return (not self._closing and not self.stop_event.is_set() and self.live_mode
                and state.get('enabled') is True and state.get('stage')=='centered'
                and not state.get('focus_paused') and not state.get('focus_reacquiring')
                and not state.get('input_inflight') and all(abs(v)<=1 for v in pending)
                and self.follower._foreground_allowed() is True)

    def _trigger_click(self, guard, expires_at):
        with self.follower.lock:
            generation=self.follower._generation
        submitted=self.pointer.trigger_click(guard,expires_at)
        if submitted is True:
            self.follower.note_trigger_shot(generation)
        return submitted

    def _trigger_physical_down(self):
        monitor=getattr(self,'manual_monitor',None)
        state=monitor.snapshot() if monitor is not None else {}
        if state.get('available') is not True or state.get('error') or state.get('dropped_reports')!=0:
            raise OSError('物理鼠标监测不可用或丢帧')
        held=state.get('left_button_down')
        if type(held) is not bool:raise OSError('物理左键状态未知')
        return held

    def _sync_follow_state(self, running, now=None):
        """Keep a UI arm request pending until an enable actually succeeds.

        Both foreground checks remain fail-closed. A transient loss between
        precheck and set_enabled must not silently consume the one-shot request.
        """
        self._ensure_runtime()
        now = time.monotonic() if now is None else now
        deadline = getattr(self, '_follow_arm_deadline', None)
        state = self.follower.snapshot()
        if deadline is not None:
            if not self.live_mode or not running or self.stop_event.is_set():
                self.disarm()
                self.follower.last_status = '待开启已取消：实时识别已停止，请先开始识别'
            elif state['f8_presses'] != self._follow_arm_f8_presses or state['enabled']:
                # A hotkey decision is authoritative, including an explicit OFF.
                self._follow_arm_deadline = None
            elif now >= deadline:
                self.disarm()
                self.follower.last_status = '待开启已超时：未能确认游戏前台；请在游戏内按一次 F8，或重新勾选后 5 秒内切回'
            elif self.follower._foreground_allowed():
                self.follower.set_enabled(True)
                state = self.follower.snapshot()
                if state['enabled']:
                    self._follow_arm_deadline = None
                elif state['stage'] in ('manual_unavailable', 'input_error', 'fire_input_unavailable'):
                    # Device/monitor faults require another explicit request.
                    self._follow_arm_deadline = None
                # Else keep the request within its original deadline. No output
                # is allowed before set_enabled's own foreground check succeeds.
        state = self.follower.snapshot()
        pending = self._follow_arm_deadline is not None
        if pending:
            self.follow_var.set(True)
            message = (f'跟随待开启：{max(1, int(self._follow_arm_deadline-now+.999))} 秒内切回游戏；'
                       '正在确认前台/输入就绪，再点取消，尚未输出')
        else:
            self.follow_var.set(state['enabled'])
            message = self.follower.last_status
        self._follow_ui_message = message
        self.control_status.set(message)
        signature = (state['enabled'], pending, state['focus_paused'], state['f8_presses'],
                     message if not state['enabled'] and not pending else '')
        if signature != self._follow_ui_signature:
            self._follow_ui_signature = signature
            self._follow_ui_events.append(dict(time=now, kind='state', enabled=state['enabled'],
                pending=pending, stage=state['stage'], status=message,
                f8_presses=state['f8_presses'], last_f8_result=state['last_f8_result']))

    def _motion_document(self):
        state = self.follower.snapshot()
        state['enemy_filter'] = self.enemy_filter
        state['enemy_color'] = getattr(self, 'enemy_color', 'red')
        names = ('aim_region','gain','step_limit','aim_height_ratio','response_ms','chase_strength','predictive',
                 'enemy_filter','enemy_color','soft_assist','tolerance_px','pause_on_fire','fire_release_ms',
                 'control_algorithm','pd_kp','pd_kd','manual_fire_priority','fire_target_lock')
        return dict({key: state[key] for key in names}, paced_corrections=self.follower.pacing_enabled)

    def _worker_error(self, detail, generation):
        # All work here is Tk-free. A late obsolete worker cannot stop a new run.
        if generation != self._run_generation:
            return
        notice = detail.splitlines()[-1] if detail.splitlines() else 'Unknown worker error'
        self.stop_event.set()
        self._trigger.set_enabled(False)
        self._cancel_trigger_input()
        try:
            self.follower.set_enabled(False)
        except Exception:
            detail += '\nAdditional disarm error:\n' + traceback.format_exc()
        self.post(('error', notice), generation)
        try:
            atomic_text(ROOT / 'preview-error.log', detail)
        except OSError:
            pass  # Keep the original error visible even if diagnostic writing fails.

    def run(self, image_path, index, generation=None):
        self._ensure_runtime()
        generation = self._run_generation if generation is None else generation
        self._worker_local.generation = generation
        try:
            self._run_live(image_path, index)
        except Exception:
            self._worker_error(traceback.format_exc(), generation)
        finally:
            try:
                if generation == self._run_generation:
                    self.stop_event.set()
                    self._trigger.set_enabled(False)
                    self._cancel_trigger_input()
                    self.follower.set_enabled(False)
            except Exception:
                self._worker_error(traceback.format_exc(), generation)
            finally:
                self.post(('stopped',), generation)
                del self._worker_local.generation

    def monitor_width(self):
        from mss import mss
        index = self.monitor.current() + 1 if hasattr(self, 'monitor') else 1
        with mss() as source:
            if not 1 <= index < len(source.monitors):
                raise ValueError('无法确认显示器，请重新选择显示器')
            return source.monitors[index]['width']

    def prepare_adaptation(self):
        self.adaptation_inputs, notice = load_profile(self.adaptation_settings_path)
        self._initial_adaptation_notice = notice or ''
        self._base_strength_level = self.adaptation_inputs.get('basis_level')
        self._adaptation_needs_save = notice is not None or not self.adaptation_settings_path.exists()
        try:
            width = self.monitor_width()
        except (OSError, ValueError, IndexError):
            width = REFERENCE.width
            self._initial_adaptation_notice += ' 显示器信息暂不可用，开始前需重新应用。'
        try:
            result = calculate(self.adaptation_inputs['dpi'], self.adaptation_inputs['sensitivity'], width,
                               self.adaptation_inputs['base_limit'])
        except ValueError:
            self.adaptation_inputs = dict(DEFAULT_PROFILE)
            result = calculate(800, .3, width)
            self._initial_adaptation_notice = '当前组合超出增益范围，已恢复默认值；跟随仍默认关闭。'
            self._adaptation_needs_save = True
        self.adaptation_result = result
        self.follower.configure_motion(gain=self.adaptation_inputs.get('manual_gain', result.gain), step_limit=result.step_limit)
        try:
            saved = json.loads(self.motion_settings_path.read_text('utf-8'))
            enabled = saved.get('paced_corrections', False)
            if type(enabled) is not bool:
                raise ValueError('Invalid rhythm flag')
        except FileNotFoundError:
            enabled = False
        except (OSError, ValueError, TypeError, AttributeError):
            enabled = False
            self._initial_adaptation_notice += ' 节奏配置无效，已保留原节奏。'
        self.follower.set_pacing_enabled(enabled)
        try:
            saved_region = json.loads(self.motion_settings_path.read_text('utf-8')).get('aim_region', 'original')
            region = validate_region(saved_region)
        except FileNotFoundError:
            region = 'original'
        except (OSError, ValueError, TypeError, AttributeError):
            region = 'original'
            self._initial_adaptation_notice += ' 定位部位配置无效，已恢复原定位；跟随仍关闭。'
        self.follower.configure_motion(aim_region=region)
        # Confidence is an optional UI setting; older capture profiles remain valid.
        if hasattr(self, 'capture_settings_path'):
            try:
                capture_profile = json.loads(self.capture_settings_path.read_text('utf-8'))
                if 'confidence' in capture_profile:
                    self.confidence = number(capture_profile['confidence'], '识别置信度', .1, .95)
            except (OSError, ValueError, TypeError, KeyError):
                pass

    def refresh_adaptation_readout(self):
        r = self.adaptation_result
        if r is None:
            return
        self.adapt_summary.set(f'eDPI {r.edpi:g}  ·  自动建议 {r.gain:.3f}\n生效增益 {self.follower.gain:.3f}  ·  实际上限 {r.step_limit} 计数')
        self.adapt_error.set(self._initial_adaptation_notice or r.warning)

    def apply_adaptation(self, base_limit=None, motion=None, *, manual_gain=..., basis_level=None, sync_ui=True):
        if self._closing:
            return False
        try:
            desired = profile(self.dpi_var.get(), self.sensitivity_var.get(),
                              self.adaptation_inputs['base_limit'] if base_limit is None else base_limit)
            selected_manual = self.adaptation_inputs.get('manual_gain') if manual_gain is ... else manual_gain
            if selected_manual is not None:
                desired['manual_gain'] = number(selected_manual, '手动位移增益', .05, 8)
            old_state = self.follower.snapshot()
            old_state['step_limit'] = self.adaptation_inputs['base_limit']
            basis = basis_level if basis_level is not None else (getattr(self, '_base_strength_level', None)
                    or self.adaptation_inputs.get('basis_level') or identify_strength(old_state))
            if basis is not None:
                desired['basis_level'] = number(basis, '基准档位', 1, 6, True)
            result = calculate(desired['dpi'], desired['sensitivity'], self.monitor_width(), desired['base_limit'])
        except (OSError, ValueError, IndexError) as exc:
            self.disarm()
            self.adapt_error.set(str(exc))
            return False
        before = self.follower.snapshot()
        motion_changed = any(before.get(k) != v for k, v in (motion or {}).items() if k != 'step_limit')
        actual_gain = desired.get('manual_gain', result.gain)
        changed = (result != self.adaptation_result or desired != self.adaptation_inputs or motion_changed
                   or before['gain'] != actual_gain or before['step_limit'] != result.step_limit)
        if changed:
            # Commit all validated values together. Never multiply the last output again.
            self.disarm()
            values = dict(motion or {})
            values.update(gain=actual_gain, step_limit=result.step_limit)
            costs = parameter_cost_changes(before, values)
            self._last_strength_change_notice = '\n'.join(costs) if costs else '未检测到本次参数的单向降力度项；仍需注意已启用选项的代价。'
            self.follower.configure_motion(**values)
            self.adaptation_inputs = desired
            self.adaptation_result = result
            self._base_strength_level = basis
        self._initial_adaptation_notice = ''
        self.dpi_var.set(str(desired['dpi']))
        self.sensitivity_var.set(f"{desired['sensitivity']:g}")
        self.refresh_adaptation_readout()
        self._refresh_strength_profile()
        if changed or self._adaptation_needs_save:
            try:
                save_profile(self.adaptation_settings_path, desired)
                self._adaptation_needs_save = False
                self.schedule_motion_save()
                self.motion_save_status.set('适配已应用并保存 · 跟随需手动重新开启' if changed else '适配参数已保存')
            except (OSError, ValueError):
                self._adaptation_needs_save = True
                self.adapt_error.set('本次参数已应用，但保存失败；请检查目录是否可写。')
        if sync_ui:
            self.sync_ui_drafts()
        return True

    def commit_adaptation(self, event=None):
        if hasattr(self, 'ui_fields'):
            return self.commit_ui_parameters(event)
        ok = self.apply_adaptation()
        return 'break' if event is not None else ok

    def change_monitor(self, event=None):
        if hasattr(self, 'ui_fields'):
            self.mark_ui_draft()
            return
        return self.commit_adaptation(event)

    def _refresh_strength_profile(self):
        if not hasattr(self, 'strength_choice') or self.adaptation_result is None:
            return
        state = self.follower.snapshot()
        # Preset identity uses the unscaled budget, not its sensitivity-adjusted result.
        state['step_limit'] = self.adaptation_inputs['base_limit']
        level = identify_strength(state)
        if level is not None:
            self._base_strength_level = level
        basis = getattr(self, '_base_strength_level', None)
        self.strength_choice.set(get_strength_profile(level).label if level is not None else CUSTOM_STRENGTH)
        self.strength_description.set(get_strength_profile(level).capability if level is not None else
                                      (f'基于{get_strength_profile(basis).name} · 已微调' if basis else '手动参数 · 未自动套用任何预设'))
        if hasattr(self, 'strength_badge'):
            self.strength_badge.set('预设参数' if level is not None else '已微调')
            for item, button in self.strength_buttons.items():
                button.configure(style='Selected.TButton' if item == (level or basis) else 'Chip.TButton')
            self.restore_strength_button.configure(state='normal' if basis else 'disabled')

    def apply_strength_level(self, level):
        p = get_strength_profile(level)
        ok = self.apply_adaptation(base_limit=p.step_limit, motion=p.motion(), basis_level=level)
        if ok:
            self.disarm()  # An explicit preset click always pauses, even on the same preset.
            if hasattr(self, 'ui_commit_status'):
                self.ui_commit_status.set(f'已应用 {p.label} · 跟随已暂停；其他未应用调参草稿已重置')
        return ok

    def change_strength_level(self, event=None):
        try:
            p = profile_from_label(self.strength_choice.get())
        except ValueError:
            p = None
        if p is not None:
            self.apply_strength_level(p.level)
        self._refresh_strength_profile()

    def toggle_partition_timing(self):
        if self._closing:
            return
        desired = self.partition_timing_var.get()
        try:
            self.follower.set_partition_timing_enabled(desired)
        except ValueError as exc:
            self.partition_timing_var.set(self.follower.partition_timing.enabled)
            self.partition_timing_status.set('诊断开关未改变：' + str(exc))
            return
        self.partition_timing_status.set(
            '已开启，仅记录后续小修正的分片；不会开启跟随或均匀细调，可能增加处理开销' if desired else
            '已关闭，不再开始新记录；进行中的记录可能完成，已保存历史不会自动删除')

    def change_rhythm(self):
        if self._closing:
            return False
        desired = self.rhythm_var.get()
        self.disarm()
        try:
            self.follower.set_pacing_enabled(desired)
        except ValueError as exc:
            self.rhythm_var.set(self.follower.pacing_enabled)
            self.motion_save_status.set(str(exc))
            return False
        self.schedule_motion_save()
        self.motion_save_status.set('修正节奏已切换 · 增益和位移预算未变 · 跟随需重新开启')
        return True

    def save_motion_profile(self):
        if self._motion_save_job is not None:
            self.root.after_cancel(self._motion_save_job)
            self._motion_save_job = None
        try:
            atomic_json(self.motion_settings_path, self._motion_document())
            self.motion_save_status.set('V8 参数已保存')
            return True
        except OSError:
            self.motion_save_status.set('本次参数已应用，但配置保存失败')
            return False

    def write_diagnostic(self, state=None, running=False):
        if self._closing:
            return False
        state = dict(self.follower.snapshot() if state is None else state)
        state['correction_rhythm'] = self.follower.pacing_snapshot()
        # Reuse this detached 1 Hz snapshot; no extra controller polling for UI.
        self._timing_rhythm_snapshot = state['correction_rhythm']
        state['auto_trigger'] = self._trigger.snapshot()
        self._ensure_runtime()
        state['ui_mailbox'] = self._messages.snapshot()
        deadline = getattr(self, '_follow_arm_deadline', None)
        state['follow_ui'] = dict(revision='follow-ui-state-20261108',
            pending=deadline is not None,
            pending_seconds=max(0., deadline-time.monotonic()) if deadline is not None else 0.,
            status=self._follow_ui_message, events=list(self._follow_ui_events))
        state['engineering_revision'] = ENGINEERING_REVISION
        state['pipeline_health'] = self._pipeline_health.snapshot()
        watchdog = getattr(self, '_ui_watchdog', None)
        if watchdog is not None:
            state['ui_heartbeat'] = watchdog.snapshot()
        # Capture UI-owned metadata here, without passing Tk variables/callbacks
        # to the writer. These small mappings are detached; published control
        # trace records are append-only and remain read-only after publication.
        capture_info = deepcopy(self.capture_info)
        data = deepcopy(dict(state,
            recorded_unix=time.time(), process_id=os.getpid(), session_id=self.session_id,
            version='V8', hotkeys=dict(toggle_follow='F8', escape_action='none'),
            live_max_fps=self.live_fps_limit, enemy_filter=self.enemy_filter, friend_guard=getattr(self,'friend_guard',False),
            enemy_color=getattr(self, 'enemy_color', 'red'),
            target_filter=dict(self.target_filter_report), capture_scope=self.capture_scope,
            inference_size=self.inference_size, capture_area=self.current_capture_area,
            capture_backend=capture_info['backend'], capture_backend_info=capture_info,
            inference=(dict(self.detector.info) if self.detector is not None else None),
            performance=dict(self.performance),
            latency_window=self.latency.snapshot(), game_calibration=core.GAME_CALIBRATION,
            is_admin=self.is_admin, running=bool(running), live_mode=self.live_mode,
            capture_black=self.capture_black))
        writer_state = self._diagnostic_writer.snapshot()
        data['diagnostic_writer'] = writer_state
        data['diagnostic_sequence'] = writer_state['submitted'] + 1
        trace = self.follower.trace_snapshot()
        try:
            accepted = self._diagnostic_writer.submit((data, trace, ROOT))
        except RuntimeError as exc:
            self._diagnostic_submit_error = str(exc)
            return False
        self._diagnostic_submit_error = None
        return accepted

    def begin(self, image_path=None):
        if image_path is not None:
            raise ValueError('V8 简洁版不提供图片/视频预览入口')
        self._ensure_runtime()
        if getattr(self,'_startup_input_error',None):
            self.stop_event.set();self.disarm()
            self.status.set('尚未开始：输入未就绪；请关闭占用程序后重新启动V8')
            return False
        if self._closing or self._stopping or (self.worker is not None and self.worker.is_alive()):
            return False
        if not (self.commit_ui_parameters() if hasattr(self, 'ui_fields') else self.apply_adaptation()):
            self.status.set('尚未开始：请先修正参数适配区的提示')
            return False
        self._run_generation += 1
        self._messages.reset(self._run_generation)
        self._run_error = None
        self.live_mode = True
        self.stop_event.clear()
        self.monitor.configure(state='disabled')
        self.start_button.configure(state='disabled')
        self.status.set('正在加载模型…' if self.detector is None else '正在识别…')
        index = self.monitor.current() + 1
        self.worker = threading.Thread(target=self.run, args=(None, index, self._run_generation), daemon=True)
        try:
            self.worker.start()
        except Exception:
            self.stop_event.set()
            self.worker = None
            self.live_mode = False
            self.monitor.configure(state='readonly')
            self.start_button.configure(state='normal')
            self._run_error = '无法启动识别线程'
            self.status.set('识别失败：' + self._run_error)
            return False
        return True

    def _run_live(self, image_path, index):
        """V5 recognition/control path, without a renderer or image UI mailbox."""
        source = None
        try:
            from mss import mss
            import numpy as np
            if self.detector is None:
                self.detector = core.Detector(device=self.inference_device)
            if hasattr(self.pointer, 'check_backend'):
                try:
                    self.post(('backend_ready', self.pointer.check_backend()))
                except OSError as exc:
                    self.follower.note_input_error(exc)
                    self.post(('backend_ready', f'连接失败：{exc}'))
            with mss() as sct:
                self.current_capture_area = core.capture_area(sct, index, self.capture_scope)
                monitor = {key: sct.monitors[index][key] for key in ('left', 'top', 'width', 'height')}
                source = core.LatestScreenSource(monitor, self.current_capture_area,
                                lambda: core.capture(sct, index, with_area=True, scope=self.capture_scope))
                self.capture_info = dict(source.info)
                warm_image = None
                deadline = time.perf_counter() + 5
                while warm_image is None and not self.stop_event.is_set():
                    warm_image, _ = source.grab()
                    if time.perf_counter() > deadline:
                        raise RuntimeError('等待首个截图超时')
                if warm_image is None:
                    return
                self.detector.warm(warm_image, self.confidence, input_size=self.inference_size)
                next_stats = 0.
                previous_filter_settings = None
                cached_rows = None
                fresh_times = deque(maxlen=120)
                previous_frame_id = previous_image = last_target = previous_area = previous_confidence = None
                timing_loop_id = 0
                while not self.stop_event.is_set():
                    start = time.perf_counter()
                    image, area = source.grab()
                    if image is None:
                        self.follower.poll_hotkeys(live=True)
                        continue
                    self.capture_info = dict(source.info)
                    self.current_capture_area = area
                    captured = time.perf_counter()
                    observed_at = source.observed_at
                    if source.frame_id is not None:
                        new_frame = source.frame_id != previous_frame_id
                        previous_frame_id = source.frame_id
                    else:
                        new_frame = previous_image is None or area != previous_area or not np.array_equal(image, previous_image)
                    inference_performed = new_frame or self.confidence != previous_confidence
                    if inference_performed:
                        raw_rows, ms = self.detector.infer(image, self.confidence, input_size=self.inference_size)
                        previous_image, previous_area, previous_confidence = image, area, self.confidence
                    else:
                        ms = 0.
                    inferred = time.perf_counter()
                    filter_settings = (self.confidence, self.enemy_filter, getattr(self, 'enemy_color', 'red'),getattr(self,'friend_guard',False))
                    filter_reused = not new_frame and cached_rows is not None and filter_settings == previous_filter_settings
                    if not filter_reused:
                        cached_rows, self.target_filter_report = core.filter_targets(image, raw_rows, self.confidence, strict=self.enemy_filter,
                            enemy_color=filter_settings[2],friend_guard=filter_settings[3])
                        previous_filter_settings = filter_settings
                    rows = cached_rows
                    filtered = time.perf_counter()
                    try:
                        if self.follower.partition_timing.enabled:
                            timing_loop_id += 1
                            self.follower.note_partition_timing_context(dict(loop_id=timing_loop_id,
                                source_frame_id=source.frame_id,new_frame=new_frame,observed_at=observed_at,
                                capture_started_at=start,capture_completed_at=captured,
                                inference_completed_at=inferred,filter_completed_at=filtered))
                        target = self.follower.update(rows, area, observed_at, self.confidence, new_frame=new_frame)
                        if new_frame or target is not None:
                            last_target = target
                        elif self.follower.enabled and self.follower.snapshot()['stage'] == 'repeat_frame':
                            target = last_target
                    except OSError:
                        target = None
                        try:
                            atomic_text(ROOT / 'input-error.log', traceback.format_exc())
                        except OSError:
                            self.post(('backend_ready', '输入已停止，错误日志保存失败'))
                    controlled = time.perf_counter()
                    if self._trigger.enabled:
                        self._trigger.update(rows,area,observed_at,self.confidence,self.follower.aim_region,new_frame,
                            self._trigger_gate,self._trigger_physical_down,self._trigger_click)
                        if self._trigger.snapshot()['error']:
                            raise OSError('自动扳机保护停止：'+self._trigger.snapshot()['error'])
                    trigger_done=time.perf_counter()
                    elapsed = trigger_done - start
                    self.live_fps_limit = core.LIVE_MAX_FPS if self.detector.info.get('backend') == 'tensorrt' else 60
                    core.wait_for_frame(self.stop_event, start + 1 / self.live_fps_limit)
                    fps = 1 / max(time.perf_counter() - start, .001)
                    if new_frame:
                        fresh_times.append(captured)
                    fresh_fps = ((len(fresh_times)-1) / (fresh_times[-1]-fresh_times[0])) if len(fresh_times) > 1 else 0.
                    control_state = self.follower.pipeline_snapshot()
                    self.performance = dict(capture_ms=round((captured-start)*1000, 2), inference_ms=round(ms, 2),
                        trigger_ms=round((trigger_done-controlled)*1000,3),
                        filter_ms=round((filtered-inferred)*1000, 2), control_ms=round((controlled-filtered)*1000, 2),
                        process_ms=round(elapsed*1000, 2), capture_width=area['width'], capture_height=area['height'],
                        input_size=self.inference_size, new_frame=new_frame, loop_fps=round(fps, 2), render_ms=0.,
                        capture_to_submit_ms=round((controlled-start)*1000, 3),
                        pacing_ms=round((time.perf_counter()-trigger_done)*1000, 3), loop_ms=round(1000/fps, 3),
                        inference_reused=not inference_performed,
                        inference_path=(getattr(self.detector, 'info', {}).get('last_path') or
                                        getattr(self.detector, 'info', {}).get('backend', 'unknown')) if inference_performed else 'cached',
                        filter_reused=filter_reused, submitted=control_state['stage']=='sent',
                        inference_stages=dict(getattr(self.detector, 'timings', {})),
                        frame_age_at_capture_ms=round(max(0., captured-observed_at)*1000, 3),
                        frame_to_submit_ms=round(max(0., controlled-observed_at)*1000, 3), fresh_frame_fps=round(fresh_fps, 2),
                        timestamp_source=self.capture_info.get('timestamp_source'), renderer_error=None)
                    self.latency.add(self.performance)
                    self._pipeline_health.observe(control_state, self.performance)
                    if controlled >= next_stats:
                        sample = image[::16, ::16]
                        self.capture_black = bool(sample.max() < 8 and sample.mean() < 1)
                        self.post(('stats', len(rows), ms, fresh_fps))
                        next_stats = controlled + .1
        except Exception:
            self._worker_error(traceback.format_exc(), self._worker_local.generation)
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:
                    self._worker_error(traceback.format_exc(), self._worker_local.generation)

    def poll_hotkeys(self):
        if self._closing:
            return
        running = self.worker is not None and self.worker.is_alive()
        self.follower.poll_hotkeys(live=self.live_mode and running and not self.stop_event.is_set())
        if self.stop_event.is_set() and self.follower.enabled:
            self.disarm()
        self._sync_follow_state(running)
        state=self.follower.snapshot()
        hold_allowed=(self._trigger.enabled and state.get('enabled') is True and not state.get('focus_paused')
                      and not state.get('focus_reacquiring') and not self.stop_event.is_set())
        if not hold_allowed and getattr(self,'_trigger_hold_allowed',True):self._cancel_trigger_input()
        self._trigger_hold_allowed=hold_allowed
        error_reader=getattr(self.pointer,'trigger_error',None)
        background_error=error_reader() if error_reader is not None else None
        if background_error:
            self.stop_event.set();self._trigger.set_enabled(False);self._cancel_trigger_input()
            with self._trigger.lock:self._trigger.error=background_error;self._trigger.status='后台输入故障 · 已关闭'
        if hasattr(self,'trigger_var'):
            trigger=self._trigger.snapshot()
            self.trigger_var.set(trigger['enabled'])
            self.trigger_status.set(f"{trigger['status']} · 已提交按下 {trigger['shots']} 次" +
                (' · '+trigger['error'] if trigger['error'] else ''))
        state = self.follower.snapshot()
        writer_note = diagnostic_writer_status(self._diagnostic_writer.snapshot())
        if self._diagnostic_submit_error:
            writer_note += '【诊断线程未启动：' + self._diagnostic_submit_error + '】'
        if hasattr(self,'prediction_status'):
            self.prediction_status.set(format_lock_model_status(state))
        if hasattr(self,'fire_lock_status'):
            self.fire_lock_status.set(format_fire_lock_status(state))
        self.diagnostic_status.set(f"V8 · {state['stage']} · {state['detections']} 个目标 · 输出 {state['sent']} 次\n" + writer_note)
        if time.monotonic() >= self._next_diagnostic_write:
            self.write_diagnostic(state, running)
            self._next_diagnostic_write = time.monotonic() + 1
        # Keep the complete header owned by the read-only workflow presenter.
        refresh_status = getattr(self, 'ui_refresh_status', None)
        if refresh_status is not None:
            refresh_status()
        self.root.after(20, self.poll_hotkeys)

    def poll(self):
        if self._closing:
            return
        self._ensure_runtime()
        # Bound one UI turn even if producers keep publishing events.
        for _ in range(32):
            try:
                item = self._messages.get_nowait()
            except queue.Empty:
                break
            if item[0] == 'backend_ready':
                self.backend_status.set(item[1])
            elif item[0] == 'error':
                self._run_error = self._run_error or item[1]
                self.status.set('识别失败：' + self._run_error + '；详情见 preview-error.log')
            elif item[0] == 'stats':
                _, count, ms, fps = item
                self.processed += 1
                if self._run_error is None and not self._stopping and not self.stop_event.is_set():
                    self.status.set(f'检测到 {count} 个目标 · 推理 {ms:.0f} ms · 新帧 {fps:.1f} 帧/秒')
                info = getattr(self.detector, 'info', {})
                device = info.get('gpu_name') or info.get('device', '未知')
                capture = getattr(self, 'capture_info', {})
                perf = getattr(self, 'performance', {})
                capture_backend = capture.get('backend', 'pending')
                details = (f"{device} · 采集 {capture_backend} · 推理 {info.get('backend', 'pytorch')} · 无预览渲染 · "
                           + ('均匀细调（实验）' if self.follower.pacing_enabled else 'V5 原节奏'))
                if 'capture_ms' in perf:
                    details += f"\n本次采集 {perf['capture_ms']:.1f} ms · 推理 {ms:.1f} ms · 新帧 {fps:.1f} 帧/秒"
                if capture.get('fallback_reason'):
                    details += '\n采集降级原因：' + str(capture['fallback_reason'])
                if info.get('fallback_reason'):
                    details += '\n推理回退原因：' + str(info['fallback_reason'])
                details += '\n' + format_timing_status(
                    perf, getattr(self, '_timing_rhythm_snapshot', None))
                self.performance_status.set(details)
                if hasattr(self, 'target_filter_status'):
                    self.target_filter_status.set(format_recognition_report(
                        getattr(self, 'target_filter_report', {}),
                        getattr(self.follower, 'aim_region', 'original')))
            elif item[0] == 'input_diagnostic':
                self.input_diagnostic_status.set(item[1])
            elif item[0] == 'stopped':
                self.live_mode = False
        if self.worker is not None and not self.worker.is_alive():
            self.live_mode = False
            self._stopping = False
            self.monitor.configure(state='readonly')
            self.start_button.configure(state='normal')
            if self._run_error is not None:
                self.status.set('识别失败：' + self._run_error + '；详情见 preview-error.log')
            elif self.stop_event.is_set():
                self.status.set('已停止识别')
        self.root.after(80, self.poll)

    def stop(self):
        if self._closing:
            return
        self._ensure_runtime()
        self.stop_event.set()
        self.disarm()
        running = self.worker is not None and self.worker.is_alive()
        self._stopping = running
        if running:
            self.start_button.configure(state='disabled')
        self.status.set('已请求停止，正在退出后台…' if running else '已停止识别')

    def redraw(self):
        # Compatibility entry point deliberately does not import cv2/Pillow/Tk images.
        return None

    def close(self):
        self._ensure_runtime()
        if self._closing and self._shutdown is not None:
            if self._shutdown.thread is None:
                self._start_shutdown()
            return
        self._closing = True
        self._trigger.set_enabled(False)
        self._cancel_trigger_input()
        self.stop_event.set()  # Immediate gate for all unsent input, before waiting.
        self._follow_arm_deadline = None
        self.follow_var.set(False)
        self.live_mode = False
        self._messages.close()
        pending_save = self._motion_save_job is not None
        if pending_save:
            self.root.after_cancel(self._motion_save_job)
            self._motion_save_job = None
        self.start_button.configure(state='disabled')
        self.status.set('正在安全关闭，等待后台退出…')
        workers = [getattr(self, name, None) for name in
                   ('worker', '_input_diagnostic_worker', '_connection_check')]
        tasks = []
        tasks.append(('closing_diagnostics', self._diagnostic_writer.close))
        if pending_save:
            tasks.append(('saving_settings', lambda: atomic_json(self.motion_settings_path, self._motion_document())))
        if hasattr(self.pointer, 'close'):
            tasks.append(('closing_pointer', self.pointer.close))
        monitor = getattr(self, 'manual_monitor', None)
        if monitor is not None:
            tasks.append(('closing_monitor', monitor.close))
        self._shutdown = BackgroundShutdown(lambda: self.follower.set_enabled(False), workers, tasks,
                                             finalizer=self._record_shutdown_errors)
        self._start_shutdown()

    def _start_shutdown(self):
        try:
            self._shutdown.start()
        except RuntimeError:
            self.status.set('无法创建关闭线程；已停止输入，请再次点击关闭重试')
            return
        if self._shutdown_poll_job is None:
            self._shutdown_poll_job = self.root.after(25, self._poll_shutdown)

    def _record_shutdown_errors(self):
        state = self._shutdown.snapshot()
        if state['errors']:
            atomic_json(self.motion_settings_path.parent / 'shutdown-error.json', state)

    def _poll_shutdown(self):
        self._shutdown_poll_job = None
        if self._destroyed:
            return
        state = self._shutdown.snapshot()
        if state['done']:
            self._destroyed = True
            self.root.destroy()
            return
        if state['elapsed_seconds'] >= 5. and getattr(self, '_shutdown_display_phase', None) != state['phase']:
            names = dict(waiting_for_worker='识别/检查线程', disarming='关闭跟随', saving_settings='保存配置',
                         closing_pointer='关闭输入连接', closing_monitor='关闭输入监控',
                         closing_diagnostics='写完最后一份诊断', recording_errors='记录异常')
            self.status.set('后台仍在退出：' + names.get(state['phase'], state['phase']) + '；未强制释放使用中的设备')
            self._shutdown_display_phase = state['phase']
        self._shutdown_poll_job = self.root.after(25, self._poll_shutdown)


def main():
    parser = argparse.ArgumentParser(description='V8 compact console')
    parser.add_argument('--input-backend', choices=['windows', 'logitech', 'virtualhid'], default='virtualhid')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    parser.add_argument('--input-unavailable',action='store_true',help='Open settings UI without any native input device')
    args = parser.parse_args()
    core.enable_physical_coordinates()
    import tkinter as tk
    root = tk.Tk()
    if args.input_unavailable:
        from startup_recovery import UnavailablePointer,load_startup_error,show_recovery
        error=load_startup_error(ROOT)
        app=PreviewApp(root,pointer=UnavailablePointer(error),input_backend='virtualhid',inference_device=args.device)
        show_recovery(app,error)
        root.title('V8 · 输入未就绪 · 设置查看模式')
    else:
        PreviewApp(root, input_backend=args.input_backend, inference_device=args.device)
    root.mainloop()


if __name__ == '__main__':
    main()
