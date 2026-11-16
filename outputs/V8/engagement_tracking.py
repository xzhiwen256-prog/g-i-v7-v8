"""Bounded, conservative scene correspondence for retaining a selected candidate.

This is geometric correspondence, not a player ID, a kill detector, or a measured
camera transform.  It neither emits input nor modifies a motion predictor.  The
caller must observe only confirmed selections and must pause on uncertainty.
"""
import math


_MAX_CANDIDATES = 16
_MAX_INPUT_RECORDS = 64
_MAX_TRANSLATION = 120.0
_AMBIGUITY = 8.0
_MAX_PAIR_CHECKS = 4096
_MAX_ASSIGNMENT_WORK = 16384
_UNMATCHED = 10000.0
_INVALID = 10000000.0


def _finite_stamp(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _normalize(target):
    try:
        point = tuple(float(target["point"][i]) for i in (0, 1))
        row = target["row"]
        box = tuple(float(row["box"][i]) for i in range(4))
        label, source = row["label"], target["aim_source"]
        if not isinstance(label, str) or not isinstance(source, str):
            return None
        if not all(math.isfinite(value) for value in point + box):
            return None
        width, height = box[2] - box[0], box[3] - box[1]
        if width <= 0.0 or height <= 0.0:
            return None
        diagonal = math.hypot(width, height)
        if not math.isfinite(diagonal):
            return None
        return dict(point=point, box=box, label=label, source=source,
                    width=width, height=height,
                    gate=max(6.0, min(12.0, diagonal * .4)),
                    key=(label, source, point, box), target=target)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return None


def _bounded(candidates, preferred=None):
    # At most 16 normalized records are retained, including the confirmed lock.
    # Overflow is never silently trusted as a complete scene.
    records, seen, overflow = [], set(), False
    try:
        iterator = iter(candidates)
    except TypeError:
        return [], False
    for index, candidate in enumerate(iterator):
        if index >= _MAX_INPUT_RECORDS:
            overflow = True
            break
        item = _normalize(candidate)
        if item is None:
            continue
        key = item["key"]
        if key in seen:
            continue
        if len(records) < _MAX_CANDIDATES:
            records.append(item)
            seen.add(key)
        else:
            overflow = True
            if preferred is not None and key == preferred:
                removed = records[-1]
                seen.discard(removed["key"])
                records[-1] = item
                seen.add(key)
    records.sort(key=lambda item: item["key"])
    return records, overflow


def _compatible(old, new):
    return (old["label"] == new["label"] and old["source"] == new["source"]
            and .5 <= new["width"] / old["width"] <= 2.0
            and .5 <= new["height"] / old["height"] <= 2.0)


def _assignment(costs, forbidden=None):
    """Maximum-cardinality, minimum-cost rectangular one-to-one assignment."""
    count, current_count = len(costs), len(costs[0])
    columns = current_count + count  # One unmatched dummy per old candidate.
    matrix = []
    for i, row in enumerate(costs):
        matrix.append([
            (_INVALID if value is None or forbidden == (i, j) else value)
            for j, value in enumerate(row)
        ] + [_UNMATCHED] * count)
    # Rectangular Hungarian algorithm; all sizes are <= 16 x 32.
    u, v = [0.0] * (count + 1), [0.0] * (columns + 1)
    owner, way = [0] * (columns + 1), [0] * (columns + 1)
    for i in range(1, count + 1):
        owner[0] = i
        j0, minimum, used = 0, [float("inf")] * (columns + 1), [False] * (columns + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = owner[j0], float("inf"), 0
            for j in range(1, columns + 1):
                if not used[j]:
                    value = matrix[i0 - 1][j - 1] - u[i0] - v[j]
                    if value < minimum[j]:
                        minimum[j], way[j] = value, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(columns + 1):
                if used[j]:
                    u[owner[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if owner[j0] == 0:
                break
        while True:
            j1 = way[j0]
            owner[j0] = owner[j1]
            j0 = j1
            if j0 == 0:
                break
    mapping = [None] * count
    for j in range(1, current_count + 1):
        if owner[j] and matrix[owner[j] - 1][j - 1] < _UNMATCHED:
            mapping[owner[j] - 1] = j - 1
    total = sum(costs[i][j] for i, j in enumerate(mapping) if j is not None)
    return mapping, sum(j is not None for j in mapping), total


class EngagementTracker:
    """Retain one confirmed candidate using the surrounding candidate layout.

    observe() stores detached geometry, never predicted/extrapolated boxes.
    match() returns the original object in its current candidates argument.
    It does not advance the reference; call observe() after accepting a match.
    Timestamps must be finite and strictly newer than the confirmed reference.
    """
    MAX_CANDIDATES = _MAX_CANDIDATES
    MAX_INPUT_RECORDS = _MAX_INPUT_RECORDS
    MAX_TRANSLATION = _MAX_TRANSLATION
    AMBIGUITY = _AMBIGUITY
    MAX_PAIR_CHECKS = _MAX_PAIR_CHECKS
    MAX_ASSIGNMENT_WORK = _MAX_ASSIGNMENT_WORK

    def __init__(self):
        self.reset()

    def reset(self):
        self._scene = ()
        self._locked_index = None
        self._stamp = None
        self._overflow = False
        self._frames = 0

    def snapshot(self):
        return dict(candidates_count=len(self._scene), stamp=self._stamp,
                    locked_index=self._locked_index, truncated=self._overflow,
                    frames=self._frames)

    def observe(self, candidates, locked, stamp):
        stamp = _finite_stamp(stamp)
        selected = _normalize(locked)
        if (stamp is None or selected is None
                or (self._stamp is not None and stamp <= self._stamp)):
            return False
        scene, overflow = _bounded(candidates, selected["key"])
        indices = [i for i, item in enumerate(scene) if item["key"] == selected["key"]]
        if len(indices) != 1:
            return False
        # Do not keep caller-owned dictionaries/box lists after observation.
        self._scene = tuple({key: value for key, value in item.items() if key != "target"}
                            for item in scene)
        self._locked_index, self._stamp = indices[0], stamp
        self._overflow = overflow
        self._frames += 1
        return True

    @staticmethod
    def _result(status, reason, support=0, target=None, translation=None, cost=None):
        return dict(status=status, target=target, translation=translation,
                    reason=reason, support=support, cost=cost)

    def match(self, candidates, stamp):
        if not self._scene or self._locked_index is None:
            return self._result("unavailable", "no_confirmed_scene")
        stamp = _finite_stamp(stamp)
        if stamp is None or stamp <= self._stamp:
            return self._result("uncertain", "non_increasing_stamp")
        current, overflow = _bounded(candidates)
        if self._overflow or overflow:
            return self._result("uncertain", "candidate_limit")
        if len(self._scene) == 1 and len(current) == 1:
            return self._result("unavailable", "single_candidate_scene")
        if len(self._scene) < 2 or len(current) < 2:
            return self._result("uncertain", "insufficient_scene_support")

        old = self._scene
        # Every hypothesis comes from an actually observed compatible pair.
        displacements, hypotheses = {}, {}
        for i, previous in enumerate(old):
            for j, candidate in enumerate(current):
                if _compatible(previous, candidate):
                    dx = candidate["point"][0] - previous["point"][0]
                    dy = candidate["point"][1] - previous["point"][1]
                    if math.hypot(dx, dy) <= _MAX_TRANSLATION:
                        displacements[i, j] = (dx, dy)
                        # Merge only floating-point-equivalent translations; retain
                        # an actual observed vector rather than a quantized output.
                        key = (round(dx, 6), round(dy, 6))
                        if key in hypotheses:
                            hypotheses[key][0] += 1
                        else:
                            hypotheses[key] = [1, (dx, dy)]
        if not hypotheses:
            return self._result("uncertain", "no_compatible_translation")

        best_support, best_cost, plans = 1, float("inf"), []
        ordered = sorted(hypotheses.values(),
                         key=lambda item: (-item[0], math.hypot(*item[1]), item[1]))
        pairs = [(i, j, vector, min(old[i]["gate"], current[j]["gate"]) ** 2)
                 for (i, j), vector in displacements.items()]
        pair_checks, assignment_work = 0, 0
        assignment_cost = len(old) ** 2 * (len(current) + len(old))
        for _, translation in ordered:
            # Exhausting a deterministic work budget never accepts a partial
            # search: unseen hypotheses might disagree about the original lock.
            pair_checks += len(pairs)
            if pair_checks > _MAX_PAIR_CHECKS:
                return self._result("uncertain", "matching_budget",
                                    max(0, best_support if plans else 0))
            costs = [[None] * len(current) for _ in old]
            rows, columns = set(), set()
            for i, j, vector, gate_squared in pairs:
                dx, dy = vector[0] - translation[0], vector[1] - translation[1]
                error_squared = dx * dx + dy * dy
                if error_squared <= gate_squared:
                    costs[i][j] = math.sqrt(error_squared)
                    rows.add(i)
                    columns.add(j)
            if min(len(rows), len(columns)) < max(2, best_support):
                continue
            assignment_work += assignment_cost
            if assignment_work > _MAX_ASSIGNMENT_WORK:
                return self._result("uncertain", "matching_budget",
                                    max(0, best_support if plans else 0))
            mapping, support, cost = _assignment(costs)
            if support < max(2, best_support):
                continue
            if support > best_support:
                best_support, best_cost, plans = support, cost, []
            best_cost = min(best_cost, cost)
            if cost - best_cost >= _AMBIGUITY:
                continue
            plans = [plan for plan in plans if plan[0] - best_cost < _AMBIGUITY]
            plans.append((cost, mapping[self._locked_index], translation, mapping))
            locked_current = mapping[self._locked_index]
            if locked_current is not None:
                # Detect a second almost-equally-good ownership even when it
                # shares the same translation hypothesis (e.g. crossing boxes).
                assignment_work += assignment_cost
                if assignment_work > _MAX_ASSIGNMENT_WORK:
                    return self._result("uncertain", "matching_budget",
                                        best_support, cost=best_cost)
                alternative, alt_support, alt_cost = _assignment(
                    costs, forbidden=(self._locked_index, locked_current))
                if alt_support == support and alt_cost - best_cost < _AMBIGUITY:
                    plans.append((alt_cost, alternative[self._locked_index],
                                  translation, alternative))
            # With full cardinality, no later hypothesis can gain support or
            # cost less than zero.  Two ownerships each below the absolute
            # ambiguity margin therefore prove uncertainty immediately.
            if best_support == min(len(old), len(current)):
                certain_competitors = {plan[1] for plan in plans
                                       if plan[0] < _AMBIGUITY}
                if len(certain_competitors) > 1:
                    return self._result("uncertain", "ambiguous_scene",
                                        best_support, cost=best_cost)

        if not plans:
            return self._result("uncertain", "no_common_translation")
        plans.sort(key=lambda plan: (plan[0], plan[2]))
        best = plans[0]
        competitive = [plan for plan in plans if plan[0] - best[0] < _AMBIGUITY]
        ownership = {plan[1] for plan in competitive}
        if len(ownership) != 1:
            return self._result("uncertain", "ambiguous_scene", best_support, cost=best[0])
        locked_current = best[1]
        if locked_current is None:
            return self._result("uncertain", "original_not_supported", best_support,
                                cost=best[0])

        # Refine the reported translation only when all assigned pairs remain
        # within their gates.  It is an inferred layout shift, not HID/camera IO.
        vectors = [displacements[i, j] for i, j in enumerate(best[3]) if j is not None]
        translation = (sum(v[0] for v in vectors) / len(vectors),
                       sum(v[1] for v in vectors) / len(vectors))
        if (math.hypot(*translation) > _MAX_TRANSLATION
                or any(math.hypot(displacements[i, j][0] - translation[0],
                                  displacements[i, j][1] - translation[1])
                       > min(old[i]["gate"], current[j]["gate"])
                       for i, j in enumerate(best[3]) if j is not None)):
            translation = best[2]
        return self._result("matched", "common_translation", best_support,
                            current[locked_current]["target"], translation, best[0])
