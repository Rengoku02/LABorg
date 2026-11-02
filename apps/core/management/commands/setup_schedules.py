from django.core.management.base import BaseCommand

from apps.core.schedules import ensure_all


class Command(BaseCommand):
    help = (
        "Create or update every app's Django-Q2 schedules (Quartzy sync, guard, inventory, "
        "reports), and remove the ones no longer wanted. Idempotent."
    )

    def handle(self, *args, **options):
        totals = [0, 0, 0]
        for prefix, counts in sorted(ensure_all().items()):
            created, updated, removed = counts
            self.stdout.write(f"{prefix} {created} created, {updated} updated, {removed} removed.")
            totals = [total + count for total, count in zip(totals, counts, strict=True)]
        created, updated, removed = totals
        self.stdout.write(
            self.style.SUCCESS(
                f"Schedules: {created} created, {updated} updated, {removed} removed."
            )
        )
