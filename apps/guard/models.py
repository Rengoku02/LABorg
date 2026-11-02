"""The duplicate guard's records (docs/design/mvp-a.md §5).

- ``CheckRun``: one batch of checks (a sync run's announced requests, the safety net, a
  replay). It is the "cycle" the circuit breaker counts alerts in.
- ``DuplicateCheck``: one request checked by one matcher version, live or as a replay of
  history; its status says what happened (never alerted while the guard is in shadow).
- ``DuplicateMatch``: one earlier thing the request matched: a stock item, or another
  request (open, or bought before), with its tier and what was shown. The co-owner labels
  matches; per-tier precision decides when a tier may go live (plan §9).
- ``Outcome``: what the requester (or the sync) did about an alerted check.
- ``PairSuppression``: "not the same thing" / "need my own", so the pair isn't flagged again.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

TIERS = (
    ("T1", "T1: same catalog #"),
    ("T1x", "T1x: same # at another vendor"),
    ("T2", "T2: same product, other pack or listing"),
    ("T3", "T3: shared identifier"),
    ("T4", "T4: similar name"),
)


class CheckRunQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)


class CheckRun(models.Model):
    class Via(models.TextChoices):
        HEAD = "head", "Head probe"
        SWEEP = "sweep", "Rolling sweep"
        HOT = "hot", "Hot set"
        RESOLVE = "resolve", "Re-resolution"
        SAFETY_NET = "safety_net", "Safety net"
        REPLAY = "replay", "Replay of history"
        MANUAL = "manual", "Manual"

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="check_runs")
    via = models.CharField(max_length=20, choices=Via.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    requests = models.PositiveIntegerField(default=0)
    checks_created = models.PositiveIntegerField(default=0)
    alerts_planned = models.PositiveIntegerField(default=0)
    # The circuit breaker holds every alert of a run that planned too many (design §7.6).
    held = models.BooleanField(default=False)
    hold_reason = models.CharField(max_length=200, blank=True)

    objects = CheckRunQuerySet.as_manager()

    class Meta:
        indexes = [models.Index(fields=["lab", "started_at"], name="guard_run_lab_started")]

    def __str__(self):
        return f"{self.get_via_display()} {self.started_at:%Y-%m-%d %H:%M} ({self.requests})"


class DuplicateCheckQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)

    def live(self):
        return self.filter(replay=False)


class DuplicateCheck(models.Model):
    class Status(models.TextChoices):
        NO_MATCH = "no_match", "No match"
        UNCHECKABLE = "uncheckable", "Can't be checked"
        OUT_OF_SCOPE = "out_of_scope", "Type not in scope"
        SUPPRESSED = "suppressed", "Every match suppressed"
        UNARMED = "unarmed", "Before alerts were armed"
        LATE = "late", "Seen too late for an alert"
        HELD = "held", "Alert held"
        ALERTED = "alerted", "Alerted"
        SHADOW_ONLY = "shadow_only", "Shadow only (not sent)"
        CLEAR = "clear", "Clear to order sent"

    # Statuses with matches someone should see (labelling, digests).
    WITH_MATCHES = (
        Status.UNARMED,
        Status.LATE,
        Status.HELD,
        Status.ALERTED,
        Status.SHADOW_ONLY,
    )

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="duplicate_checks")
    order_request = models.ForeignKey(
        "quartzy.OrderRequest", on_delete=models.CASCADE, related_name="duplicate_checks"
    )
    run = models.ForeignKey(
        CheckRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="checks"
    )
    matcher_version = models.PositiveIntegerField()
    replay = models.BooleanField(default=False)
    # Not sent as a live alert: shadow mode, a tier not live yet, or a replay.
    shadow = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    uncheckable_reason = models.CharField(max_length=30, blank=True)
    hold_reason = models.CharField(max_length=200, blank=True)
    late = models.BooleanField(default=False)
    best_tier = models.CharField(max_length=4, choices=TIERS, blank=True)
    # Would Quartzy's own warning have fired? Raw: the same catalog # as typed among open
    # requests of the same Quartzy lab; norm: the same normalized key (design §5).
    quartzy_native_would_fire_raw = models.BooleanField(default=False)
    quartzy_native_would_fire_norm = models.BooleanField(default=False)
    # Did the requester search LabStock for it first? (step 8; None until known)
    searched_before = models.BooleanField(null=True, blank=True)
    # Did the alert reach them before they placed the vendor order? (None until answered)
    alert_in_time = models.BooleanField(null=True, blank=True)
    # The moment the check reflects: when it ran, or the request's time for a replay.
    as_of = models.DateTimeField()
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(null=True, blank=True)

    objects = DuplicateCheckQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order_request", "matcher_version", "replay"],
                name="guard_check_unique_request_version_replay",
            ),
        ]
        indexes = [
            models.Index(fields=["lab", "replay", "created_at"], name="guard_check_lab_replay"),
            models.Index(fields=["status"], name="guard_check_status"),
        ]

    def __str__(self):
        kind = "replay" if self.replay else "live"
        return f"Check of {self.order_request} ({kind}, {self.get_status_display()})"


class DuplicateMatch(models.Model):
    class Kind(models.TextChoices):
        STOCK = "stock", "In the lab"
        OPEN = "open", "On order"
        BOUGHT = "bought", "Bought before"

    class Label(models.TextChoices):
        TRUE_DUP = "true_dup", "Same thing: a duplicate"
        RELATED = "related_not_dup", "Related, not a duplicate"
        FALSE = "false", "Different thing"

    # Not "check": that would hide Django's Model.check().
    duplicate_check = models.ForeignKey(
        DuplicateCheck, on_delete=models.CASCADE, related_name="matches"
    )
    tier = models.CharField(max_length=4, choices=TIERS)
    candidate_kind = models.CharField(max_length=10, choices=Kind.choices)
    score = models.FloatField()
    # Unverified stock ("probably: ask X"), or an open request old enough to have arrived.
    probably = models.BooleanField(default=False)
    why = models.CharField(max_length=300, blank=True)
    candidate_stock_item = models.ForeignKey(
        "inventory.StockItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="duplicate_matches",
    )
    candidate_order_request = models.ForeignKey(
        "quartzy.OrderRequest",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="duplicate_matches",
    )
    # What the match looked like when it was found (product, catalog #, holder, dates), as
    # presented to the system: shown on the labelling page even after the stock moved on.
    candidate_snapshot = models.JSONField(default=dict, blank=True)
    suppressed = models.BooleanField(default=False)
    suppression_reason = models.CharField(max_length=40, blank=True)
    label = models.CharField(max_length=20, choices=Label.choices, blank=True)
    labelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    labelled_at = models.DateTimeField(null=True, blank=True)
    # A requester's "not the same thing" is a suggestion for the co-owner, never a label.
    label_suggestion = models.CharField(max_length=20, choices=Label.choices, blank=True)

    class Meta:
        ordering = ["-score", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(candidate_stock_item__isnull=False, candidate_order_request__isnull=True)
                    | Q(candidate_stock_item__isnull=True, candidate_order_request__isnull=False)
                ),
                name="guard_match_exactly_one_candidate",
            ),
            models.UniqueConstraint(
                fields=["duplicate_check", "candidate_stock_item"],
                condition=Q(candidate_stock_item__isnull=False),
                name="guard_match_unique_stock",
            ),
            models.UniqueConstraint(
                fields=["duplicate_check", "candidate_order_request"],
                condition=Q(candidate_order_request__isnull=False),
                name="guard_match_unique_request",
            ),
        ]
        indexes = [models.Index(fields=["tier", "label"], name="guard_match_tier_label")]

    def __str__(self):
        return f"{self.tier} {self.get_candidate_kind_display()} match ({self.duplicate_check_id})"


class Outcome(models.Model):
    class Kind(models.TextChoices):
        BORROW_REQUESTED = "borrow_requested", "Asked to borrow"
        JOIN_REQUESTED = "join_requested", "Asked to share the order"
        CANCEL_INTENDED = "cancel_intended", "Will cancel the request"
        VENDOR_CANCEL_INTENDED = "vendor_cancel_intended", "Will cancel with the vendor"
        VENDOR_CANCEL_CONFIRMED = "vendor_cancel_confirmed", "Cancelled with the vendor"
        KEPT_OFFER_SPARE = "kept_offer_spare", "Kept it, offers the spare"
        NOT_SAME = "not_same", "Not the same thing"
        NEED_OWN = "need_own", "Needs their own"
        BORROWED = "borrowed", "Borrowed"
        CANCELLED = "cancelled", "Cancelled (seen in Quartzy)"
        JOINED_ORDER = "joined_order", "Shared the order"

    # Outcomes that avoided a purchase, once verified (design §8 "$ avoided").
    AVOIDED = (
        Kind.CANCELLED,
        Kind.VENDOR_CANCEL_CONFIRMED,
        Kind.BORROWED,
        Kind.JOINED_ORDER,
    )

    duplicate_check = models.ForeignKey(
        DuplicateCheck, on_delete=models.CASCADE, related_name="outcomes"
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    reason = models.CharField(max_length=30, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    decided_at = models.DateTimeField(default=timezone.now)
    verified_at = models.DateTimeField(null=True, blank=True)
    est_value_avoided = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    notification = models.ForeignKey(
        "core.Notification",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["duplicate_check", "kind"], name="guard_outcome_unique_kind"
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.duplicate_check_id})"


class PairSuppressionQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)

    def active(self, now=None):
        now = now or timezone.now()
        return self.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))


class PairSuppression(models.Model):
    """Don't flag ``subject_product`` against ``candidate_product`` (either way round) for
    the requester, or for the whole lab, until it expires (design §5, §6)."""

    class Scope(models.TextChoices):
        REQUESTER = "requester", "This requester"
        LAB = "lab", "Whole lab"

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="+")
    subject_product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE, related_name="+"
    )
    candidate_product = models.ForeignKey(
        "catalog.Product", on_delete=models.CASCADE, related_name="+"
    )
    scope = models.CharField(max_length=10, choices=Scope.choices, default=Scope.REQUESTER)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = PairSuppressionQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(scope="requester") | Q(requester__isnull=False),
                name="guard_pairsuppression_requester_scope_needs_requester",
            ),
        ]

    def __str__(self):
        return f"{self.subject_product_id} ~ {self.candidate_product_id} ({self.scope})"
