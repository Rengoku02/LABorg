"""Mirror a lab's whole Quartzy history: labs, types, every request page, inventory.

    manage.py quartzy_backfill --lab smith            # alerts stay disarmed
    manage.py quartzy_backfill --lab smith --arm      # ... and arm them from now on
    manage.py quartzy_backfill --lab smith --rearm    # ... from now on, even if armed before

Backfilled requests never alert: the guard alerts only on requests made at or after
QuartzyLab.alerts_armed_at, which this leaves empty unless --arm sets it to now. A lab armed
already keeps its arming time (so requests made since then can still alert), unless --rearm.
Arming waits until no Quartzy type with requests carries an unconfirmed suggested item type.

Only active Quartzy labs are backfilled; the ones the token sees but this lab doesn't mirror
(see sync_labs) are listed. Safe to run again: unchanged requests write nothing new. Then
(re)creates the sync schedules.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.quartzy import sync, tasks
from apps.quartzy.client import QuartzyError
from apps.quartzy.locks import single_flight
from apps.quartzy.models import SyncRun

from ._shared import describe, get_lab, inactive_labs


class Command(BaseCommand):
    help = "Mirror the full Quartzy history for a lab. Alerts stay disarmed unless --arm."

    def add_arguments(self, parser):
        parser.add_argument("--lab", help="Lab slug (optional when there is only one lab)")
        parser.add_argument("--arm", action="store_true", help="Arm duplicate alerts from now on.")
        parser.add_argument(
            "--rearm",
            action="store_true",
            help="Arm duplicate alerts from now on, moving an earlier arming time too.",
        )

    def handle(self, *args, lab, arm, rearm, **options):
        lab = get_lab(lab)
        with single_flight(SyncRun.Kind.BACKFILL, f"lab-{lab.pk}") as acquired:
            if not acquired:
                raise CommandError(f"A backfill of {lab.slug} is already running")
            try:
                with sync.client_from_settings() as client:
                    runs = sync.full_backfill(client, lab)
            except QuartzyError as exc:
                raise CommandError(f"Quartzy: {exc}") from exc
        for run in runs:
            self.stdout.write(describe(run))
        if left := inactive_labs(lab):
            self.stdout.write(self.style.WARNING(left))
        created, updated, removed = tasks.ensure_schedules()
        self.stdout.write(f"Schedules: {created} created, {updated} updated, {removed} removed.")
        if not (arm or rearm):
            self.stdout.write(self.style.WARNING("Alerts are not armed; use --arm when ready."))
            return
        armed = sync.arm_alerts(lab, rearm=rearm)
        if not armed:
            self.stdout.write(self.style.WARNING("No active Quartzy lab, so nothing was armed."))
        for qlab, armed_now in armed:
            since = f"{timezone.localtime(qlab.alerts_armed_at):%Y-%m-%d %H:%M}"
            if armed_now:
                message = f"{qlab}: alerts armed from now on ({since})."
                self.stdout.write(self.style.SUCCESS(message))
            else:
                self.stdout.write(
                    f"{qlab}: alerts already armed since {since}; --rearm moves that to now."
                )
