"""Create or update accounts and memberships from the lab roster (closed signup).

CSV columns: email, full_name, role, status, start_date, end_date, quartzy_lab_admin, champion
Only email and full_name are required. Dry run by default; pass --commit to save.
"""

import csv
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.models import Lab, Membership, User

TRUE = {"1", "y", "yes", "true", "x"}


def _flag(value):
    return (value or "").strip().lower() in TRUE


def _date(value):
    value = (value or "").strip()
    return date.fromisoformat(value) if value else None


class DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Provision lab members from a roster CSV (dry run unless --commit)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--lab", required=True, help="Lab slug")
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, csv_path, lab, commit, **options):
        try:
            lab_obj = Lab.objects.get(slug=lab)
        except Lab.DoesNotExist as exc:
            raise CommandError(f"No lab with slug {lab!r}; run bootstrap_lab first") from exc
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        roles = {value for value, _ in Membership.Role.choices}
        statuses = {value for value, _ in Membership.Status.choices}
        try:
            with transaction.atomic():
                for line, row in enumerate(rows, start=2):
                    email = (row.get("email") or "").strip().lower()
                    name = (row.get("full_name") or "").strip()
                    if not email or not name:
                        raise CommandError(f"Line {line}: email and full_name are required")
                    role = (row.get("role") or "member").strip().lower()
                    status = (row.get("status") or "active").strip().lower()
                    if role not in roles or status not in statuses:
                        raise CommandError(f"Line {line}: bad role {role!r} or status {status!r}")
                    user, user_created = User.objects.get_or_create(
                        email=email, defaults={"full_name": name}
                    )
                    if not user_created and user.full_name != name:
                        user.full_name = name
                        user.save(update_fields=["full_name"])
                    _membership, m_created = Membership.objects.update_or_create(
                        user=user,
                        lab=lab_obj,
                        defaults={
                            "role": role,
                            "status": status,
                            "start_date": _date(row.get("start_date")) or date.today(),
                            "end_date": _date(row.get("end_date")),
                            "is_quartzy_lab_admin": _flag(row.get("quartzy_lab_admin")),
                            "is_champion": _flag(row.get("champion")),
                        },
                    )
                    verb = "add" if m_created else "update"
                    self.stdout.write(f"{verb}: {name} <{email}> {role}/{status}")
                if not commit:
                    raise DryRun
        except DryRun:
            self.stdout.write(
                self.style.WARNING(f"Dry run: {len(rows)} row(s); nothing saved. Use --commit.")
            )
            return
        self.stdout.write(self.style.SUCCESS(f"Saved {len(rows)} row(s)."))
