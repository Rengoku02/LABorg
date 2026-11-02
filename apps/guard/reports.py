"""What the guard has found, for the go-live decision (plan §9 "Go-live, per tier").

A tier (or one kind of match in a tier, e.g. T1 against open requests) may go live once the
co-owner has labelled at least ``guard.go_live_min_labelled`` of its matches and at least
``guard.go_live_min_precision`` of those were true duplicates. Precision is true_dup over
everything labelled, as the baseline's reviewed figure counts it. A pair labelled in both
the replay and the live shadow counts once.
"""

import math
from collections import Counter
from dataclasses import dataclass

from apps.core.policies import get_policy

from .matcher import KIND_ORDER, MATCHER_VERSION, TIER_ORDER, UNCHECKABLE_REASONS
from .models import DuplicateCheck, DuplicateMatch


@dataclass(frozen=True)
class TierPrecision:
    tier: str
    kind: str  # "" for the whole tier, else stock / open / bought
    matches: int
    labelled: int
    true_dup: int
    precision: float | None
    # The lower end of the 95% Wilson interval: how sure the precision is (information
    # only; the gate is the plan's plain threshold).
    precision_low: float | None
    min_labelled: int
    min_precision: float
    gate_ok: bool

    @property
    def name(self):
        return f"{self.tier}/{self.kind}" if self.kind else self.tier


def precision_report(lab, *, replay=None, matcher_version=MATCHER_VERSION):
    """Per tier, then per tier and kind: matches found, labelled, precision and the gate.
    ``replay``: True or False for only replay or only live checks; None for both."""
    min_labelled = get_policy("guard.go_live_min_labelled", lab)
    min_precision = get_policy("guard.go_live_min_precision", lab)
    matches = DuplicateMatch.objects.filter(
        duplicate_check__lab=lab,
        duplicate_check__matcher_version=matcher_version,
        suppressed=False,
    )
    if replay is not None:
        matches = matches.filter(duplicate_check__replay=replay)
    found, labels = Counter(), {}
    for row in matches.values(
        "tier",
        "candidate_kind",
        "label",
        "duplicate_check__order_request_id",
        "candidate_stock_item_id",
        "candidate_order_request_id",
    ):
        group = (row["tier"], row["candidate_kind"])
        found[group] += 1
        if row["label"]:
            pair = (
                row["duplicate_check__order_request_id"],
                row["candidate_stock_item_id"],
                row["candidate_order_request_id"],
            )
            labels[pair] = (group, row["label"])
    labelled, true_dup = Counter(), Counter()
    for group, label in labels.values():
        labelled[group] += 1
        true_dup[group] += label == DuplicateMatch.Label.TRUE_DUP
    rows = []
    for tier in TIER_ORDER:
        groups = [(tier, kind) for kind in KIND_ORDER]
        if not any(found[g] for g in groups):
            continue
        rows.append(_row(tier, "", groups, found, labelled, true_dup, min_labelled, min_precision))
        for group in groups:
            if found[group]:
                rows.append(
                    _row(tier, group[1], [group], found, labelled, true_dup, min_labelled,
                         min_precision)
                )  # fmt: skip
    return rows


def _row(tier, kind, groups, found, labelled, true_dup, min_labelled, min_precision):
    n = sum(labelled[g] for g in groups)
    k = sum(true_dup[g] for g in groups)
    precision = k / n if n else None
    return TierPrecision(
        tier=tier,
        kind=kind,
        matches=sum(found[g] for g in groups),
        labelled=n,
        true_dup=k,
        precision=precision,
        precision_low=wilson_low(k, n),
        min_labelled=min_labelled,
        min_precision=min_precision,
        gate_ok=n >= min_labelled and precision is not None and precision >= min_precision,
    )


def wilson_low(successes, n, z=1.96):
    if not n:
        return None
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z * z / n))


def uncheckable_share(lab, *, replay=False, matcher_version=MATCHER_VERSION):
    """{"checked": n, "uncheckable": n, "share": float|None, "by_reason": {reason: n}} over
    the checks of this matcher version; "unresolved" is reported apart (it is transient)."""
    checks = DuplicateCheck.objects.filter(lab=lab, replay=replay, matcher_version=matcher_version)
    by_status = Counter(checks.values_list("status", flat=True))
    reasons = Counter(
        checks.filter(status=DuplicateCheck.Status.UNCHECKABLE).values_list(
            "uncheckable_reason", flat=True
        )
    )
    unresolved = reasons.pop("unresolved", 0)
    total = sum(by_status.values()) - unresolved
    uncheckable = sum(reasons.values())
    return {
        "checked": total,
        "uncheckable": uncheckable,
        "share": uncheckable / total if total else None,
        "by_reason": {reason: reasons.get(reason, 0) for reason in UNCHECKABLE_REASONS},
        "unresolved": unresolved,
        "by_status": dict(by_status),
    }
