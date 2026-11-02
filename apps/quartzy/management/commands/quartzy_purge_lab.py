"""Delete what LabStock mirrored from one Quartzy lab: the undo for a lab mirrored by mistake,
such as a collaborator's lab the token also sees (plan §11 "Privacy").

    manage.py quartzy_purge_lab <Quartzy lab id>            # a dry run: what would go
    manage.py quartzy_purge_lab <Quartzy lab id> --commit

The Quartzy lab must be inactive (untick "Is active" in Admin → Quartzy labs), so no sync
writes to it meanwhile. In one transaction it deletes the lab's requests and their status
observations, its inventory items and instances, its sync runs and errors and its types; then
the people (person aliases) nothing else refers to and no user is linked to, among those its
requests named; then the provisional catalog rows only its requests used, with the products
merged into those earlier; then the Quartzy lab itself. Quartzy is never called. If the sync
finds the lab again, it is recorded inactive.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.catalog.models import SupplierProduct
from apps.catalog.services import retire_orphans
from apps.core.models import PersonAlias
from apps.quartzy.models import (
    OrderRequest,
    QuartzyInventoryItem,
    QuartzyLab,
    QuartzyType,
    SyncRun,
)

# Where a request names people (sync.upsert_order_request resolves both to aliases).
_PEOPLE = ("requested_by", "created_by")


class DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Delete what was mirrored from one inactive Quartzy lab. A dry run unless --commit."

    def add_arguments(self, parser):
        parser.add_argument("quartzy_id", metavar="QUARTZY_ID", help="The Quartzy lab's id")
        parser.add_argument("--commit", action="store_true", help="Delete; else only report.")

    def handle(self, *args, quartzy_id, commit, **options):
        qlab = QuartzyLab.objects.select_related("lab").filter(quartzy_id=quartzy_id).first()
        if qlab is None:
            raise CommandError(f"No Quartzy lab {quartzy_id!r}; see Admin → Quartzy labs")
        if qlab.is_active:
            raise CommandError(
                f"{qlab} is active: untick “Is active” in Admin → Quartzy labs first, so no "
                "sync writes to it while it is purged."
            )
        try:
            with transaction.atomic():
                counts = purge(qlab)
                if not commit:
                    raise DryRun
        except DryRun:
            pass
        self.stdout.write(f"{qlab} [{qlab.quartzy_id}], mapped to {qlab.lab}:")
        for label, key in (
            ("order request(s)", "quartzy.OrderRequest"),
            ("status observation(s)", "quartzy.OrderStatusObservation"),
            ("inventory item(s)", "quartzy.QuartzyInventoryItem"),
            ("inventory instance(s)", "quartzy.QuartzyInventoryInstance"),
            ("sync run(s)", "quartzy.SyncRun"),
            ("sync error(s)", "quartzy.SyncError"),
            ("Quartzy type(s)", "quartzy.QuartzyType"),
            ("person alias(es) nothing else refers to", "core.PersonAlias"),
            ("catalog listing(s) nothing else uses", "deleted"),
            ("catalog product(s) nothing else uses", "products_deleted"),
        ):
            self.stdout.write(f"  {counts[key]} {label}")
        if commit:
            self.stdout.write(self.style.SUCCESS(f"Purged {qlab}."))
        else:
            self.stdout.write(
                self.style.WARNING("Dry run: rolled back, nothing was deleted. Add --commit.")
            )


def purge(qlab):
    """Delete ``qlab`` and what was mirrored from it (see the module doc). Returns a Counter
    of deleted rows by model label, plus catalog.services.retire_orphans' counts."""
    counts = Counter()
    requests = OrderRequest.objects.filter(quartzy_lab=qlab)
    aliases = set()
    for field in ("requested_by_alias", "created_by_alias"):
        aliases |= set(requests.exclude(**{field: None}).values_list(field, flat=True))
    emails = set()
    for payload in requests.values_list("raw_payload", flat=True):
        for field in _PEOPLE:
            person = payload.get(field) if isinstance(payload, dict) else None
            email = person.get("email") if isinstance(person, dict) else None
            if isinstance(email, str) and email.strip():
                emails.add(email.strip().lower())
    listings = set(requests.values_list("supplier_product", flat=True)) - {None}
    for queryset in (
        requests,
        QuartzyInventoryItem.objects.filter(quartzy_lab=qlab),
        SyncRun.objects.filter(quartzy_lab=qlab),
        QuartzyType.objects.filter(quartzy_lab=qlab),
    ):
        counts.update(queryset.delete()[1])
    candidates = PersonAlias.objects.filter(
        Q(pk__in=aliases) | Q(kind=PersonAlias.Kind.EMAIL, value__in=emails), user=None
    )
    forgotten = [alias.pk for alias in candidates if not _still_named(alias)]
    counts.update(PersonAlias.objects.filter(pk__in=forgotten).delete()[1])
    counts.update(retire_orphans(SupplierProduct.objects.filter(pk__in=listings)))
    counts.update(QuartzyLab.objects.filter(pk=qlab.pk).delete()[1])
    return counts


def _still_named(alias):
    """Whether any remaining row refers to ``alias``, or, for an email alias (which rows don't
    refer to), whether a remaining request names that email."""
    for relation in PersonAlias._meta.related_objects:
        if relation.related_model._base_manager.filter(**{relation.field.name: alias}).exists():
            return True
    if alias.kind != PersonAlias.Kind.EMAIL:
        return False
    named = Q()
    for field in _PEOPLE:
        named |= Q(**{f"raw_payload__{field}__email__iexact": alias.value})
    return OrderRequest.objects.filter(named).exists()
