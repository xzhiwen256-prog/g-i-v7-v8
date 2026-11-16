"""Read-only recognition-stage diagnostics; no inference, images or input APIs."""

REASONS = {
    'head_without_body': '模型有头框，但缺少关联身体框（严格规则拒绝）',
    'body_not_verified': '头框关联的身体框未通过轮廓验证',
    'body_support_only': '身体框仅作关联支撑，不单独作为头部目标',
    'insufficient_red_outline': '红色轮廓证据不足',
    'insufficient_purple_outline': '紫色轮廓证据不足',
    'purple_fill_ambiguous': '紫色填充过多，无法可靠判定轮廓',
    'friendly_outline': '轮廓偏向友方颜色',
    'red_fill_ambiguous': '红色填充过多，无法可靠判定轮廓',
    'invalid_body_shape': '身体框尺寸或比例不符合当前筛选规则',
    'invalid_or_low_confidence': '框无效、超出画面或低于筛选置信度',
    'class_or_visibility_rejected': '类别、阵营或可见性标记不通过',
    'no_pixels': '检测区域没有可用像素',
    'unverified': '尚未验证',
}


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def format_recognition_report(report, aim_region='original'):
    """Explain post-model filtering, never recommend bypassing the filter.

    raw counts boxes AFTER model confidence/NMS, not all pre-threshold network
    candidates or a count of people. Unseen poses and model recall cannot be
    inferred from these counters. The input dictionary is never mutated.
    """
    if not isinstance(report, dict) or 'raw' not in report:
        return '尚无识别结果 · 此处区分模型输出与筛选拒绝，不采集或保存截图'
    raw, eligible = _count(report.get('raw')), _count(report.get('eligible'))
    lines = [f'模型输出 {raw} 框（已过置信度与 NMS） · 筛选通过 {eligible} 框']
    classes = report.get('class_counts')
    if isinstance(classes, dict):
        body, head = _count(classes.get('enemy')), _count(classes.get('enemy_head'))
        lines.append(f'身体框 {body} · 头部框 {head} · 框数不等于人数')
    else:
        lines.append('此记录没有分类计数，不能判断模型是否输出过头部框')
    if raw == 0:
        lines.append('本次没有保留下来的模型框；不代表画面没有敌人，也不能仅凭此判断朝向漏检')
    rejected = report.get('rejected_reasons')
    if isinstance(rejected, dict):
        other = 0
        for reason, count in sorted(rejected.items(), key=lambda pair: str(pair[0])):
            count = _count(count)
            if not count:
                continue
            if reason in REASONS:
                lines.append(f'{REASONS[reason]}：{count}')
            else:
                other += count
        if other:
            lines.append(f'其他过滤原因：{other}（原始原因见诊断日志）')
    if aim_region == 'chest':
        lines.append('胸部定位需要已验证的头身关联；只有头部时不外推胸部，筛选通过不等于胸部可用')
    if report.get('strict') is True:
        color = report.get('enemy_color')
        label = {'red': '红色', 'purple': '紫色'}.get(color) if type(color) is str else None
        if label is None:
            lines.append('当前严格筛选：轮廓颜色未知（记录缺失或无效） + 头身关联；不能假定使用红色')
        else:
            lines.append(f'当前严格筛选：{label}轮廓 + 头身关联；姿态识别效果需要标注样本单独验证')
        if color == 'purple':
            lines.append('紫色为实验兼容；技能/衣物可能产生误识别，颜色证据不能证明阵营')
    elif report.get('strict') is False:
        lines.append('当前为非严格筛选；这里只说明已有状态，不更改筛选选项')
    return '\n'.join(lines)
