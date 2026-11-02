"""Load a stock sheet: the lab's registers and personal sheets (docs/templates/stock-sheet.csv).

CSV columns and values: docs/phase0-checklist.md §7. Dry run by default: everything runs, then
rolls back. Either way a review CSV is written next to the input (<file>.review.csv): one line
per row with what was (or would be) done, the product it resolved to, and any warnings.
Pass --commit to save. Re-importing the same file creates nothing.
"""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Lab
from apps.inventory.importers import StockImportFailed, import_stock, write_review_csv


class Command(BaseCommand):
    help = "Import stock from a stock-sheet CSV (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--lab", required=True, help="Lab slug")
        parser.add_argument("--commit", action="store_true")
        parser.add_argument(
            "--verified",
            action="store_true",
            help="Someone checked these exist (default: unverified, 'probably: ask X')",
        )
        parser.add_argument(
            "--create-locations",
            action="store_true",
            help="Create locations the sheet names that LabStock doesn't have yet",
        )
        parser.add_argument(
            "--placeholders",
            action="store_true",
            help="Unknown owner emails are former members: make placeholder users",
        )

    def handle(self, *args, csv_path, lab, commit, verified, create_locations, placeholders,
               **options):  # fmt: skip
        try:
            lab_obj = Lab.objects.get(slug=lab)
        except Lab.DoesNotExist as exc:
            raise CommandError(f"No lab with slug {lab!r}; run bootstrap_lab first") from exc
        review = Path(csv_path).with_suffix(".review.csv")
        try:
            report = import_stock(
                csv_path,
                lab=lab_obj,
                commit=commit,
                verified=verified,
                create_locations=create_locations,
                placeholders=placeholders,
            )
        except OSError as exc:
            raise CommandError(f"Can't read {csv_path}: {exc}") from exc
        except StockImportFailed as exc:
            write_review_csv(exc.report, review)
            for error in exc.errors:
                self.stderr.write(error)
            raise CommandError(
                f"{len(exc.errors)} error(s) in {csv_path}; nothing saved. Review: {review}"
            ) from exc
        write_review_csv(report, review)
        for plan in report.created_locations:
            verb = "created" if commit else "would create"
            self.stdout.write(f"{verb} location: {plan.describe()} [line {plan.implied_by}]")
        for email in report.placeholders:
            self.stdout.write(f"placeholder: {email} (former member)")
        made = sum(1 for row in report.rows if row.action == "create")
        kept = sum(1 for row in report.rows if row.action == "exists")
        summary = f"{report.items} item(s) from {made} row(s), {kept} row(s) already imported"
        if commit:
            self.stdout.write(self.style.SUCCESS(f"Saved: {summary}. Review: {review}"))
        else:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: {summary}; nothing saved. Use --commit. Review: {review}"
                )
            )
