"""V7 draft/commit presentation logic. No devices, capture or inference imports."""
from decimal import Decimal
import math
from numeric_fields import NumberSpec, SPECS, parse_value, format_value
from strength_profiles import get_strength_profile, CUSTOM_STRENGTH
from ui_runtime import atomic_json
from aim_region import AIM_REGIONS
from outline_settings import OUTLINE_COLORS, normalize_enemy_color
from strength_costs import parameter_cost_changes, active_costs


FIELDS = {
    'gain': ('位移增益', NumberSpec(.05, 8, 6, .01)),
    'response_ms': ('修正响应', SPECS['response_slider']),
    'chase_strength': ('追赶倍率', SPECS['chase_slider']),
    'base_limit': ('基础位移上限', SPECS['limit_slider']),
    'tolerance_px': ('允许偏差', SPECS['tolerance_slider']),
    'confidence': ('识别置信度', SPECS['confidence_slider']),
    'pd_kp': ('P 增益', SPECS['pd_kp_slider']),
    'pd_kd': ('D 阻尼', SPECS['pd_kd_slider']),
}


def format_lock_model_status(state):
    """Pure runtime readout: model estimates are never called calibration.

    Tolerate missing/invalid diagnostic fields without inventing defaults or
    letting a NaN/zero divisor break the GUI polling loop.
    """
    if not isinstance(state, dict) or state.get('control_algorithm') != 'lock':
        return '预测强锁未启用 · 沿用所选控制模式'
    prediction = state.get('prediction')
    model = prediction.get('camera_response_model') if isinstance(prediction, dict) else None
    model = model if isinstance(model, dict) else {}

    def number(value, lower, upper=math.inf):
        return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and lower <= value <= upper else None)

    ratio = number(model.get('response_scale'), .6, 1.4)
    delay = number(model.get('delay_ms'), 0.)
    gain = number(state.get('gain'), .05, 8.)
    identified = model.get('identified')
    status = '模型已识别' if identified is True else '建模中' if identified is False else '模型未知'
    ratio_text = f'{ratio:.3f}' if ratio is not None else '未知'
    delay_text = f'{delay:.1f} ms' if delay is not None else '未知'
    effective = f'{gain/ratio:.3f}' if gain is not None and ratio is not None else '未知'
    output = '输出已开启' if state.get('enabled') is True else '输出关闭'
    return (f'预测强锁 · {status} · {output} · 响应比例 {ratio_text} · 模型时延 {delay_text} · '
            f'有效换算增益 {effective}（设置增益/估计比例） · 非实机标定'
            + ('；建模初期可能暂缓后续修正' if identified is False else
               '；激励不足保留上次估计，不保证持续正确' if identified is True else ''))


def format_fire_lock_status(state):
    """Read-only indication of a firing retention window, never a player ID."""
    if not isinstance(state, dict) or state.get('fire_target_lock') is not True:
        return '开火保留当前目标：关闭 · 沿用原目标切换策略'
    guard = state.get('fire_commitment')
    if not isinstance(guard, dict):
        return '开火保留当前目标：已启用 · 保留窗口状态未知；不能判断击杀'
    active, held = guard.get('active'), guard.get('held')
    remaining = guard.get('remaining_ms')
    valid_remaining = (isinstance(remaining, (int, float)) and not isinstance(remaining, bool)
                       and math.isfinite(remaining) and remaining >= 0.)
    if active is True:
        phase = ('开火期间' if held is True else
                 f'击发后保留 {remaining:.0f} ms' if valid_remaining else '保留窗口中')
    elif active is False:
        phase = '等待击发 · 非开火窗口沿用原切换策略'
    else:
        phase = '保留窗口状态未知'
    return ('开火保留当前目标：已启用 · ' + phase +
            '；保留窗口内原目标丢检/难以区分时暂停自动移动，不改增益/追赶/上限；不能判断击杀')


def outline_color_label(value):
    """Display invalid saved values without silently choosing a color."""
    return OUTLINE_COLORS.get(value, '无效配置 · 请重新选择') if type(value) is str else '无效配置 · 请重新选择'


def format_outline_setting(current, draft=None, error=''):
    """Pure disclosure; color changes affect eligibility, not numeric strength."""
    label = outline_color_label(current)
    lines = ['当前已生效轮廓：' + label]
    if draft is not None and draft != label:
        lines.append('待应用轮廓：' + (draft if type(draft) is str and draft in OUTLINE_COLORS.values() else '无效选择'))
        lines.append('需先停止识别再应用；有效变更会关闭跟随和扳机，需手动重新开启')
    if error:
        lines.append('配置提示：' + str(error))
    lines.append('仅改颜色匹配，不改增益/追赶/上限；会改变哪些框通过，不代表逐帧等强度')
    lines.append('紫色为实验兼容：技能、衣物可能误识别；颜色证据不能证明真实阵营，也不证明召回提升')
    lines.append('选错颜色可能漏检；紫色模式不会同时保留红色规则，不是全色增强')
    lines.append('不关闭现有友方颜色否决、身体形状与头身关联规则；严格筛选关闭时颜色选项不参与验证')
    return '\n'.join(lines)


class UiSettingsMixin:
    def mark_ui_draft(self, field=None, *args):
        if getattr(self, '_ui_syncing', False) or self._closing:
            return
        if field == 'gain' and self.gain_mode_var.get() != '手动':
            self.gain_mode_var.set('手动')
        self.ui_commit_status.set('待应用 · 回车提交整组，Esc 放弃编辑')
        self.refresh_ui_field_states()

    def sync_ui_drafts(self):
        if not hasattr(self, 'ui_fields'):
            return
        self._ui_syncing = True
        try:
            state = self.follower.snapshot()
            state.update(base_limit=self.adaptation_inputs['base_limit'], confidence=self.confidence)
            for key, cell in self.ui_fields.items():
                cell.set(f"{state[key]:.6f}".rstrip('0').rstrip('.') if key == 'gain' else format_value(state[key], FIELDS[key][1]))
            if hasattr(self, 'aim_region_var'):
                self.aim_region_var.set(AIM_REGIONS[state['aim_region']])
            if hasattr(self, 'enemy_color_var'):
                self.enemy_color_var.set(outline_color_label(getattr(self, 'enemy_color', 'red')))
            self.gain_mode_var.set('手动' if self.adaptation_inputs.get('manual_gain') is not None else '自动')
            self.soft_mode_var.set(state['soft_assist'])
            if hasattr(self, 'manual_fire_priority_var'):
                self.manual_fire_priority_var.set(state['manual_fire_priority'])
            if hasattr(self, 'fire_target_lock_var'):
                self.fire_target_lock_var.set(state.get('fire_target_lock', False))
            self.rhythm_var.set(self.follower.pacing_enabled)
            self.algorithm_var.set(next(k for k, v in self.ui_config['algorithms'].items() if v == state['control_algorithm']))
            self.capture_var.set(self.ui_config['capture_scopes'][self.capture_scope])
            self.inference_var.set(str(self.inference_size))
            self.dpi_var.set(str(self.adaptation_inputs['dpi']))
            self.sensitivity_var.set(f"{self.adaptation_inputs['sensitivity']:g}")
            self._committed_monitor = self.monitor.current()
            for cell in self.ui_errors.values():
                cell.set('')
        finally:
            self._ui_syncing = False
        self.refresh_ui_field_states()

    def refresh_ui_field_states(self):
        if not hasattr(self, 'ui_fields'):
            return
        active = {
            'tolerance_px': self.soft_mode_var.get(),
            'pd_kp': self.ui_config['algorithms'].get(self.algorithm_var.get()) == 'pd',
            'pd_kd': self.ui_config['algorithms'].get(self.algorithm_var.get()) == 'pd',
        }
        for key, enabled in active.items():
            for widget in self.ui_field_widgets.get(key, ()):
                widget.configure(state='normal' if enabled else 'disabled')
        self.tolerance_hint.set('柔和容差生效' if active['tolerance_px'] else '原逻辑自动停止区；手动容差未启用')
        self.gain_hint.set('手动覆盖 · DPI / 灵敏度变化不会覆盖此值' if self.gain_mode_var.get() == '手动' else '自动建议 · 输入增益即可切换为手动')
        self.refresh_outline_status()
        self.refresh_strength_costs()

    def refresh_outline_status(self):
        current = getattr(self, 'enemy_color', 'red')
        draft = self.enemy_color_var.get() if hasattr(self, 'enemy_color_var') else None
        error = getattr(self, '_enemy_color_load_error', '')
        if hasattr(self, 'outline_compatibility_status'):
            self.outline_compatibility_status.set(format_outline_setting(current, draft, error))
        if hasattr(self, 'outline_current_status'):
            self.outline_current_status.set('当前已生效轮廓：' + outline_color_label(current) +
                (' · 配置无效，需重新选择后应用' if error else '') +
                (' · 严格筛选关闭，颜色不参与验证' if getattr(self, 'enemy_filter', True) is False else ''))

    def refresh_strength_costs(self):
        if not hasattr(self, 'strength_cost_detail'):
            return
        before = self.follower.snapshot()
        before['paced_corrections'] = self.follower.pacing_enabled
        proposed = dict(before)
        try:
            for key in ('gain', 'response_ms', 'chase_strength', 'tolerance_px'):
                proposed[key] = parse_value(self.ui_fields[key].get(), FIELDS[key][1])
            before['step_limit'] = self.adaptation_inputs['base_limit']
            proposed['step_limit'] = parse_value(self.ui_fields['base_limit'].get(), FIELDS['base_limit'][1])
            proposed['soft_assist'] = self.soft_mode_var.get()
            proposed['manual_fire_priority'] = self.manual_fire_priority_var.get()
            proposed['fire_target_lock'] = (self.fire_target_lock_var.get() if hasattr(self, 'fire_target_lock_var')
                                            else before.get('fire_target_lock', False))
            proposed['paced_corrections'] = self.rhythm_var.get()
            proposed['control_algorithm'] = self.ui_config['algorithms'].get(self.algorithm_var.get())
            if proposed['control_algorithm'] == 'pd':
                for key in ('pd_kp', 'pd_kd'):
                    proposed[key] = parse_value(self.ui_fields[key].get(), FIELDS[key][1])
            if hasattr(self, 'aim_region_var'):
                proposed['aim_region'] = next((key for key, label in AIM_REGIONS.items() if label == self.aim_region_var.get()), before['aim_region'])
            changes = parameter_cost_changes(before, proposed)
            if (float(self.sensitivity_var.get()) != self.adaptation_inputs['sensitivity'] or
                    self.monitor.current() != getattr(self, '_committed_monitor', self.monitor.current())):
                changes.append('【自动适配变化】灵敏度/显示器变化可能改变实际增益和上限；应用后列出实际变化')
        except (ValueError, TypeError):
            self.strength_impact_badge.set('待应用：参数无效')
            self.strength_cost_detail.set('参数尚未通过校验；不会应用。')
            if hasattr(self, 'draft_change_summary'):
                self.draft_change_summary.set('参数无效 · 尚未应用，请检查输入或放弃编辑')
            return
        active = active_costs(before)
        self.strength_impact_badge.set('待应用：有力度/响应代价' if changes else
            '当前：有力度/响应代价' if active else '未启用额外减弱/延迟选项')
        lines = ['当前已生效选项：'] + (active or ['没有列出的额外减弱/延迟选项；不代表已经验证等强度。'])
        if changes:
            lines += ['本次草稿会产生：'] + changes
        previous = getattr(self, '_last_strength_change_notice', '')
        if previous:
            lines += ['最近一次实际参数变更：', previous]
        self.strength_cost_detail.set('\n'.join(lines))
        if hasattr(self,'draft_change_summary'):
            categories=list(dict.fromkeys(note.split('】',1)[0].lstrip('【') for note in changes))
            self.draft_change_summary.set(('草稿代价：'+' / '.join(categories)) if categories else
                ('编辑后请应用全部参数 · 当前策略有代价' if active else '编辑后请应用全部参数 · Esc 放弃编辑'))


    def step_ui_field(self, key, direction):
        if self._closing:
            return
        if key == 'tolerance_px' and not self.soft_mode_var.get():
            return
        if key in ('pd_kp', 'pd_kd') and self.ui_config['algorithms'].get(self.algorithm_var.get()) != 'pd':
            return
        spec = FIELDS[key][1]
        try:
            old = parse_value(self.ui_fields[key].get(), spec)
            value = min(Decimal(str(spec.maximum)), max(Decimal(str(spec.minimum)),
                        Decimal(str(old)) + Decimal(str(spec.step)) * direction))
            self.ui_fields[key].set(format_value(value, spec))
        except ValueError as exc:
            self.ui_errors[key].set(str(exc))

    def revert_ui_drafts(self, event=None):
        if not self._closing:
            self.monitor.current(getattr(self, '_committed_monitor', 0))
            self.sync_ui_drafts()
            self.adapt_error.set(self._initial_adaptation_notice or self.adaptation_result.warning)
            self.ui_commit_status.set('已放弃编辑 · 生效参数未变')
        return 'break' if event is not None else True

    def preview_control_algorithm(self, algorithm):
        # Preview only: preserve every numeric/optional field, saved setting and
        # enabled state. Explicit Apply uses the existing disarm transaction.
        if self._closing:
            return False
        label = next((label for label, value in self.ui_config['algorithms'].items()
                      if value == algorithm), None)
        if label is None:
            return False
        self.algorithm_var.set(label)
        self.mark_ui_draft()
        self.ui_commit_status.set('待应用 · 仅预览控制模式；全部数值和选项保持，请查看力度/响应代价')
        return True

    def restore_auto_gain(self):
        # This is a draft too: no surprise disarm while editing another field.
        self.gain_mode_var.set('自动')
        self.mark_ui_draft()

    def commit_ui_parameters(self, event=None):
        if self._closing:
            return 'break' if event is not None else False
        for cell in self.ui_errors.values():
            cell.set('')
        field = None
        try:
            state = self.follower.snapshot()
            motion = {}
            current_color = getattr(self, 'enemy_color', 'red')
            color_label = (self.enemy_color_var.get() if hasattr(self, 'enemy_color_var')
                           else outline_color_label(current_color))
            color = next((key for key, label in OUTLINE_COLORS.items() if label == color_label), None)
            if color is None:
                raise ValueError('敌方轮廓颜色无效，请明确选择红色或紫色；本组尚未应用')
            color = normalize_enemy_color(color)
            color_changed = color != current_color
            if color_changed and self.worker is not None and self.worker.is_alive():
                raise ValueError('请先停止识别，再修改敌方轮廓颜色；本组尚未应用')
            region_label = self.aim_region_var.get() if hasattr(self, 'aim_region_var') else AIM_REGIONS[state['aim_region']]
            region = next((key for key, label in AIM_REGIONS.items() if label == region_label), None)
            if region is None:
                raise ValueError('定位部位无效，请选择原定位或胸部定位')
            motion['aim_region'] = region
            for field in ('response_ms', 'chase_strength', 'base_limit'):
                motion[field] = parse_value(self.ui_fields[field].get(), FIELDS[field][1])
            soft = self.soft_mode_var.get()
            if type(soft) is not bool:
                raise ValueError('柔和容差状态无效')
            motion['soft_assist'] = soft
            manual_priority = self.manual_fire_priority_var.get() if hasattr(self, 'manual_fire_priority_var') else state['manual_fire_priority']
            if type(manual_priority) is not bool:
                raise ValueError('开火手动输入优先状态无效')
            motion['manual_fire_priority'] = manual_priority
            fire_lock = (self.fire_target_lock_var.get() if hasattr(self, 'fire_target_lock_var')
                         else state.get('fire_target_lock', False))
            if type(fire_lock) is not bool:
                raise ValueError('开火保留当前目标状态无效')
            motion['fire_target_lock'] = fire_lock
            field = 'tolerance_px'
            motion[field] = parse_value(self.ui_fields[field].get(), FIELDS[field][1]) if soft else state[field]
            field = 'gain'
            mode = self.gain_mode_var.get()
            if mode not in ('自动', '手动'):
                raise ValueError('增益模式无效')
            manual = parse_value(self.ui_fields[field].get(), FIELDS[field][1]) if mode == '手动' else None
            field = 'confidence'
            confidence = parse_value(self.ui_fields[field].get(), FIELDS[field][1])
            field = None
            algorithm = self.ui_config['algorithms'].get(self.algorithm_var.get())
            if algorithm not in ('legacy', 'pd', 'lock'):
                raise ValueError('控制模式无效')
            if algorithm == 'lock' and state.get('predictive') is not True:
                raise ValueError('预测强锁需要开启预测；请恢复预测设置后再应用。本组未应用')
            motion['control_algorithm'] = algorithm
            if algorithm == 'pd':
                for field in ('pd_kp', 'pd_kd'):
                    motion[field] = parse_value(self.ui_fields[field].get(), FIELDS[field][1])
            field = None
            scope = next((k for k, v in self.ui_config['capture_scopes'].items() if v == self.capture_var.get()), None)
            size = int(self.inference_var.get())
            if scope is None or size not in self.ui_config['inference_sizes']:
                raise ValueError('范围或识别尺寸无效')
            capture_changed = (scope, size) != (self.capture_scope, self.inference_size)
            if capture_changed and self.worker is not None and self.worker.is_alive():
                raise ValueError('请先停止识别，再修改范围 / 输入尺寸；本组尚未应用')
            rhythm = self.rhythm_var.get()
            if type(rhythm) is not bool:
                raise ValueError('修正节奏状态无效')
        except (ValueError, TypeError) as exc:
            self.disarm()
            if field is not None:
                self.ui_errors[field].set(str(exc))
            self.adapt_error.set((FIELDS[field][0] + '：' if field is not None else '') + str(exc))
            self.ui_commit_status.set('未应用 · 请修正输入；跟随已暂停')
            return 'break' if event is not None else False
        base_limit = motion.pop('base_limit')
        other_extras_changed = capture_changed or self.confidence != confidence or rhythm != self.follower.pacing_enabled
        extras_changed = color_changed or other_extras_changed
        # apply_adaptation validates DPI, sensitivity and display width before the
        # first mutation. Every optional/advanced field has also passed validation.
        ok = self.apply_adaptation(base_limit, motion, manual_gain=manual, sync_ui=False)
        if ok:
            if extras_changed:
                self.disarm()
                self.follower.set_pacing_enabled(rhythm)
                self.confidence = confidence
                if color_changed:
                    self.enemy_color = color
                    self._enemy_color_load_error = ''
                    self.target_filter_report = {}
                    if hasattr(self, 'target_filter_status'):
                        self.target_filter_status.set('轮廓颜色已改变 · 旧筛选结果已清空；等待新识别结果')
                self.capture_scope, self.inference_size = scope, size
                if capture_changed:
                    self.current_capture_area = None
                if other_extras_changed:
                    self.save_capture_profile()
                self.schedule_motion_save()
            self.sync_ui_drafts()
            self._refresh_strength_profile()
            if not self.adapt_error.get():
                self.ui_commit_status.set(('轮廓颜色已应用：' + OUTLINE_COLORS[color] + ' · 跟随和扳机已关闭，请手动重新开启') if color_changed else
                    '参数已应用 · 跟随保持关闭时，请手动 / F8 重新开启')
            else:
                self.ui_commit_status.set('参数已应用 · 请查看上方提示')
        else:
            self.ui_commit_status.set('未应用 · 生效参数未变；跟随已暂停')
        return 'break' if event is not None else ok

    def restore_basis_strength(self):
        level = getattr(self, '_base_strength_level', None)
        if level is None:
            self.ui_commit_status.set('当前为未知手动组合 · 请明确选择一个档位')
            return False
        return self.apply_strength_level(level)

    def save_capture_profile(self):
        try:
            atomic_json(self.capture_settings_path, dict(scope=self.capture_scope, input_size=self.inference_size,
                                                         confidence=self.confidence))
            self.capture_settings_status.set('识别参数已保存')
            return True
        except OSError:
            self.adapt_error.set('识别参数已应用，但保存失败；请检查目录权限。')
            self.capture_settings_status.set('识别参数保存失败')
            return False
