"""Deterministic V7 strength presets; no device, aiming or firing operations.

Presets are engineering starting points, not measured gameplay capabilities.
Only these five control fields belong to a strength level. Calibration, firing
protection, prediction and target eligibility remain independent settings.
"""
from dataclasses import dataclass
import math

CUSTOM_STRENGTH='手动参数'


@dataclass(frozen=True)
class StrengthProfile:
    level: int
    name: str
    response_ms: int
    chase_strength: float
    step_limit: int
    tolerance_px: float
    capability: str
    tradeoff: str
    soft_assist: bool = True

    @property
    def label(self):return f'{self.level}档 · {self.name}'

    def motion(self):
        # A new dict prevents UI callbacks from mutating the preset registry.
        return dict(soft_assist=self.soft_assist,response_ms=self.response_ms,
                    chase_strength=self.chase_strength,step_limit=self.step_limit,
                    tolerance_px=self.tolerance_px)


STRENGTH_PROFILES=(
    StrengthProfile(1,'极轻',60,1.,90,12.,
                    '修正较慢、容差最大，保留较多手动空间',
                    '快速移动目标更容易跟不上'),
    StrengthProfile(2,'轻辅助',30,1.5,160,8.,
                    '保留现有柔和预设，接近目标后减少细拉',
                    '允许一定位置偏差，不追求贴住中心'),
    StrengthProfile(3,'柔和',24,2.,200,6.,
                    '缩小容差并提高修正力度，跟随更紧',
                    '相较低档更可能与手动操作拉扯'),
    StrengthProfile(4,'均衡',18,2.5,260,4.,
                    '更积极地缩小偏差，接近中心后仍有缓动',
                    '过冲和反复修正的可能性增加'),
    StrengthProfile(5,'紧跟',12,3.,320,3.,
                    '保留原第五档的响应和预算',
                    '拉扯可能更明显，不一定更准'),
    StrengthProfile(6,'强辅助',6,6.,480,3.,
                    '沿用原最高自定义组合；手动容差关闭',
                    '修正更直接，手动拉扯可能更明显', soft_assist=False),
)
STRENGTH_CHOICES=tuple(profile.label for profile in STRENGTH_PROFILES)+(CUSTOM_STRENGTH,)


def get_strength_profile(level):
    if type(level) is not int or not 1<=level<=len(STRENGTH_PROFILES):
        raise ValueError('Strength level must be an integer from 1 to 6')
    return STRENGTH_PROFILES[level-1]


def profile_from_label(label):
    if label==CUSTOM_STRENGTH:return None
    for profile in STRENGTH_PROFILES:
        if label==profile.label:return profile
    raise ValueError('Unknown strength label')


def identify_strength(state):
    """Reconstruct the level from real parameters, never from a stale saved ID."""
    for profile in STRENGTH_PROFILES:
        matches=state.get('soft_assist') is profile.soft_assist
        for name,expected in profile.motion().items():
            if name=='soft_assist':continue
            actual=state.get(name)
            if (isinstance(actual,bool) or not isinstance(actual,(int,float)) or
                    not math.isfinite(actual) or not math.isclose(actual,expected,rel_tol=0,abs_tol=1e-8)):
                matches=False;break
        if matches:return profile.level
    return None


def describe_strength(state):
    level=identify_strength(state)
    if level is None:
        result='手动参数：实际参数未匹配固定档位；选择一个档位会替换响应、追赶、位移预算和容差。'
        if state.get('soft_assist') is not True:result+=' 当前柔和辅助已关闭。'
    else:
        profile=get_strength_profile(level)
        result=f'能力：{profile.capability}；取舍：{profile.tradeoff}。'
    if state.get('mode','relative')!='relative':
        result+=' 当前为桌面定位，档位效果仅针对相对跟随。'
    elif state.get('stabilization',True) is not True:
        result+=' 稳定控制已关闭，缓动与容差暂不生效。'
    elif state.get('predictive',True) is not True:
        result+=' 位置预测已关闭，不含运动前馈。'
    return result


def strength_guide():
    lines=['六档调节效果（控制预设，不是命中能力保证）',
           '只调响应、追赶、向量预算、容差，保留各档原柔和辅助状态。',
           '不改灵敏度增益、开火暂停、松键间隔、预测或敌方筛选。','']
    for profile in STRENGTH_PROFILES:
        lines += [profile.label,
                  f'  参数：{profile.response_ms} ms / {profile.chase_strength:g}× / 向量 {profile.step_limit} 计数 / 容差 {profile.tolerance_px:g} px',
                  f'  能力：{profile.capability}；取舍：{profile.tradeoff}。','']
    lines += ['2档与原“V5 柔和”力度相同；6档为原最高自定义组合，手动容差关闭。',
              '切档先暂停辅助，确认后用 F8 重新开启；参数自动保存。',
              '响应 ms 是控制参数，不是截图到游戏的延迟。',
              '容差是原截图坐标的半径；位移还受时间预算限制。',
              '高档不一定更准；所有档位都不是自动压枪或防封措施。']
    return '\n'.join(lines)
