"""Pure aim-region policy; no native input, model, capture or image imports.

Chest is estimated from a detected BODY box, not extrapolated from a head.
Strict filtering marks body boxes support-only and linked verified HEADS eligible.
Chest mode may reuse that exact joint evidence for the same-frame body box.
"""
import math
from outline_settings import VERIFIED_OUTLINE_REASONS

AIM_REGIONS = {
    'original': '原定位（保持原行为）',
    'chest': '胸部定位（身体框估算）',
}
CHEST_HEIGHT_RATIO = .24
CHEST_TUNING_REVISION = "chest-tuning-20261109"
# Dampen box jitter without changing gain, response time or output budgets.
CHEST_FILTER_CONFIG = dict(min_cutoff=5., beta=.02, derivative_cutoff=4.)
AIM_REGION_POLICY_REVISION = "strict-joint-evidence-20261108"


def validate_region(region):
    if not isinstance(region, str) or region not in AIM_REGIONS:
        raise ValueError('定位部位无效，请选择原定位或胸部定位')
    return region


def _box_key(box):
    try:
        values = tuple(float(value) for value in box)
        if (len(values) != 4 or not all(math.isfinite(value) for value in values)
                or values[2] <= values[0] or values[3] <= values[1]):
            return None
        return values
    except (TypeError, ValueError, OverflowError):
        return None


def chest_candidates(rows):
    """Reuse strict head/body JOINT validation, never body outline alone.

    The strict filter deliberately keeps body.target_eligible=False. Do not
    change that global head-targeting contract. Promote a COPY only when a
    verified eligible head links to this exact verified body in the same rows.
    Non-strict/legacy direct body rows retain their existing eligibility.
    """
    verified_links = {}
    for head in rows:
        if (head.get('label') != 'enemy_head' or head.get('target_eligible') is not True
                or head.get('filter_reason') not in VERIFIED_OUTLINE_REASONS
                or head.get('outline_evidence', {}).get('passed') is not True
                or head.get('team') in ('ally', 'friendly', 'teammate')
                or head.get('visible') is False or head.get('occluded') is True):
            continue
        key = _box_key(head.get('linked_body_box'))
        try:
            score = float(head['confidence'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if key is not None and math.isfinite(score) and score > 0:
            verified_links[key] = max(verified_links.get(key, 0), score)
    selected = []
    for body in rows:
        if body.get('label') != 'enemy':
            continue
        if body.get('target_eligible') is not False:
            selected.append(body)
            continue
        key = _box_key(body.get('box'))
        if (key not in verified_links or body.get('filter_reason') != 'body_support_only'
                or body.get('outline_evidence', {}).get('passed') is not True):
            continue
        try:
            score = float(body['confidence'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(score):
            continue
        selected.append(dict(body, target_eligible=True,
                             confidence=min(score, verified_links[key]),
                             filter_reason='verified_head_body_chest',
                             chest_validation='same_frame_head_body_joint'))
    return selected


def selection_policy(rows, region, aim_height_ratio, prefer_head):
    validate_region(region)
    if region == 'original':
        return rows, aim_height_ratio, prefer_head
    return chest_candidates(rows), CHEST_HEIGHT_RATIO, False
