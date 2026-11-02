"""Resolve mirrored requests against the catalog again, e.g. after a normalization fix.

    manage.py quartzy_resolve --lab smith --unresolved        # those whose resolution failed
    manage.py quartzy_resolve --lab smith --type <Quartzy type id>
    manage.py quartzy_resolve --lab smith --all --dry-run     # what a catalog fix would change
    manage.py quartzy_resolve --lab smith --all

It runs here, not in the worker, BATCH requests at a time with a line of progress per batch,
and never calls Quartzy. A request whose resolution comes out different gets its new supplier
product and product; the listings and provisional products nothing uses any more are retired
(catalog.services.retire_orphans). --dry-run does all of it in one transaction that is rolled
back, so it saves nothing and prints what it would have done.
"""

from collections import Counter
from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.quartzy import sync
from apps.quartzy.models import OrderRequest, QuartzyLab, QuartzyType, SyncRun

from ._shared import describe, get_lab

BATCH = 200


class DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Resolve mirrored requests against the catalog again (after a catalog fix)."

    def add_arguments(self, parser):
        parser.add_argument("--lab", help="Lab slug (optional when there is only one lab)")
        which = parser.add_mutually_exclusive_group(required=True)
        which.add_argument(
            "--unresolved", action="store_true", help="Requests whose resolution failed"
        )
        which.add_argument(
            "--type",
            metavar="QUARTZY_TYPE_ID",
            help="One Quartzy type's requests (its Quartzy id, or its number in the admin)",
        )
        which.add_argument(
            "--all", action="store_true", help="Every request; retires what none uses any more"
        )
        parser.add_argument("--dry-run", action="store_true", help="Roll everything back.")

    def handle(self, *args, lab, dry_run, **options):
        lab = get_lab(lab)
        requests = OrderRequest.objects.for_lab(lab)
        if options["unresolved"]:
            requests = requests.filter(resolution_status=OrderRequest.Resolution.UNRESOLVED)
        elif options["type"]:
            requests = requests.filter(quartzy_type=self._quartzy_type(lab, options["type"]))
        runs, totals = [], Counter()
        try:
            with transaction.atomic() if dry_run else nullcontext():
                for qlab in QuartzyLab.objects.for_lab(lab).order_by("pk"):
                    of_qlab = requests.filter(quartzy_lab=qlab).order_by("pk")
                    ids = list(of_qlab.values_list("pk", flat=True))
                    if ids:
                        runs.append(self._resolve(qlab, ids, totals))
                if dry_run:
                    raise DryRun
        except DryRun:
            pass
        if not runs:
            self.stdout.write("No requests to resolve.")
            return
        for run in runs:
            self.stdout.write(describe(run))
        self.stdout.write(
            f"Retired {totals['deleted']} listing(s) nothing uses; products: "
            f"{totals['products_merged']} merged, {totals['products_deleted']} deleted."
        )
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: rolled back, nothing was saved."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Done: {totals['changed']} changed."))

    def _resolve(self, qlab, ids, totals):
        counts = Counter()
        with sync.recorded_run(SyncRun.Kind.RESOLVE, quartzy_lab=qlab) as run:
            for start in range(0, len(ids), BATCH):
                counts.update(sync.resolve_again(ids[start : start + BATCH], run))
                self.stdout.write(
                    f"{qlab}: {min(start + BATCH, len(ids))} of {len(ids)} requests, "
                    f"{counts['changed']} changed, {counts['failed']} failed"
                )
        totals.update(counts)
        return run

    def _quartzy_type(self, lab, value):
        types = QuartzyType.objects.filter(quartzy_lab__lab=lab)
        qtype = types.filter(quartzy_id=value).first()
        if qtype is None and value.isdigit():
            qtype = types.filter(pk=value).first()
        if qtype is None:
            raise CommandError(
                f"No Quartzy type {value!r} in {lab.slug}; see Admin → Quartzy types"
            )
        return qtype
