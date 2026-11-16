"""Explain strength/response tradeoffs. Read-only, no controller or device calls."""


FIRE_TARGET_LOCK_COST = '【延后换敌 / 可能暂停】开火期间及松开后 350 ms 保留原目标；原目标丢检或无法可靠区分时暂停自动移动，不切到另一人。可能推迟攻击下一目标，不降低设置增益/追赶/上限，也不代表一直有输出；画面匹配不能判断击杀。同一目标出现 >8 px 非预期几何跳变时重置旧速度/滤波状态（保留实际输入记录），瞬时修正/前馈可能变化，不保证逐帧等强度；匹配增加 CPU 工作，不保证帧率不变；目标拥挤、计算预算不足也可能暂停'

LOCK_BOOTSTRAP_COST = '【可能暂缓修正】反馈未识别时按 100 ms 模型保留未反馈位移，可能暂缓后续修正；有足够变化供识别后才刷新估计'
LOCK_COMPUTE_COST = '【计算代价】联合拟合和 32 点历史重建增加 CPU 工作；不保证识别吞吐、帧率或端到端延迟不变'
LOCK_SCALE_COST = '【实际力度变化】响应比例估计范围 0.6–1.4，实际换算增益 = 设置增益 / 估计比例；自动输出可能减小或增大，不是实机标定，标称参数不变不代表实际力度不变'

LOCK_FEEDFORWARD_COST = '【降低小运动前馈】原有小运动前馈抑制仍保留：未满 8 px 的短窗运动将速度前馈置 0；低速前馈按 20–80 px/s 权重减弱，不影响位置误差修正是否运行'
LOCK_UNCERTAINTY_COST = '【模型不确定性】窗口拟合可能因目标动作、噪声或映射变化误判；模型已识别不代表持续正确，激励不足时保留上次估计'

def parameter_cost_changes(before, after):
    merged = dict(before, **after)
    notes = []
    for key, label in (('gain', '位移增益'), ('chase_strength', '追赶倍率'), ('step_limit', '位移上限')):
        a, z = before.get(key), merged.get(key)
        if isinstance(a, (int,float)) and isinstance(z, (int,float)) and z < a:
            notes.append(f'【降低力度】{label} {a:g} → {z:g}')
    if merged.get('response_ms', 0) > before.get('response_ms', 0):
        notes.append(f"【响应变慢】修正响应 {before['response_ms']:g} → {merged['response_ms']:g} ms")
    if merged.get('soft_assist') and (not before.get('soft_assist') or merged.get('tolerance_px',0) > before.get('tolerance_px',0)):
        notes.append('【降低近中心力度】启用柔和容差或扩大停止范围')
    if merged.get('manual_fire_priority') and not before.get('manual_fire_priority'):
        notes.append('【让出力度】开火手动移动的轴，辅助输出可降至 0')
    if merged.get('fire_target_lock') and not before.get('fire_target_lock'):
        notes.append(FIRE_TARGET_LOCK_COST)
    elif before.get('fire_target_lock') and merged.get('fire_target_lock') is False:
        notes.append('【恢复自动换敌】关闭开火保留；原目标失配后可能改选其他人，开火不再延长保留窗口')
    if merged.get('pause_on_fire') and not before.get('pause_on_fire'):
        notes.append('【暂停辅助】开火时不输出自动修正')
    if merged.get('fire_release_ms',0) > before.get('fire_release_ms',0) and merged.get('pause_on_fire'):
        notes.append('【延长暂停】松键后恢复等待更长')
    if merged.get('predictive') is False and before.get('predictive') is True:
        notes.append('【响应代价】关闭预测可能增加移动目标跟随滞后')
    if merged.get('control_algorithm') == 'pd' and (before.get('control_algorithm') != 'pd' or any(merged.get(k) != before.get(k) for k in ('pd_kp','pd_kd'))):
        notes.append('【改变瞬时力度】PD 的 P/D 会改变修正和刹车，不能保证等强度')
    if merged.get('control_algorithm') == 'lock' and before.get('control_algorithm') != 'lock':
        notes.append('【改变瞬时力度】预测强锁使用原始位置误差并扣除未反馈量，不保留旧速度积累；去掉固定近场 20 ms 下限，近场采用用户响应、远场由追赶倍率缩短时间常数。原始头/胸位置更敏感噪声，可能增加瞬时修正与抢鼠标，不保证无抖或等强度')
        notes.extend((LOCK_BOOTSTRAP_COST, LOCK_SCALE_COST, LOCK_FEEDFORWARD_COST, LOCK_UNCERTAINTY_COST, LOCK_COMPUTE_COST))
    elif before.get('control_algorithm') == 'lock' and merged.get('control_algorithm') == 'legacy':
        notes.append('【反馈律变化】退出预测强锁，恢复原控制；可能改变瞬时力度和响应，不保证等强度')
    if merged.get('paced_corrections') and not before.get('paced_corrections'):
        notes.append('【延迟代价】启用分段修正，计划最多延后 3 ms')
    if merged.get('aim_region') == 'chest' and before.get('aim_region') != 'chest' and merged.get('predictive') and merged.get('control_algorithm') != 'lock':
        notes.append('【延迟代价】胸部滤波平滑可能增加跟随滞后')
    return notes


def active_costs(state):
    notes=[]
    if state.get('predictive') and state.get('prediction',{}).get('input_coordination_revision') == 'camera-ledger-20261109':
        notes.append('【协同输出变化】换目标保留已记录的手动/提交位移，按反馈估计扣除未反馈量；自动量可能增减，并非逐帧等输出')
    if state.get('low_latency'):
        notes.append('【原有减弱行为】反向手动输入时，内置混合会压低辅助轴输出')
    if state.get('soft_assist'):
        notes.append('【降低近中心力度】柔和容差会减小修正，并在容差内停止')
    if state.get('manual_fire_priority'):
        notes.append('【让出力度】开火手动移动的轴，辅助可降至 0；停手后渐恢复')
    if state.get('fire_target_lock'):
        notes.append(FIRE_TARGET_LOCK_COST)
    if state.get('pause_on_fire'):
        notes.append('【暂停辅助】开火和恢复等待期间不输出修正')
    if state.get('control_algorithm') == 'pd':
        notes.append('【改变瞬时力度】PD 有刹车/修正代价；P<1 会削弱 P 分量')
    if state.get('control_algorithm') == 'lock':
        notes.append('【预测强锁 · 实验】头/胸均采用原始位置误差并扣除未反馈量，无旧速度积累和固定近场 20 ms 下限；近场按用户响应、远场随追赶倍率加快。原始位置更敏感噪声，可能增加瞬时修正与抢鼠标，不保证无抖')
        notes.extend((LOCK_BOOTSTRAP_COST, LOCK_SCALE_COST, LOCK_FEEDFORWARD_COST, LOCK_UNCERTAINTY_COST, LOCK_COMPUTE_COST))
        notes.append('【仍有输出/延迟代价】未反馈量扣除、原停止圈、每轴限幅、已启用的柔和容差/手动混合/开火暂停/分段仍可能削减或延迟输出；每轴预算不足 1 计数时不输出。参数不变不等于逐帧等强度')
    if state.get('paced_corrections'):
        notes.append('【延迟代价】均匀细调计划分摊至 3 ms；完整未中断才保留总量')
    if state.get('control_algorithm') == 'lock' and state.get('predictive'):
        notes.append('【估计代价】速度估计仍有平滑滞后与噪声，可能误判；原始位置不等于免费防抖，尚未验证实机效果')
    elif state.get('aim_region') == 'chest' and state.get('predictive'):
        notes.append('【延迟代价】胸部抑抖滤波增加平滑滞后，不保证等响应')
    elif state.get('predictive'):
        notes.append('【原有响应代价】头部自适应滤波与预测不代表零滞后或无代价')
    return notes
