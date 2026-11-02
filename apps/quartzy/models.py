"""The read-only mirror of Quartzy (plan §7 "Quartzy mirror", §8).

``sync.py`` writes these rows from what Quartzy's API returns. Nothing is deleted because
Quartzy stopped listing it: a request is marked ``deleted_in_quartzy_at`` only after its own
GET returned 404 in a run that looked healthy. The app-only exceptions are
``OrderRequest.requested_for`` and ``hot_until``, and requests the lab logs itself
(``source`` outside or gift), which have no Quartzy id.

Rows mirrored from a Quartzy lab that is not active (a collaborator's lab the token happens to
see, or one switched off) are kept but hidden: ``for_lab()`` and ``live()`` leave them out,
and ``manage.py quartzy_purge_lab`` deletes them.
"""

from datetime import timedelta
from functools import cached_property, partial

from django.conf import settings
from django.db import models, transaction
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

# The health panel (and any page) warns when no head sync has succeeded for this long.
STALE_AFTER = timedelta(minutes=30)
_UNSET = object()
# A mirrored row counts only while its Quartzy lab is active; rows logged in the app have none.
_MIRROR_SHOWN = Q(quartzy_lab__isnull=True) | Q(quartzy_lab__is_active=True)


class QuartzyLabQuerySet(models.QuerySet):
    def for_lab(self, lab):
        return self.filter(lab=lab)

    def active(self):
        return self.filter(is_active=True)

    def with_last_head_ok(self):
        """Annotate ``last_head_ok``: when the last successful head run finished."""
        runs = SyncRun.objects.filter(
            quartzy_lab=OuterRef("pk"), kind=SyncRun.Kind.HEAD, status=SyncRun.Status.OK
        ).order_by("-finished_at")
        return self.annotate(last_head_ok=Subquery(runs.values("finished_at")[:1]))

    def stale(self, now=None):
        """Active labs with no successful head sync in the last 30 minutes (plan §6): new
        requests are not reaching LabStock, so pages should say so."""
        cutoff = (now or timezone.now()) - STALE_AFTER
        return (
            self.active()
            .with_last_head_ok()
            .filter(Q(last_head_ok__lt=cutoff) | Q(last_head_ok__isnull=True))
        )


class QuartzyLab(models.Model):
    """A Lab in Quartzy, mapped onto one of our Labs (several Quartzy Labs may share one).

    The sync finds every Quartzy lab the token can see, but mirrors only the active ones;
    ``sync_labs`` decides that for a lab it meets for the first time (QUARTZY_LAB_IDS). Its
    requests are tenant data of ``lab``: changing ``lab`` moves them along (``save()``).
    """

    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="quartzy_labs")
    quartzy_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200, blank=True)
    organization_id = models.CharField(max_length=64, blank=True)
    organization_name = models.CharField(max_length=200, blank=True)
    # Quartzy's rollout flag. None means Quartzy left the field out, which the research says
    # to read as "migrated to instances" (instances_enabled).
    uses_instances = models.BooleanField(null=True, blank=True)
    is_active = models.BooleanField(
        default=True, help_text="Only active labs are synced, and only their rows are shown."
    )
    # The guard alerts only on requests made at or after this moment. The history backfill
    # leaves it empty, so old requests never alert (plan §9 "Safety").
    alerts_armed_at = models.DateTimeField(null=True, blank=True)
    # When the app took over physical stock from Quartzy inventory for this lab (plan §2).
    cutover_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # Sync bookkeeping, owned by sync.py. head_cursor holds what the head probe needs between
    # cycles, e.g. {"last_page": 41}.
    head_cursor = models.JSONField(default=dict, blank=True)
    sweep_cursor = models.IntegerField(default=0)
    sweep_pass_started_at = models.DateTimeField(null=True, blank=True)
    # Start of the last complete sweep pass: every request Quartzy listed then has been read
    # since, so one not seen since then is a deletion suspect that the hot set reads by id.
    full_sweep_started_at = models.DateTimeField(null=True, blank=True)
    inventory_cursor = models.IntegerField(default=0)
    inventory_pass_started_at = models.DateTimeField(null=True, blank=True)
    # From a 429's Retry-After: sync tasks for this lab wait until then.
    backoff_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = QuartzyLabQuerySet.as_manager()

    class Meta:
        verbose_name = "Quartzy lab"

    def __str__(self):
        return self.name or self.quartzy_id

    def save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")
        watched = self.pk is not None and (
            fields is None or not {"lab", "lab_id"}.isdisjoint(fields)
        )
        with transaction.atomic():
            stored = QuartzyLab.objects.filter(pk=self.pk).values_list("lab", flat=True)
            before = stored.first() if watched else None
            super().save(*args, **kwargs)
            if before is not None and before != self.lab_id:
                # The requests' own ``lab`` is what tenant queries filter on.
                self.order_requests.update(lab_id=self.lab_id)

    @property
    def instances_enabled(self):
        return self.uses_instances is not False


def resolve_types_on_commit(qtype_pks):
    """Once the transaction commits, re-resolve the requests of these Quartzy types (also
    used by receivers.py, when an item type's identity rule changes)."""
    from django_q.tasks import async_task  # imported here, as in apps/core/mail.py

    for pk in qtype_pks:
        transaction.on_commit(partial(async_task, "apps.quartzy.tasks.resolve_type", pk))


class QuartzyTypeQuerySet(models.QuerySet):
    def update(self, **kwargs):
        """QuerySet.update; types whose item type it changes get their requests re-resolved,
        as ``QuartzyType.save`` does."""
        value = kwargs.get("item_type", kwargs.get("item_type_id", _UNSET))
        if value is _UNSET:
            return super().update(**kwargs)
        changing = list(self.exclude(item_type=value).values_list("pk", flat=True))
        updated = super().update(**kwargs)
        resolve_types_on_commit(changing)
        return updated


class QuartzyType(models.Model):
    """A Quartzy Type, mapped onto an ItemType.

    The sync fills ``item_type`` from ``catalog.services.suggest_item_type`` when it first meets
    a type, or later for an unmapped type nobody has reviewed, and sets ``suggested``. The
    wet-lab co-owner confirms or corrects the mapping in the admin, which clears ``suggested``
    and sets ``reviewed_at``; the sync never changes a reviewed mapping.

    The mapping decides, among other things, whether a request's catalog # identifies anything
    (``ItemType.identity_rule``). So whenever ``item_type`` changes, through ``save()`` or
    ``QuerySet.update()``, the type's requests are resolved again after the transaction
    commits (``tasks.resolve_type``), and so are they when the item type's identity rule is
    changed and saved (receivers.py).
    """

    quartzy_lab = models.ForeignKey(QuartzyLab, on_delete=models.CASCADE, related_name="types")
    quartzy_id = models.CharField(max_length=64)
    name = models.CharField(max_length=200, blank=True)
    item_type = models.ForeignKey(
        "catalog.ItemType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="quartzy_types",
    )
    suggested = models.BooleanField(
        default=False, help_text="Suggested by the sync from the name; nobody has confirmed it."
    )
    reviewed_at = models.DateTimeField(
        null=True, blank=True, help_text="When a person last saved or confirmed this mapping."
    )

    objects = QuartzyTypeQuerySet.as_manager()

    class Meta:
        verbose_name = "Quartzy type"
        constraints = [
            models.UniqueConstraint(
                fields=["quartzy_lab", "quartzy_id"], name="quartzy_type_unique_per_lab"
            ),
        ]

    def __str__(self):
        return self.name or self.quartzy_id

    def save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")
        watched = self.pk is not None and (
            fields is None or not {"item_type", "item_type_id"}.isdisjoint(fields)
        )
        stored = QuartzyType.objects.filter(pk=self.pk).values_list("item_type", flat=True)
        before = stored.first() if watched else None
        super().save(*args, **kwargs)
        if watched and before != self.item_type_id:
            resolve_types_on_commit([self.pk])


class OrderRequestQuerySet(models.QuerySet):
    def for_lab(self, lab):
        """The lab's requests: logged in the app, or mirrored from an active Quartzy lab."""
        return self.filter(_MIRROR_SHOWN, lab=lab)

    def live(self):
        """Not deleted in Quartzy, nor from a Quartzy lab that is not mirrored."""
        return self.filter(_MIRROR_SHOWN, deleted_in_quartzy_at__isnull=True)

    def open(self):
        """Any status but RECEIVED or CANCELLED, and live."""
        return self.live().exclude(status__in=OrderRequest.CLOSED_STATUSES)


class OrderRequest(models.Model):
    """One purchase request: mirrored from Quartzy, or logged in the app (outside purchase or
    gift) so the duplicate guard sees it too."""

    class Source(models.TextChoices):
        QUARTZY = "quartzy", "Quartzy"
        OUTSIDE = "outside", "Outside purchase"
        GIFT = "gift", "Gift"

    class Status(models.TextChoices):
        CREATED = "CREATED", "Created"
        APPROVED = "APPROVED", "Approved"
        ORDERED = "ORDERED", "Ordered"
        BACKORDERED = "BACKORDERED", "Backordered"
        RECEIVED = "RECEIVED", "Received"
        CANCELLED = "CANCELLED", "Cancelled"

    class Resolution(models.TextChoices):
        """``catalog.models.ResolutionStatus`` plus "unresolved" (not yet, or it failed)."""

        EXACT = "exact", "Same catalog #"
        BASE_VARIANT = "base_variant", "Other pack size"
        NEW = "new", "New product"
        GENERIC = "generic", "Generic catalog #"
        NO_CATALOG = "no_catalog", "No catalog #"
        UNRESOLVED = "unresolved", "Not resolved"

    CLOSED_STATUSES = (Status.RECEIVED, Status.CANCELLED)
    # The guard can't check these on catalog # (plan §9; the dashboard shows their share).
    UNCHECKABLE = (Resolution.NO_CATALOG, Resolution.GENERIC, Resolution.UNRESOLVED)

    # A mirrored request's lab is its Quartzy lab's: every sync writes it again, and
    # QuartzyLab.save() moves the requests along when the Quartzy lab is mapped elsewhere.
    lab = models.ForeignKey("core.Lab", on_delete=models.CASCADE, related_name="order_requests")
    quartzy_lab = models.ForeignKey(
        QuartzyLab,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="order_requests",
    )
    quartzy_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.QUARTZY)
    # Quartzy may add statuses; an unknown one is stored as sent (and counts as open).
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.CREATED)

    # As typed in Quartzy.
    item_name = models.CharField(max_length=500, blank=True)
    vendor_name_raw = models.CharField(max_length=300, blank=True)
    catalog_number_raw = models.CharField(max_length=200, blank=True)
    unit_size_raw = models.CharField(max_length=200, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    unit_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    # In major units (dollars), whatever unit Quartzy's Money used (money_unit_used).
    total_price = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)
    money_unit_used = models.CharField(max_length=10, blank=True)
    quartzy_type = models.ForeignKey(
        QuartzyType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_requests",
    )

    # People. requested_by is the alias's user at sync time; if Quartzy sent no requested_by,
    # it is created_by's (plan §7 "People"). requested_for exists only in the app.
    requested_by_alias = models.ForeignKey(
        "core.PersonAlias",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_order_requests",
    )
    created_by_alias = models.ForeignKey(
        "core.PersonAlias",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_order_requests",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_requests",
    )
    requested_for = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="order_requests_for_me",
    )

    requested_at = models.DateTimeField(null=True, blank=True)
    quartzy_updated_at = models.DateTimeField(null=True, blank=True)
    app_url = models.URLField(max_length=500, blank=True)
    spend_tracking_code = models.CharField(max_length=200, blank=True)
    is_urgent = models.BooleanField(null=True, blank=True)
    notes = models.TextField(blank=True)

    # Change detection: a sha256 of the canonical payload (Quartzy's updated_at is a hint).
    payload_sha256 = models.CharField(max_length=64, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    # When the read that last wrote or confirmed this row was fetched, so a payload fetched
    # before it (a slow backfill, an overlapping run) is never applied over it.
    last_seen_at = models.DateTimeField(default=timezone.now)
    deleted_in_quartzy_at = models.DateTimeField(null=True, blank=True)
    # The last read by id (hot set, deletion check), answered or not: suspects are read in
    # this order, so one Quartzy keeps failing on waits behind the others.
    by_id_checked_at = models.DateTimeField(null=True, blank=True)
    # The guard (or a person) keeps a request in the hot set, re-read by id, until then.
    hot_until = models.DateTimeField(null=True, blank=True)

    # Resolved once per request line, because every resolution except "exact" creates catalog
    # rows. Again only when its vendor, catalog # or item type changes (also through its
    # Quartzy type's mapping), at most daily while it failed, or by manage.py quartzy_resolve.
    supplier_product = models.ForeignKey(
        "catalog.SupplierProduct",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="order_requests",
    )
    product = models.ForeignKey(
        "catalog.Product",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="order_requests",
    )
    resolution_status = models.CharField(
        max_length=20, choices=Resolution.choices, default=Resolution.UNRESOLVED
    )
    resolution_attempted_at = models.DateTimeField(null=True, blank=True)

    objects = OrderRequestQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["status"], name="quartzy_or_status"),
            models.Index(fields=["requested_at"], name="quartzy_or_requested_at"),
            # product has the index Django gives every foreign key.
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(source="quartzy") | Q(quartzy_id__isnull=False),
                name="quartzy_orderrequest_mirrored_has_id",
            ),
        ]

    def __str__(self):
        label = self.item_name or self.catalog_number_raw or self.quartzy_id or f"#{self.pk}"
        return f"{label} ({self.status})"

    @property
    def is_open(self):
        return self.deleted_in_quartzy_at is None and self.status not in self.CLOSED_STATUSES

    @property
    def requester_alias(self):
        """Who asked for it: requested_by, else created_by (Quartzy sends null for some)."""
        return self.requested_by_alias or self.created_by_alias


class OrderStatusObservation(models.Model):
    """A status the sync saw a request in (plan §8 "Change detection").

    The API exposes no status history, so these rows are the only record of when a request
    moved on. ``observed_at`` is when the sync saw it, not when it happened; the first
    observation of a request has a blank ``from_status``.
    """

    class Via(models.TextChoices):
        POLL = "poll", "Polling"
        WEBHOOK = "webhook", "Webhook"
        BACKFILL = "backfill", "History backfill"

    order_request = models.ForeignKey(
        OrderRequest, on_delete=models.CASCADE, related_name="status_observations"
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    observed_at = models.DateTimeField(default=timezone.now)
    quartzy_updated_at = models.DateTimeField(null=True, blank=True)
    via = models.CharField(max_length=10, choices=Via.choices)

    class Meta:
        ordering = ["observed_at", "pk"]

    def __str__(self):
        return f"{self.from_status or '∅'} → {self.to_status}"


class QuartzyInventoryItemQuerySet(models.QuerySet):
    def for_lab(self, lab):
        """Mirrored from one of the lab's active Quartzy labs."""
        return self.filter(quartzy_lab__lab=lab, quartzy_lab__is_active=True)

    def live(self):
        """Not deleted in Quartzy, nor from a Quartzy lab that is not mirrored."""
        return self.filter(deleted_in_quartzy_at__isnull=True, quartzy_lab__is_active=True)


class QuartzyInventoryItem(models.Model):
    """Quartzy inventory, mirrored raw until each lab's cutover (plan §2 "Physical stock")."""

    quartzy_lab = models.ForeignKey(
        QuartzyLab, on_delete=models.CASCADE, related_name="inventory_items"
    )
    quartzy_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=500, blank=True)
    vendor_name = models.CharField(max_length=300, blank=True)
    catalog_number = models.CharField(max_length=200, blank=True)
    cas_number = models.CharField(max_length=50, blank=True)
    quantity_raw = models.CharField(max_length=100, blank=True)
    unit_size_raw = models.CharField(max_length=200, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    payload_sha256 = models.CharField(max_length=64, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    # Set only after GET /inventory-items/{id} returned 404, never because a listing missed it.
    deleted_in_quartzy_at = models.DateTimeField(null=True, blank=True)
    # The last read by id, answered or not (as OrderRequest.by_id_checked_at).
    by_id_checked_at = models.DateTimeField(null=True, blank=True)

    objects = QuartzyInventoryItemQuerySet.as_manager()

    class Meta:
        verbose_name = "Quartzy inventory item"

    def __str__(self):
        return self.name or self.quartzy_id


class QuartzyInventoryInstance(models.Model):
    """One physical instance (tube, bottle) of a Quartzy inventory item."""

    item = models.ForeignKey(
        QuartzyInventoryItem, on_delete=models.CASCADE, related_name="instances"
    )
    quartzy_id = models.CharField(max_length=64, unique=True)
    quantity_raw = models.CharField(max_length=100, blank=True)
    lot_number = models.CharField(max_length=200, blank=True)
    expiration_date = models.DateField(null=True, blank=True)
    location_path = models.TextField(blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    # Gone from its item's instance list, which an item's own payload always lists in full.
    deleted_in_quartzy_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Quartzy inventory instance"

    def __str__(self):
        return f"{self.item} [{self.quartzy_id}]"


class SyncRun(models.Model):
    """One sync job run, for the health panel and for counting calls to Quartzy."""

    class Kind(models.TextChoices):
        HEAD = "head", "Head probe"
        SWEEP = "sweep", "Rolling sweep"
        HOT = "hot", "Hot set"
        FULL = "full", "Full backfill"
        INVENTORY = "inventory", "Inventory"
        TYPES = "types", "Types"
        LABS = "labs", "Labs"
        BACKFILL = "backfill", "Request history"
        # Requests resolved against the catalog again; no Quartzy calls.
        RESOLVE = "resolve", "Catalog re-resolution"

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        OK = "ok", "OK"
        ABORTED = "aborted", "Aborted"
        FAILED = "failed", "Failed"
        SKIPPED_LOCKED = "skipped_locked", "Skipped (already running)"

    quartzy_lab = models.ForeignKey(
        QuartzyLab, on_delete=models.CASCADE, null=True, blank=True, related_name="sync_runs"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RUNNING)
    calls = models.PositiveIntegerField(default=0)
    pages = models.PositiveIntegerField(default=0)
    seen = models.PositiveIntegerField(default=0)
    new = models.PositiveIntegerField(default=0)
    changed = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["quartzy_lab", "kind", "-started_at"], name="quartzy_run_lab_kind"
            ),
            models.Index(fields=["started_at"], name="quartzy_run_started"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} {self.started_at:%Y-%m-%d %H:%M} {self.status}"

    def add_note(self, text):
        self.note = f"{self.note}\n{text}".strip()[-4000:]

    @cached_property
    def synced_request_ids(self):
        """Pks of the order requests this run created or resolved differently, kept in memory
        for ``signals.requests_synced`` (sync.recorded_run sends it once the run is saved)."""
        return set()

    def add_count(self, label, count=1):
        """Count something in the note: one line "``label``: total", however often it is
        counted in the run."""
        prefix = f"{label}: "
        lines = self.note.splitlines()
        for index, line in enumerate(lines):
            if line.startswith(prefix) and line.removeprefix(prefix).isdigit():
                count += int(lines.pop(index).removeprefix(prefix))
                break
        self.note = "\n".join([*lines, f"{prefix}{count}"])[-4000:]


class SyncError(models.Model):
    """A record a run could not take in. The rest of its page went on."""

    class Kind(models.TextChoices):
        PARSE = "parse", "Unreadable record"
        UPSERT = "upsert", "Could not save"
        RESOLVE = "resolve", "Catalog resolution failed"
        READ = "read", "Quartzy would not serve it by id"

    sync_run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="sync_errors")
    quartzy_id = models.CharField(max_length=64, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    message = models.TextField()
    payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.get_kind_display()} {self.quartzy_id}".strip()


class SpecSnapshot(models.Model):
    """A copy of Quartzy's published OpenAPI spec, kept each time its sha256 changes. The
    baseline before the first change is docs/research/quartzy-public-api.yaml."""

    sha256 = models.CharField(max_length=64)
    fetched_at = models.DateTimeField(default=timezone.now)
    content = models.TextField()

    class Meta:
        get_latest_by = ["fetched_at", "pk"]

    def __str__(self):
        return f"{self.sha256[:12]} ({self.fetched_at:%Y-%m-%d})"
