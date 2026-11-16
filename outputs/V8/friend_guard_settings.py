"""Pure mixed-outline veto preference/disclosure; no input/capture/model imports."""
import json
FRIEND_GUARD_COST = '【可能漏检 / 暂停候选】单帧框外沿多行、多个边区出现友方色，与敌方轮廓冲突时拒绝该目标；绿衣、遮挡、特效、队友紧贴可能让真敌人被拒绝。只增加否决，不改增益/追赶/上限/响应时间；增加少量像素统计，不能保证帧率不变。颜色不能证明阵营，仍可能误锁。'

def load_friend_guard(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8-sig'))['enabled']
        if type(value) is not bool:raise ValueError('enabled must be boolean')
        return value,''
    except FileNotFoundError:return False,''
    except (OSError,ValueError,KeyError,TypeError):
        return True,'保护配置无效，暂按开启处理；请停止识别后明确选择并应用'

def format_friend_guard(enabled,draft=None,strict=True,error=''):
    status='开启' if enabled else '关闭（原筛选）'
    text='友方轮廓冲突保护：'+status
    if strict is not True:text+='；严格筛选关闭，当前不生效'
    if type(draft) is bool and draft!=enabled:text+='；待应用：'+('开启' if draft else '关闭')
    text+='\n'+FRIEND_GUARD_COST+'\n更改前须停止识别；应用后跟随/扳机关闭，需手动开启。关闭保护恢复原筛选，误锁风险回升；本开关不是阵营保证。'
    if error:text+='\n'+str(error)
    return text
