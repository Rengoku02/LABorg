"""Django-Q2 schedules, owned by name prefix (``manage.py setup_schedules``).

Each app that schedules work registers a provider in its ``AppConfig.ready()``::

    schedules.register("guard.", guard_tasks.wanted_schedules)

A provider returns ``{name: values}`` for every schedule it wants right now. ``values`` are
Schedule fields (``func``, ``args``, ``schedule_type``, ``minutes``…) plus an optional
``first_run``: the ``next_run`` of a schedule when it is created (later runs follow its
type). ``sync_prefix`` creates what is missing, updates what changed and removes the
prefix's schedules nobody wants any more (a Quartzy lab switched off). A prefix only ever
touches its own schedules, so the Quartzy sync can't delete the guard's.

Lab-local times (a digest on Monday at 08:00 in the lab's time zone) don't map onto Q2's
schedule types across DST changes, and Q2 skips a missed DAILY run (``catch_up`` is off).
Such jobs run from an hourly tick instead: the tick checks each lab's local hour, and the
Notification ``period_key`` (e.g. the ISO week) makes each send happen once.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

_PROVIDERS = {}


def register(prefix, provider):
    """Register ``provider()`` → {name: values} for the schedules named ``prefix``…"""
    if not prefix.endswith("."):
        raise ValueError(f"A schedule prefix ends with a dot: {prefix!r}")
    for other in _PROVIDERS:
        if other != prefix and (other.startswith(prefix) or prefix.startswith(other)):
            raise ValueError(f"Schedule prefix {prefix!r} overlaps {other!r}")
    _PROVIDERS[prefix] = provider


def providers():
    return dict(_PROVIDERS)


def sync_prefix(prefix, wanted):
    """Make the stored schedules named ``prefix``… exactly ``wanted``. Idempotent. Returns
    (created, updated, removed)."""
    from django_q.models import Schedule

    for name in wanted:
        if not name.startswith(prefix):
            raise ValueError(f"Schedule {name!r} is not under {prefix!r}")
    created = updated = 0
    for name, values in wanted.items():
        values = {"args": None, "repeats": -1, **values}
        first_run = values.pop("first_run", None)
        schedule = Schedule.objects.filter(name=name).order_by("pk").first()
        if schedule is None:
            Schedule.objects.create(name=name, next_run=first_run or timezone.now(), **values)
            created += 1
            continue
        changed = [field for field, value in values.items() if getattr(schedule, field) != value]
        if changed:
            for field in changed:
                setattr(schedule, field, values[field])
            schedule.save(update_fields=changed)
            updated += 1
    removed, _ = Schedule.objects.filter(name__startswith=prefix).exclude(name__in=wanted).delete()
    return created, updated, removed


def ensure_all():
    """Sync every registered prefix. Returns {prefix: (created, updated, removed)}."""
    return {prefix: sync_prefix(prefix, provider()) for prefix, provider in _PROVIDERS.items()}


def zone(name):
    """The time zone called ``name``; UTC for a blank or unknown name."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_now(lab, now=None):
    """``now`` (default: the current time) in the lab's time zone."""
    return (now or timezone.now()).astimezone(zone(lab.time_zone))


def next_local(lab, hour, now=None):
    """The next time the lab's clock shows ``hour``:00."""
    current = local_now(lab, now)
    moment = datetime.combine(current.date(), time(hour), tzinfo=current.tzinfo)
    return moment if moment > current else moment + timedelta(days=1)


def hourly(func, *, minute_offset=0):
    """The values of an hourly tick schedule for ``func`` (a dotted path)."""
    from django_q.models import Schedule

    now = timezone.now()
    first = now.replace(minute=minute_offset % 60, second=0, microsecond=0)
    if first <= now:
        first += timedelta(hours=1)
    return {
        "func": func,
        "schedule_type": Schedule.HOURLY,
        "minutes": None,
        "first_run": first,
    }
