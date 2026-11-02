"""Run Quartzy sync steps now, e.g. to check a new token or the counts against Quartzy's UI.

    manage.py quartzy_sync --lab smith --dry-run          # one sync cycle, then roll back
    manage.py quartzy_sync --lab smith --head --sweep
    manage.py quartzy_sync --lab smith --labs --backfill --dry-run   # count every request

With no step flags it runs one cycle: labs, types, head, sweep, hot and inventory. --backfill
(every request page) runs only when asked. --dry-run runs inside one transaction that is
rolled back, so it saves nothing (Quartzy is only ever read) and prints what it would have done.
Only active Quartzy labs are synced; it lists the ones it found but left inactive.
"""

from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.quartzy import sync
from apps.quartzy.client import QuartzyError
from apps.quartzy.locks import single_flight
from apps.quartzy.models import QuartzyLab, SyncRun

from ._shared import describe, get_lab, inactive_labs


def _hot(client, qlab, run):
    sync.refresh_hot_set(client, qlab, run, sync.hot_set_ids(qlab))
    if run.status != SyncRun.Status.ABORTED:
        sync.confirm_unlisted_requests(client, qlab, run)


# Steps per Quartzy lab, in order; "labs" runs first, once.
LAB_STEPS = {
    "types": (SyncRun.Kind.TYPES, sync.sync_types),
    "head": (SyncRun.Kind.HEAD, sync.head_probe),
    "sweep": (SyncRun.Kind.SWEEP, sync.rolling_sweep),
    "hot": (SyncRun.Kind.HOT, _hot),
    "inventory": (SyncRun.Kind.INVENTORY, sync.sync_inventory),
    "backfill": (SyncRun.Kind.BACKFILL, sync.backfill_requests),
}
STEPS = ("labs", *LAB_STEPS)
DEFAULT_STEPS = [step for step in STEPS if step != "backfill"]


class DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Run Quartzy sync steps now (read-only). With no step flags, runs one cycle."

    def add_arguments(self, parser):
        parser.add_argument("--lab", help="Lab slug (optional when there is only one lab)")
        for step in STEPS:
            parser.add_argument(f"--{step}", action="store_true", help=f"Run the {step} step")
        parser.add_argument("--dry-run", action="store_true", help="Roll everything back.")

    def handle(self, *args, lab, dry_run, **options):
        lab = get_lab(lab)
        steps = [step for step in STEPS if options[step]] or DEFAULT_STEPS
        runs, left = [], ""
        try:
            with transaction.atomic() if dry_run else nullcontext():
                with sync.client_from_settings() as client:
                    self._run(client, lab, steps, runs)
                left = inactive_labs(lab)
                if dry_run:
                    raise DryRun
        except QuartzyError as exc:
            raise CommandError(f"Quartzy: {exc}") from exc
        except DryRun:
            pass
        for run in runs:
            self.stdout.write(describe(run))
        if left:
            self.stdout.write(self.style.WARNING(left))
        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: rolled back, nothing was saved."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Done: {len(runs)} run(s)."))

    def _run(self, client, lab, steps, runs):
        if "labs" in steps:
            self._step(runs, client, SyncRun.Kind.LABS, f"lab-{lab.pk}", lab, sync.sync_labs)
        qlabs = list(QuartzyLab.objects.for_lab(lab).active())
        if not qlabs and set(steps) - {"labs"}:
            left = inactive_labs(lab)
            raise CommandError(
                f"No active Quartzy lab maps to {lab.slug}. {left}"
                if left
                else f"No active Quartzy lab maps to {lab.slug}; run with --labs"
            )
        for qlab in qlabs:
            for step in steps:
                if step in LAB_STEPS:
                    kind, work = LAB_STEPS[step]
                    self._step(runs, client, kind, qlab.pk, qlab, work)

    def _step(self, runs, client, kind, key, target, work):
        # ``target`` is the Quartzy lab, or for the labs step our own Lab.
        qlab = target if isinstance(target, QuartzyLab) else None
        with single_flight(kind, key, quartzy_lab=qlab) as acquired:
            if not acquired:
                self.stdout.write(self.style.WARNING(f"{kind} {target}: already running"))
                return
            with sync.recorded_run(kind, quartzy_lab=qlab, client=client) as run:
                runs.append(run)
                work(client, target, run)
