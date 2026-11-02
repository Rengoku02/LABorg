"""The email budget (plan §5 "Email budget"; design §10.9): at most
``email.max_non_alert_per_week`` non-alert emails per person per week.

Asks of the lab and the weekly claims email are dropped (``Suppressed("budget")``) once the
recipient had that many non-alert emails in the last 7 days. The personal digest is never
dropped (it gathers what didn't go out) but it counts. Alerts, receipts, asks to a holder,
nudges, system mail and the PI summary are exempt and don't count.
"""

from datetime import timedelta

from django.utils import timezone

from apps.core.mail import Suppressed, register_send_check
from apps.core.models import Notification
from apps.core.policies import get_policy

Kind = Notification.Kind
COUNTED = (Kind.ASK_LAB, Kind.CLAIMS, Kind.PERSONAL_DIGEST)
LIMITED = (Kind.ASK_LAB, Kind.CLAIMS)


def sent_this_week(notification, now=None):
    now = now or timezone.now()
    return (
        Notification.objects.filter(
            recipient_email=notification.recipient_email,
            kind__in=COUNTED,
            status=Notification.Status.SENT,
            sent_at__gte=now - timedelta(days=7),
        )
        .exclude(pk=notification.pk)
        .count()
    )


@register_send_check(*LIMITED)
def within_budget(notification):
    limit = get_policy("email.max_non_alert_per_week", notification.lab)
    if limit is not None and sent_this_week(notification) >= limit:
        return Suppressed("budget")
    return None
