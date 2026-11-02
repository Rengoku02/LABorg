"""A demo lab to look at: synthetic people, freezers, stock, and the synthetic Quartzy history
of tests/fixtures/baseline/export-sample, replayed by the guard and ready to claim.

    manage.py seed_demo            # creates the lab "demo" (refuses if it exists)
    manage.py seed_demo --password demo-only   # also lets every demo person sign in with it

Every person, number and price is made up. Never run it against a real lab's database:
it refuses unless the lab slug is free.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.catalog.services import resolve_supplier_product
from apps.core.models import Lab, Membership, User
from apps.guard.replay import replay
from apps.inventory import services
from apps.inventory.claims import surface_claims
from apps.quartzy import sync
from apps.quartzy.models import OrderStatusObservation, QuartzyLab, SyncRun

SAMPLE = Path(settings.BASE_DIR) / "tests" / "fixtures" / "baseline" / "export-sample"
PEOPLE = (
    # email (as the sample's Quartzy users), name, role, champion
    ("pat.investigator@example.edu", "Pat Investigator", Membership.Role.PI, False),
    ("morgan.hale@example.edu", "Morgan Hale", Membership.Role.CO_OWNER, False),
    ("priya.patel@example.edu", "Priya Patel", Membership.Role.MEMBER, True),
    ("raj.kumar@example.edu", "Raj Kumar", Membership.Role.MEMBER, False),
    ("lee.chen@example.edu", "Lee Chen", Membership.Role.MEMBER, False),
    ("sam.ortiz@example.edu", "Sam Ortiz", Membership.Role.MEMBER, False),
    ("dana.fox@example.edu", "Dana Fox", Membership.Role.MEMBER, False),
    ("alex.kim@example.edu", "Alex Kim", Membership.Role.MEMBER, False),
)
STOCK = (
    # holder, vendor, catalog, name, type, place, sharing, lifecycle
    ("raj", "CST", "2118S", "GAPDH (14C10) Rabbit mAb", "antibody", "box1", "shared", "available"),
    ("priya", "Abcam", "ab8245", "Anti-GAPDH antibody [6C5]", "antibody", "box2", "restricted",
     "available"),
    ("lee", "NEB", "R0101S", "EcoRI", "enzyme", "box1", "ask_first", "available"),
    ("sam", "Gibco", "15140-122", "Penicillin-Streptomycin", "media_buffer", None, "shared",
     "unverified"),
    ("dana", "Sigma", "A2220-5ML", "ANTI-FLAG M2 Affinity Gel", "antibody", "box2", "ask_first",
     "available"),
)  # fmt: skip


class Command(BaseCommand):
    help = "Create a demo lab with synthetic people, stock and Quartzy history."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="demo")
        parser.add_argument(
            "--password", default="", help="Let every demo person sign in with this password"
        )

    def handle(self, *args, slug, password, **options):
        if Lab.objects.filter(slug=slug).exists():
            raise CommandError(f"A lab {slug!r} exists already; the demo only makes a new one.")
        call_command("seed_catalog", verbosity=0)
        call_command("seed_inventory", verbosity=0)
        call_command("bootstrap_lab", name="Demo Lab", slug=slug, verbosity=0)
        lab = Lab.objects.get(slug=slug)
        with transaction.atomic():
            people = self._people(lab, password)
            places = self._places(lab, people)
            history = self._history(lab)
            self._stock(lab, people, places)
        run = replay(lab, months=24)
        surfaced = surface_claims(lab)
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo lab {slug!r}: {len(people)} people, {history} Quartzy requests, "
                f"{run.requests} replayed, {surfaced} claims waiting."
            )
        )
        if password:
            self.stdout.write("Sign in with any demo email, e.g. priya.patel@example.edu.")

    def _people(self, lab, password):
        found = {}
        for email, name, role, champion in PEOPLE:
            user = User.objects.filter(email=email).first() or User.objects.create_user(
                email=email, full_name=name, password=password or None
            )
            Membership.objects.get_or_create(
                user=user, lab=lab, defaults={"role": role, "is_champion": champion}
            )
            found[email.split(".")[0]] = user
        return found

    def _places(self, lab, people):
        def make(name, kind, parent=None, **fields):
            return services.create_location(
                lab=lab, parent=parent, name=name, location_type=kind, user=None, **fields
            )

        room = make("Room 210", "room")
        freezer = make("-80 A", "freezer", room, temperature="-80C")
        shelf = make("Shelf 1", "shelf", freezer)
        fridge = make("4C door", "fridge", room, temperature="4C")
        return {
            "box1": make("Box 1", "box", shelf, default_custodian=people["raj"]),
            "box2": make(
                "Priya's antibodies",
                "box",
                shelf,
                default_custodian=people["priya"],
                visibility="restricted",
            ),
            "fridge": fridge,
        }

    def _history(self, lab):
        """The sample export, moved forward so its newest request is from yesterday."""
        manifest = json.loads((SAMPLE / "manifest.json").read_text())
        lines = (SAMPLE / "order_requests.jsonl").read_text().splitlines()
        payloads = [json.loads(line) for line in lines]
        newest = max(datetime.fromisoformat(p["requested_at"]) for p in payloads)
        shift = timezone.now() - timedelta(days=1) - newest
        labs = {row["id"]: row for row in json.loads((SAMPLE / "labs.json").read_text())}
        for quartzy_id in manifest["lab_ids"]:
            qlab = QuartzyLab.objects.create(
                lab=lab,
                quartzy_id=f"demo-{quartzy_id}",
                name=labs[quartzy_id]["name"],
                alerts_armed_at=timezone.now(),
            )
            run = SyncRun.objects.create(kind=SyncRun.Kind.BACKFILL, quartzy_lab=qlab)
            for payload in payloads:
                if payload["lab"]["id"] != quartzy_id:
                    continue
                for key in ("requested_at", "updated_at"):
                    if payload.get(key):
                        moved = datetime.fromisoformat(payload[key]) + shift
                        payload[key] = moved.isoformat()
                payload["id"] = f"demo-{payload['id']}"
                payload["lab"] = {**payload["lab"], "id": qlab.quartzy_id}
                sync.upsert_order_request(
                    qlab, payload, via=OrderStatusObservation.Via.BACKFILL, run=run
                )
        return len(payloads)

    def _stock(self, lab, people, places):
        for holder, vendor, catalog, name, kind, place, sharing, lifecycle in STOCK:
            resolution = resolve_supplier_product(
                vendor_name=vendor, catalog_number=catalog, item_name=name, item_type=kind
            )
            person = people[holder]
            services.create_stock_item(
                lab=lab,
                product=resolution.product,
                supplier_product=resolution.supplier_product,
                custodian=person,
                user=person,
                source="purchased",
                location=places.get(place) if place else None,
                temperature="4C" if place is None else None,
                visibility=sharing,
                lifecycle=lifecycle,
            )
