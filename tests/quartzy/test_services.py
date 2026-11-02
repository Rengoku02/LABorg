from datetime import timedelta

import pytest
from django.utils import timezone

from apps.quartzy.models import OrderRequest
from apps.quartzy.services import watch_requests


@pytest.fixture
def make_request(lab):
    counter = {"n": 0}

    def _make(**fields):
        counter["n"] += 1
        values = {"lab": lab, "quartzy_id": f"q-{counter['n']}", "item_name": "Thing"}
        return OrderRequest.objects.create(**{**values, **fields})

    return _make


@pytest.mark.django_db
def test_watching_starts_or_extends_a_watch_but_never_shortens_one(make_request):
    now = timezone.now()
    fresh = make_request()
    longer = make_request(hot_until=now + timedelta(days=30))
    shorter = make_request(hot_until=now + timedelta(days=1))
    outside = make_request(source=OrderRequest.Source.OUTSIDE, quartzy_id=None)
    assert watch_requests([fresh, longer.pk, shorter, outside], now=now) == 2
    for row in (fresh, longer, shorter, outside):
        row.refresh_from_db()
    assert fresh.hot_until == now + timedelta(days=7) == shorter.hot_until
    assert longer.hot_until == now + timedelta(days=30)
    assert outside.hot_until is None  # nothing to re-read: it isn't in Quartzy


@pytest.mark.django_db
def test_watching_takes_a_single_request_or_a_queryset(make_request):
    one, two = make_request(), make_request()
    assert watch_requests(one, days=2) == 1
    assert watch_requests(OrderRequest.objects.filter(pk=two.pk), days=2) == 1
    assert OrderRequest.objects.filter(hot_until__isnull=False).count() == 2
