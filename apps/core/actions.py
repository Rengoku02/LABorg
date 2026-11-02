"""One-action signed links in emails (plan §12 "Signed links").

A link encodes ``{notification id, recipient id, nonce}`` signed with SECRET_KEY. Opening it
(GET) only shows a confirm page, because mail scanners and link previewers fetch URLs on
their own; the action happens on the POST from that page, once.

Other apps register what an action does::

    @register_action(Notification.Kind.DUPLICATE_ALERT, "borrow", label="Borrow instead")
    def borrow(notification, request, data): ...
"""

from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from .models import Notification

SALT = "labstock.action.v1"
MAX_AGE = timedelta(days=14)


@dataclass(frozen=True)
class Action:
    kind: str
    name: str
    label: str
    handler: object
    # Optional choices shown as a pick-list on the confirm page (e.g. "need my own" reasons).
    reasons: tuple = ()


_REGISTRY: dict[tuple[str, str], Action] = {}


def register_action(kind, name, *, label, reasons=()):
    def decorator(func):
        _REGISTRY[(str(kind), name)] = Action(str(kind), name, label, func, tuple(reasons))
        return func

    return decorator


def get_action(kind, name):
    return _REGISTRY.get((str(kind), name))


def make_token(notification):
    payload = {"n": notification.pk, "r": notification.recipient_id, "k": notification.nonce}
    return signing.TimestampSigner(salt=SALT).sign_object(payload)


class InvalidActionLink(Exception):
    pass


def read_token(token):
    """Return the Notification a token points at, or raise InvalidActionLink."""
    try:
        payload = signing.TimestampSigner(salt=SALT).unsign_object(token, max_age=MAX_AGE)
    except signing.SignatureExpired as exc:
        raise InvalidActionLink("This link has expired.") from exc
    except signing.BadSignature as exc:
        raise InvalidActionLink("This link is not valid.") from exc
    notification = Notification.objects.filter(pk=payload.get("n")).first()
    if (
        notification is None
        or notification.nonce != payload.get("k")
        or notification.recipient_id != payload.get("r")
    ):
        raise InvalidActionLink("This link is no longer valid.")
    return notification


def action_url(notification, action_name):
    from django.urls import reverse

    path = reverse("core:action", kwargs={"token": make_token(notification), "action": action_name})
    return f"{settings.SITE_URL}{path}"


class AlreadyRecorded(Exception):
    def __init__(self, notification):
        super().__init__("already recorded")
        self.notification = notification


def perform(token, action_name, request, data):
    """Run a registered action exactly once (called from the POST view)."""
    with transaction.atomic():
        notification = read_token(token)
        notification = Notification.objects.select_for_update().get(pk=notification.pk)
        action = get_action(notification.kind, action_name)
        if action is None:
            raise InvalidActionLink("This action is not available.")
        if notification.acted_at is not None:
            raise AlreadyRecorded(notification)
        result = action.handler(notification, request, data) or {}
        notification.acted_at = timezone.now()
        notification.action = action_name
        notification.action_detail = {**data, **result}
        notification.save(update_fields=["acted_at", "action", "action_detail"])
    return notification, action
