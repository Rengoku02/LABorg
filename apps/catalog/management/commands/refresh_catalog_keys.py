"""Re-derive supplier products' catalog keys and bases.

Run it after the normalization code or a vendor family's pack rule changed (the admin does
this by itself when someone edits a family's rule). Supplier products whose new key is
already taken in their family are folded into that one, merging the two products.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import VendorFamily
from apps.catalog.services import refresh_catalog_keys


class DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Re-derive supplier products' catalog keys (after a rule or normalization change)."

    def add_arguments(self, parser):
        parser.add_argument("--family", help="Only this vendor family (slug)")
        parser.add_argument("--dry-run", action="store_true", help="Report only; save nothing.")

    def handle(self, *args, family, dry_run, **options):
        family_obj = None
        if family:
            family_obj = VendorFamily.objects.filter(slug=family).first()
            if family_obj is None:
                raise CommandError(f"No vendor family with slug {family!r}")
        try:
            with transaction.atomic():
                changed, folded = refresh_catalog_keys(family=family_obj)
                if dry_run:
                    raise DryRun
        except DryRun:
            self.stdout.write(
                self.style.WARNING(f"Dry run: {changed} would change, {folded} would be folded.")
            )
            return
        self.stdout.write(self.style.SUCCESS(f"{changed} changed, {folded} folded."))
