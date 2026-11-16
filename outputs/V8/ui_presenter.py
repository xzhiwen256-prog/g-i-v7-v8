"""Read-only V8 workflow presentation; never controls input or changes settings.

workflow_status consumes an already collected follower snapshot. It does not
query the system, instantiate devices, infer a hit, or promise output from an
armed switch. The caller supplies lifecycle flags from its existing UI state.
"""


def workflow_status(state, *, running=False, loading=False, stopping=False,
                    closing=False, recovering=False, arm_pending=False,
                    dirty=False, trigger_enabled=False):
    """Return six display strings without changing the supplied snapshot.

    tone is one of neutral, active, warning, error. Safety and lifecycle
    messages take precedence over an unapplied draft. An optional
    recognition_error string is supplied by the caller on its detached copy.
    Trigger text describes
    its switch/eligibility state, never whether a click actually occurred.
    """
    state = state if isinstance(state, dict) else {}
    stage = state.get('stage')
    stage = stage if isinstance(stage, str) else ''
    enabled = state.get('enabled') is True
    known_enabled = type(state.get('enabled')) is bool
    active = running is True
    pending = arm_pending is True
    trigger_on = trigger_enabled is True
    recognition_error = state.get('recognition_error')
    recognition_failed = isinstance(recognition_error, str) and bool(recognition_error.strip())

    recognition = ('识别：退出中' if closing is True else
                   '识别：不可启动' if recovering is True else
                   '识别：异常' if recognition_failed else
                   '识别：停止中' if stopping is True else
                   '识别：加载中' if loading is True else
                   '识别：运行中' if active else '识别：未运行')
    trigger = '扳机：等待条件' if trigger_on else '扳机：关闭'

    def result(follow, title, hint, tone='neutral', *, trigger_text=None,
               draft_note=True):
        if dirty is True and draft_note:
            hint += '；另有未应用修改，应用后需重新开启跟随'
        return dict(recognition=recognition, follow=follow,
                    trigger=trigger if trigger_text is None else trigger_text,
                    title=title, hint=hint, tone=tone)

    if closing is True:
        return result('跟随：停止中' if enabled else '跟随：关闭', '正在退出 V8',
                      '等待后台任务退出，窗口会自动关闭',
                      trigger_text='扳机：停止中' if trigger_on else '扳机：关闭',
                      draft_note=False)
    if recovering is True:
        return result('跟随：不可用', '输入设备未就绪',
                      '查看运行详情中的连接提示，处理后重新启动 V8；当前仅可调整参数',
                      'error', trigger_text='扳机：不可用')

    input_error = state.get('input_error')
    fire_error = state.get('fire_error')
    has_error = any(isinstance(value, str) and bool(value.strip())
                    for value in (input_error, fire_error))
    if stage in ('input_error', 'manual_unavailable', 'fire_input_unavailable') or has_error:
        hint = ('物理鼠标监测不可用，查看运行详情并确认输入连接后再开启跟随'
                if stage == 'manual_unavailable' else
                '查看运行详情，确认输入连接正常后再开启跟随')
        return result('跟随：输入异常', '输入异常，跟随不可用', hint, 'error')
    if recognition_failed:
        return result('跟随：识别中断', '识别未能继续',
                      '查看下方原始错误和运行详情，处理后重新开始识别；不会自动恢复跟随',
                      'error', trigger_text='扳机：等待识别' if trigger_on else '扳机：关闭')
    if stage == 'invalid_configuration':
        return result('跟随：配置异常', '参数无法使用',
                      '检查参数提示，修正并应用后再开启跟随', 'error')
    if stopping is True:
        return result('跟随：停止中' if enabled else '跟随：关闭', '正在停止识别',
                      '等待后台任务结束，再重新开始或修改画面范围',
                      trigger_text='扳机：停止中' if trigger_on else '扳机：关闭',
                      draft_note=False)
    if loading is True:
        return result('跟随：等待识别' if enabled or pending else '跟随：关闭',
                      '正在加载识别', '加载完成后，切回游戏并按 F8 开启跟随')
    if not active:
        return result('跟随：未运行' if enabled else '跟随：关闭',
                      '先应用参数，再开始识别' if dirty is True else '尚未开始识别',
                      '应用或撤销本次编辑，再点击开始识别' if dirty is True else
                      '确认常用设置后点击开始识别，跟随不会自动开启', draft_note=False)
    if not known_enabled:
        return result('跟随：状态未知', '等待控制状态更新',
                      '查看运行详情；状态未确认前不要重复开启', 'warning')
    if pending and not enabled:
        return result('跟随：等待前台', '请切回游戏窗口',
                      '在待开启窗口内切回游戏；也可在游戏中按 F8，再点跟随可取消', 'warning')
    if not enabled:
        if stage == 'focus_lost':
            return result('跟随：关闭', '跟随因失焦关闭',
                          '切回游戏后按 F8 重新开启；识别仍在运行', 'warning')
        return result('跟随：关闭', '识别已运行，跟随尚未开启',
                      '切回游戏并按 F8，或勾选准心跟随后切回游戏')
    if state.get('focus_paused') is True or stage == 'focus_paused':
        return result('跟随：前台暂停', '已开启，等待切回游戏',
                      '当前不输出鼠标修正；切回后重新确认画面，F8 可关闭', 'warning')
    if state.get('focus_reacquiring') is True or stage == 'focus_reacquiring':
        return result('跟随：确认新画面', '正在恢复跟随',
                      '已回到前台，等待不同的新画面确认目标；F8 可关闭')
    if stage == 'fire_target_hold':
        return result('跟随：保留目标', '原目标未确认，修正暂停',
                      '保持原目标而不切换旁人；松键保护结束后恢复重选，F8 可关闭', 'warning')
    if stage == 'target_hold':
        return result('跟随：等待原目标', '原目标暂时丢失',
                      '暂不输出修正，等待原目标或按既有规则重新选择', 'warning')
    if stage == 'manual_yield':
        return result('跟随：手动优先', '已让出手动操作轴',
                      '当前设置会降低该轴辅助输出；停止手动移动后渐恢复，F8 可关闭', 'warning')
    if stage in ('firing_pause', 'fire_cooldown', 'fire_reacquiring'):
        title = {'firing_pause': '开火期间暂停辅助',
                 'fire_cooldown': '等待松键恢复间隔',
                 'fire_reacquiring': '开火后重新确认目标'}[stage]
        return result('跟随：开火暂停', title,
                      '按当前开火保护设置等待，不发送自动修正；F8 可关闭', 'warning')
    if stage in ('no_target', 'target_rejected'):
        rejected = stage == 'target_rejected'
        return result('跟随：等待目标', '没有通过校验的目标' if rejected else '正在等待目标',
                      '跟随开关已开启；目标通过检测与校验后才输出修正，F8 可关闭')
    if stage in ('acquiring', 'tracking'):
        return result('跟随：确认目标', '正在确认目标',
                      '等待目标关联确认，不会将开启开关视为已经输出；F8 可关闭')
    if stage in ('invalid_frame', 'stale', 'repeat_frame'):
        return result('跟随：等待新画面', '画面暂不可用于修正',
                      '等待新的有效画面，不补发旧帧修正；持续出现时查看运行详情',
                      'warning' if stage != 'repeat_frame' else 'neutral')
    if state.get('input_inflight') is True or stage == 'input_inflight':
        return result('跟随：等待回执', '正在处理上一条输入',
                      '等待输入完成，不叠加发送；持续出现时查看运行详情')
    if stage == 'centered':
        return result('跟随：中心范围', '目标已在中心范围',
                      '当前无需继续细拉；已开启的扳机仍需满足自身条件，F8 可关闭',
                      'active', trigger_text='扳机：已开启' if trigger_on else '扳机：关闭')
    if stage == 'settling':
        return result('跟随：细调等待', '正在等待足够的细调量',
                      '本次修正不足一个计数，暂不发送；F8 可关闭')
    if stage == 'sent':
        return result('跟随：修正中', '正在跟随目标',
                      '使用已应用参数逐帧修正；F8 可随时关闭', 'active')
    return result('跟随：已开启', '跟随开关已开启',
                  '等待明确的目标与输出状态；当前状态不代表已发送鼠标修正')


def format_timing_status(performance, rhythm):
    """Format detached timing snapshots only; no polling, settings or control calls.

    The rhythm snapshot may be older than the latest loop sample. Neither is
    a measurement of game/display response or a same-command joined trace.
    """
    import math

    perf = performance if isinstance(performance, dict) else {}
    rhythm = rhythm if isinstance(rhythm, dict) else {}

    def milliseconds(value):
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not 0 <= value <= 1e9):
            return '未知'
        return f'{value:.3f} ms' if math.isfinite(value) else '未知'

    def count(value):
        return value if type(value) is int and value >= 0 else None

    enabled = rhythm.get('enabled')
    status = '开启' if enabled is True else '关闭' if enabled is False else '未知'
    lines = [
        '最近循环限帧等待：' + milliseconds(perf.get('pacing_ms')) + '（不是分片耗时）',
        '最近循环控制调用：' + milliseconds(perf.get('control_ms')) + '（含分片执行，未必有提交）',
        '分片快照：' + status + ' · 计划窗口 ' + milliseconds(rhythm.get('planned_window_ms')) + '（不是实际完成上限）',
    ]
    last = rhythm.get('last')
    if isinstance(last, dict):
        complete = last.get('complete')
        outcome = '完整完成' if complete is True else '已中断' if complete is False else '完成状态未知'
        reports = count(last.get('reports'))
        report_text = str(reports) if reports is not None else '未知'
        lines.append('最近记录的分片：' + milliseconds(last.get('elapsed_ms')) + ' · ' + outcome
                     + ' · ' + report_text + ' 个已确认报告（可能为历史值，不是 P95）')
    else:
        lines.append('最近记录的分片：暂无可用记录（不按 0 ms 处理）')
    total, overruns = count(rhythm.get('split_commands')), count(rhythm.get('scheduling_overruns'))
    if total is not None and overruns is not None and overruns <= total:
        ratio = f'{100*overruns/total:.2f}%' if total else '无可统计比例'
        lines.append(f'进程累计调度超时：{overruns}/{total}（{ratio}；不是总时延超过 3 ms 的帧比例）')
    else:
        lines.append('进程累计调度超时：未知或计数不一致')
    detail = rhythm.get('partition_timing')
    if isinstance(detail, dict):
        recording = '开启' if detail.get('enabled') is True else '关闭' if detail.get('enabled') is False else '未知'
        records = detail.get('records')
        retained = str(min(len(records), 64)) if isinstance(records, list) else '未知'
        lines.append('有界分片时序诊断：' + recording + ' · 保留 ' + retained + '/64 条命令（不是全帧；关闭可留历史）')
    lines.append('分片复用约每秒更新的诊断快照；与循环样本未必同一命令，均非游戏/显示端到端延迟')
    return '\n'.join(lines)
