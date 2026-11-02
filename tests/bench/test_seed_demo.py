from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.core.models import Lab
from apps.guard.models import DuplicateCheck
from apps.inventory.models import HistoryClaim, StockItem

pytestmark = pytest.mark.django_db


def test_the_demo_lab_has_people_stock_history_and_matches():
    out = StringIO()
    call_command("seed_demo", stdout=out)
    lab = Lab.objects.get(slug="demo")
    assert lab.memberships.count() == 8
    assert StockItem.objects.for_lab(lab).count() == 5
    assert DuplicateCheck.objects.filter(lab=lab, replay=True).count() == 52
    assert HistoryClaim.objects.for_lab(lab).exists()
    assert "Demo lab 'demo'" in out.getvalue()
    with pytest.raises(CommandError, match="exists already"):
        call_command("seed_demo", stdout=StringIO())
