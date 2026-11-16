"""Explicit outline settings; pure configuration only, no pixel/model/input imports."""

OUTLINE_COLORS = {
    'red': '红色（原行为）',
    'purple': '紫色（实验兼容）',
}


VERIFIED_OUTLINE_REASONS = frozenset(('red_outline_verified', 'purple_outline_verified'))


def normalize_enemy_color(value):
    """Validate without coercion, autodetection or silent fallback.

    Callers may use red when a legacy configuration lacks this field, but must
    not convert an invalid supplied value to red or to a non-strict setting.
    """
    if type(value) is not str or value not in ('red', 'purple'):
        raise ValueError('敌方轮廓颜色无效，仅支持 red 或 purple')
    return value
