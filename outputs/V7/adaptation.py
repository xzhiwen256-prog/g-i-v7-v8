"""Pure V7 profile conversion. No device, game, model or capture imports.

Hardware DPI describes physical hand movement. It is NOT an extra multiplier
on the relative counts already emitted by the V5 controller. The reference is
the V5 engineering estimate, not a measured game calibration.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import math
import os
import shutil
import unicodedata
import uuid


@dataclass(frozen=True)
class Reference:
    dpi: int = 800
    sensitivity: float = .3
    width: int = 2560
    gain: float = 2.68


REFERENCE = Reference()
DEFAULT_PROFILE = dict(schema=1, dpi=800, sensitivity=.3, base_limit=480)


def number(value, label, low, high, integer=False):
    try:
        if isinstance(value, bool):
            raise ValueError()
        d = Decimal(unicodedata.normalize('NFKC', str(value)).strip())
        if not d.is_finite() or not Decimal(str(low)) <= d <= Decimal(str(high)):
            raise ValueError()
        if integer and d != d.to_integral_value():
            raise ValueError()
        return int(d) if integer else float(d)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        unit = '整数' if integer else '数值'
        raise ValueError(f'{label}请输入 {low:g}–{high:g} 的{unit}') from None


def profile(dpi, sensitivity, base_limit=480):
    return dict(schema=1, dpi=number(dpi, 'DPI', 100, 64000, True),
                sensitivity=number(sensitivity, '游戏灵敏度', .01, 10),
                base_limit=number(base_limit, '基础位移上限', 10, 480, True))


def stored_profile(data):
    """Schema 1 remains readable; optional UI calibration/identity is explicit."""
    clean = profile(data['dpi'], data['sensitivity'], data['base_limit'])
    if data.get('manual_gain') is not None:
        clean['manual_gain'] = number(data['manual_gain'], '手动位移增益', .05, 8)
    if data.get('basis_level') is not None:
        clean['basis_level'] = number(data['basis_level'], '基准档位', 1, 6, True)
    return clean


@dataclass(frozen=True)
class Adapted:
    dpi: int
    sensitivity: float
    width: int
    edpi: float
    sensitivity_ratio: float
    resolution_ratio: float
    gain: float
    step_limit: int
    base_limit: int
    limit_capped: bool
    warning: str


def calculate(dpi, sensitivity, width, base_limit=480, reference=REFERENCE):
    p = profile(dpi, sensitivity, base_limit)
    width = number(width, '显示器宽度', 320, 16384, True)
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (reference.dpi, reference.sensitivity, reference.width, reference.gain))
            or min(reference.dpi, reference.sensitivity, reference.width, reference.gain) <= 0):
        raise ValueError('V5 换算基准无效')
    sensitivity_ratio = reference.sensitivity / p['sensitivity']
    resolution_ratio = reference.width / width
    gain = reference.gain * sensitivity_ratio * resolution_ratio
    if not .05 <= gain <= 8:
        raise ValueError('当前组合超出 V5 增益范围（0.05–8）；未应用，请调整灵敏度或另做标定')
    desired_limit = round(p['base_limit'] * sensitivity_ratio)
    step_limit = min(480, max(10, desired_limit))
    capped = desired_limit != step_limit
    warning = '位移上限已限制在 V5 的 10–480 计数范围，极端组合下速度可能受限。' if capped else ''
    return Adapted(p['dpi'], p['sensitivity'], width, p['dpi'] * p['sensitivity'],
                   sensitivity_ratio, resolution_ratio, round(gain, 6), step_limit,
                   p['base_limit'], capped, warning)


def load_profile(path):
    try:
        data = json.loads(Path(path).read_text('utf-8-sig'))
        if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
            raise ValueError('不支持的适配配置')
        return stored_profile(data), None
    except FileNotFoundError:
        return dict(DEFAULT_PROFILE), None
    except (OSError, ValueError, TypeError, KeyError):
        return dict(DEFAULT_PROFILE), '适配配置损坏，已恢复 800 DPI / 0.3；跟随仍默认关闭。'


def save_profile(path, data):
    """Persist inputs/base budget, never the previously scaled result."""
    clean = stored_profile(data)
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction():
        raise ValueError('不向链接配置路径写入')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backups = path.parent / '.backups'
        backups.mkdir(exist_ok=True)
        shutil.copy2(path, backups / (path.name + '.' + uuid.uuid4().hex[:12] + '.bak'))
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return clean
