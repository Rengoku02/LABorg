"""Tunable thresholds, with code defaults and per-lab overrides in ``PolicySetting``.

Every number the guard, the sync and the email budget depend on lives here, so it can be
tuned from the admin without a deploy (plan §6/§9).
"""

from django.db.models import Q

from .models import PolicySetting

DEFAULTS = {
    # Alerts (plan §9 "Safety"). Off until the shadow-mode gate is passed tier by tier.
    "alerts.enabled": False,
    "alerts.tiers_live": [],
    "alerts.late_after_hours": 6,
    "alerts.burst_max_requests": 8,
    "alerts.burst_window_minutes": 15,
    "alerts.circuit_breaker_max": 10,
    "alerts.clear_to_order_until": None,  # ISO date; "No match — clear to order" receipts
    # Guard candidates and noise control.
    "guard.probably_bought_days": 14,
    "guard.open_probably_arrived_days": 60,
    "guard.recurring_per_year": 6,
    "guard.cross_family_min_key_length": 7,
    "guard.cross_family_min_name_similarity": 0.2,
    "guard.go_live_min_labelled": 30,
    "guard.go_live_min_precision": 0.85,
    "guard.include_same_requester": False,
    "guard.need_own_suppress_days": 30,
    "guard.not_same_suppress_days": 180,
    # Inventory (docs/design/mvp-a.md §2 and §3).
    "inventory.default_visibility": "ask_first",  # shared / ask_first / restricted
    "inventory.quartzy_split_max": 20,
    # Days after which stock events stop naming who did them (None: the PI hasn't decided).
    "inventory.pseudonymize_after_days": None,
    # Nudges and claims.
    "nudges.stale_days": 21,
    "claims.after_receipt_days": 7,
    "claims.after_request_days": 21,
    "claims.lookback_months": 24,
    "claims.batch_size": 15,
    "claims.max_rounds": 3,  # how many weekly rounds a "later" comes back
    # Asks and the email budget. The ask cap holds both ways: one lab-wide ask started per
    # person per day, and at most one batched ask email received per person per day.
    "asks.max_per_person_per_day": 1,
    "asks.batch_hours": [10, 15],  # lab-local hours at which held asks go out
    "email.max_non_alert_per_week": 3,
    # Search before you buy.
    "search.bought_before_months": 24,
    "search.searched_before_days": 14,  # a search this long before a request counts
    # Who opens the lab-totals dashboard: "leads" (PI, co-owners, system owners) or "members".
    "reports.dashboard_audience": "leads",
    # Quartzy sync cadence (plan §8).
    "sync.lab_hours": [7, 20],
    "sync.head_interval_lab_hours_minutes": 2,
    "sync.head_interval_off_hours_minutes": 10,
    "sync.head_max_pages": 5,
    "sync.sweep_pages_per_cycle": 3,
    "sync.max_404_fraction": 0.05,
    "sync.max_tombstones_per_day": 50,
    # Privacy.
    "search.log_retention_days": 30,
}


def get_policy(key, lab=None):
    if key not in DEFAULTS:
        raise KeyError(f"Unknown policy {key!r}; add it to apps.core.policies.DEFAULTS")
    scope = Q(lab__isnull=True) | Q(lab=lab) if lab is not None else Q(lab__isnull=True)
    rows = {row.lab_id: row.value for row in PolicySetting.objects.filter(scope, key=key)}
    if lab is not None and lab.pk in rows:
        return rows[lab.pk]
    if None in rows:
        return rows[None]
    return DEFAULTS[key]


def set_policy(key, value, lab=None, user=None, note=""):
    if key not in DEFAULTS:
        raise KeyError(f"Unknown policy {key!r}")
    row, _ = PolicySetting.objects.update_or_create(
        lab=lab, key=key, defaults={"value": value, "updated_by": user, "note": note}
    )
    return row
