from django.core.management.base import BaseCommand

from apps.quartzy.tasks import ensure_schedules


class Command(BaseCommand):
    help = "Create or update the Django-Q2 schedules for the Quartzy sync (idempotent)."

    def handle(self, *args, **options):
        created, updated, removed = ensure_schedules()
        self.stdout.write(
            self.style.SUCCESS(
                f"Schedules: {created} created, {updated} updated, {removed} removed."
            )
        )
