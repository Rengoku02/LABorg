from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import Lab, User


class Command(BaseCommand):
    help = "Create a lab and its 'Lab stock' pool user (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True)
        parser.add_argument("--slug", required=True)
        parser.add_argument("--time-zone", default="UTC")
        parser.add_argument("--contact-email", default="")

    @transaction.atomic
    def handle(self, *args, name, slug, time_zone, contact_email, **options):
        lab, created = Lab.objects.get_or_create(
            slug=slug,
            defaults={"name": name, "time_zone": time_zone, "contact_email": contact_email},
        )
        if lab.pool_user is None:
            lab.pool_user = User.objects.create_user(
                email=None, full_name=f"{lab.name} — lab stock", kind=User.Kind.POOL
            )
            lab.save(update_fields=["pool_user"])
        verb = "Created" if created else "Found"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} lab {lab.slug} (pool user {lab.pool_user_id})")
        )
