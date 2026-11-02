"""Turn the mirrored Quartzy inventory into unverified LabStock stock (design §3).

Dry run by default. Idempotent: what is linked already is skipped.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Lab
from apps.inventory.importers import ImportFailed
from apps.inventory.quartzy_link import link_quartzy_inventory


class Command(BaseCommand):
    help = "Link the lab's mirrored Quartzy inventory to stock items (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("--lab", required=True, help="Lab slug")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, lab, commit, **options):
        try:
            lab_obj = Lab.objects.get(slug=lab)
        except Lab.DoesNotExist as exc:
            raise CommandError(f"No lab with slug {lab!r}; run bootstrap_lab first") from exc
        try:
            report = link_quartzy_inventory(lab_obj, commit=commit)
        except ImportFailed as exc:
            for error in exc.errors:
                self.stderr.write(error)
            raise CommandError(f"{len(exc.errors)} error(s); nothing saved.") from exc
        verb = "linked" if commit else "would link"
        for external_id, name, short_ids in report.linked:
            self.stdout.write(f"{verb}: {name} [{external_id}] -> {', '.join(short_ids)}")
        summary = f"{report.items} stock item(s), {report.skipped} already linked"
        if commit:
            self.stdout.write(self.style.SUCCESS(f"Saved: {summary}."))
        else:
            self.stdout.write(
                self.style.WARNING(f"Dry run: {summary}; nothing saved. Use --commit.")
            )
