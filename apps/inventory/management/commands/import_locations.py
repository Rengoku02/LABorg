"""Load the coarse location tree from the co-owner's walk (docs/templates/locations.csv).

CSV columns: path, kind, temperature, owner_email, code, notes (docs/phase0-checklist.md §7).
Dry run by default: everything runs, then rolls back, and the report says what would be
created. Pass --commit to save. Re-running creates nothing: existing paths are kept.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Lab
from apps.inventory.importers import ImportFailed, import_locations


class Command(BaseCommand):
    help = "Import locations from a locations.csv (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--lab", required=True, help="Lab slug")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, csv_path, lab, commit, **options):
        try:
            lab_obj = Lab.objects.get(slug=lab)
        except Lab.DoesNotExist as exc:
            raise CommandError(f"No lab with slug {lab!r}; run bootstrap_lab first") from exc
        try:
            report = import_locations(csv_path, lab=lab_obj, commit=commit)
        except OSError as exc:
            raise CommandError(f"Can't read {csv_path}: {exc}") from exc
        except ImportFailed as exc:
            for error in exc.errors:
                self.stderr.write(error)
            raise CommandError(f"{len(exc.errors)} error(s) in {csv_path}; nothing saved.") from exc
        verb = "created" if commit else "would create"
        for plan in report.created:
            where = f"line {plan.line}" if plan.line else f"implied by line {plan.implied_by}"
            generated = f", code {plan.saved_code}" if commit and not plan.code else ""
            self.stdout.write(f"{verb}: {plan.describe()} [{where}{generated}]")
        for plan, differences in report.existing:
            kept = f"; kept: {'; '.join(differences)}" if differences else ""
            self.stdout.write(f"exists: {plan.path} [line {plan.line}]{kept}")
        existing = f"{len(report.existing)} already in LabStock"
        if commit:
            created = f"{len(report.created)} location(s) created"
            self.stdout.write(self.style.SUCCESS(f"Saved: {created}, {existing}."))
        else:
            created = f"{len(report.created)} to create"
            self.stdout.write(
                self.style.WARNING(f"Dry run: {created}, {existing}; nothing saved. Use --commit.")
            )
