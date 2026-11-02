from django.core.management.base import BaseCommand

from apps.guard.replay import replay
from apps.quartzy.management.commands._shared import get_lab


class Command(BaseCommand):
    help = (
        "Run the duplicate matcher over the lab's history (design §7 'Replay'). Stores "
        "replay checks for the co-owner to label; never emails anyone. Re-running skips "
        "requests already replayed with this matcher version."
    )

    def add_arguments(self, parser):
        parser.add_argument("--lab", help="Lab slug (optional with one lab)")
        parser.add_argument("--months", type=int, default=12)

    def handle(self, *args, lab, months, **options):
        lab = get_lab(lab)
        run = replay(
            lab,
            months=months,
            progress=lambda done, total: self.stdout.write(f"  {done} of {total}…"),
        )
        statuses = {}
        for status in run.checks.values_list("status", flat=True):
            statuses[status] = statuses.get(status, 0) + 1
        summary = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
        self.stdout.write(
            self.style.SUCCESS(
                f"Replayed {run.requests} request(s) of the last {months} months"
                + (f": {summary}." if summary else ".")
            )
        )
        self.stdout.write("Label the matches at /guard/label/, then run guard_report.")
