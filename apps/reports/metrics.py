"""Lab-level numbers for the dashboard and the PI summary (plan §11; design §10.8).

Lab totals only: nothing here is broken down by person (no per-person duplicate counts, no
searches, no alert answers), and the templates never name a requester.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q
from django.utils import timezone

from apps.core import schedules
from apps.guard.models import DuplicateCheck, DuplicateMatch
from apps.guard.outcomes import value_avoided
from apps.guard.reports import precision_report, uncheckable_share
from apps.inventory.models import AskReply, AskThread, HistoryClaim, Location
from apps.quartzy.presenters import request_value

MONEY = Decimal("0.01")


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    label: str

    def year_before(self):
        start = self.start.replace(year=self.start.year - 1)
        label = self.label
        if label.startswith("Q") and label.endswith(str(self.start.year)):
            label = f"{label.rsplit(' ', 1)[0]} {start.year}"  # "Q4 2026" -> "Q4 2025"
        else:
            label = f"{label}, a year earlier"
        return Period(start, self.end.replace(year=self.end.year - 1), label)


def quarter(lab, day=None):
    """The calendar quarter (lab time) containing ``day`` (default: today)."""
    local = schedules.local_now(lab) if day is None else day
    first_month = 3 * ((local.month - 1) // 3) + 1
    zone = schedules.zone(lab.time_zone)
    start = datetime.combine(local.date().replace(month=first_month, day=1), time.min, zone)
    end_month = first_month + 3
    end_year = start.year + (end_month > 12)
    end = start.replace(year=end_year, month=(end_month - 1) % 12 + 1)
    return Period(start, end, f"Q{(first_month - 1) // 3 + 1} {start.year}")


def last_week(lab, now=None):
    end = now or timezone.now()
    return Period(end - timedelta(days=7), end, "the last 7 days")


def duplicate_spend(lab, period):
    """{"requests", "amount", "reviewed_requests", "reviewed_amount"}: requests made in the
    period that matched something earlier (each counted once, at its own price), and of
    those the ones the co-owner labelled the same thing. From live and replay checks."""
    checks = DuplicateCheck.objects.filter(
        lab=lab,
        order_request__requested_at__gte=period.start,
        order_request__requested_at__lt=period.end,
    ).select_related("order_request")
    found, reviewed = {}, {}
    for check in checks:
        request = check.order_request
        matches = check.matches.filter(suppressed=False)
        if not matches.exists():
            continue
        value = request_value(request) or Decimal(0)
        found[request.pk] = value
        if matches.filter(label=DuplicateMatch.Label.TRUE_DUP).exists():
            reviewed[request.pk] = value
    return {
        "requests": len(found),
        "amount": sum(found.values(), Decimal(0)).quantize(MONEY),
        "reviewed_requests": len(reviewed),
        "reviewed_amount": sum(reviewed.values(), Decimal(0)).quantize(MONEY),
    }


def alert_in_time_rate(lab, period):
    answered = DuplicateCheck.objects.filter(
        lab=lab,
        replay=False,
        created_at__gte=period.start,
        created_at__lt=period.end,
        alert_in_time__isnull=False,
    ).aggregate(total=Count("pk"), in_time=Count("pk", filter=Q(alert_in_time=True)))
    return _rate(answered["in_time"], answered["total"])


def search_before_buy_rate(lab, period):
    known = DuplicateCheck.objects.filter(
        lab=lab,
        replay=False,
        created_at__gte=period.start,
        created_at__lt=period.end,
        searched_before__isnull=False,
    ).exclude(status__in=(DuplicateCheck.Status.UNCHECKABLE, DuplicateCheck.Status.OUT_OF_SCOPE))
    counts = known.aggregate(
        total=Count("pk"), searched=Count("pk", filter=Q(searched_before=True))
    )
    return _rate(counts["searched"], counts["total"])


def coverage(lab):
    """Claims answered, and per freezer (or fridge, dewar) how many boxes have an owner."""
    claims = HistoryClaim.objects.for_lab(lab)
    total = claims.count()
    answered = claims.exclude(outcome=HistoryClaim.Outcome.PENDING).count()
    units = (
        Location.objects.for_lab(lab)
        .physical()
        .filter(active=True, location_type__key__in=("freezer", "fridge", "ln2_dewar"))
        .order_by("path")
    )
    freezers = []
    for unit in units:
        boxes = Location.objects.filter(
            lab=lab, active=True, path__startswith=unit.path, location_type__structural=False
        ).exclude(pk=unit.pk)
        count = boxes.count()
        owned = boxes.filter(default_custodian__isnull=False).count()
        freezers.append({"name": unit.display_name, "boxes": count, "owned": owned})
    return {
        "claims_total": total,
        "claims_answered": answered,
        "claims_share": _rate(answered, total),
        "freezers": freezers,
    }


def asks_borrowed(lab, period):
    """Asks started in the period, and how many ended in a lend or an "I have it"."""
    threads = AskThread.objects.for_lab(lab).filter(
        created_at__gte=period.start, created_at__lt=period.end
    )
    positive = threads.filter(
        replies__answer__in=(
            AskReply.Answer.LEND,
            AskReply.Answer.HAVE_IT,
            AskReply.Answer.SHARE_OK,
        )
    ).distinct()
    return {"asks": threads.count(), "borrowed": positive.count()}


def overview(lab, period):
    compare = period.year_before()
    saved = value_avoided(lab, period.start, period.end)
    return {
        "period": period,
        "spend": duplicate_spend(lab, period),
        "spend_last_year": duplicate_spend(lab, compare),
        "compare": compare,
        "avoided": saved,
        "alert_in_time": alert_in_time_rate(lab, period),
        "search_before_buy": search_before_buy_rate(lab, period),
        "coverage": coverage(lab),
        "asks": asks_borrowed(lab, period),
        "tiers": precision_report(lab),
        "uncheckable": _uncheckable(lab),
    }


def _uncheckable(lab):
    """The live checks' uncheckable share; the replay's until there are live checks."""
    share = uncheckable_share(lab)
    if share["checked"]:
        return {**share, "source": "live"}
    return {**uncheckable_share(lab, replay=True), "source": "replay"}


def _rate(part, whole):
    return part / whole if whole else None


def small(number):
    """A count for PI-facing text: 1 or 2 shows as "<3", so nobody is singled out."""
    return "<3" if 0 < number < 3 else str(number)
