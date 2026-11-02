"""Forms of the bench pages. Field names avoid what the template scan reserves for presenter
views (``location``, ``temperature``, ``lot``, ``notes``, ``quantity``…): ``place``,
``temp``, ``lot_number``, ``remarks``."""

import re
from datetime import date

from django import forms

from apps.catalog.models import ItemType
from apps.core.models import Membership, User
from apps.inventory.claims import Outcome
from apps.inventory.intake import INTAKE_SOURCES, MAX_COUNT
from apps.inventory.models import Location, Source, Temperature, Visibility
from apps.inventory.presenters import present_locations

PLACE_LATER = ""


def place_choices(lab, viewer, *, structural_only=False, include_later=True):
    """[(code, path)] of the active places ``viewer`` may see in full (never another person's
    restricted box), in tree order. "" = place later (the unsorted container)."""
    rows = (
        Location.objects.for_lab(lab)
        .physical()
        .filter(active=True)
        .select_related("location_type", "default_custodian", "unsorted_owner")
        .order_by("path")
    )
    if structural_only:
        rows = rows.filter(location_type__structural=True)
    views = present_locations(list(rows), viewer, channel="page")
    choices = [(view.code, view.path) for view in views if not view.redacted]
    if include_later:
        choices.insert(0, (PLACE_LATER, "Place later (unsorted)"))
    return choices


def member_choices(lab, *, exclude=None):
    """Current members of the lab, by name."""
    people = []
    for membership in Membership.objects.filter(lab=lab).select_related("user"):
        user = membership.user
        if membership.is_current() and user.kind == User.Kind.MEMBER and user != exclude:
            people.append((str(user.pk), str(user)))
    return sorted(people, key=lambda pair: pair[1].casefold())


def temp_field(required=False):
    return forms.ChoiceField(
        label="Temperature",
        choices=[("", "Not known"), *Temperature.choices],
        required=required,
    )


def parse_expiry(value):
    """ "2027-03" -> (date(2027, 3, 1), "month"); "2027-03-14" -> (date, "day")."""
    value = (value or "").strip()
    if not value:
        return None, "day"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return date.fromisoformat(f"{value}-01"), "month"
    return date.fromisoformat(value), "day"


class ClaimForm(forms.Form):
    outcome = forms.ChoiceField(
        choices=[
            (Outcome.STILL_HAVE, "Still have it"),
            (Outcome.USED_UP, "Used up"),
            (Outcome.GAVE_AWAY, "Gave it to someone"),
            (Outcome.SHARED_STOCK, "In the shared stock"),
            (Outcome.NOT_MINE, "Not mine"),
            (Outcome.NOT_ARRIVED, "Did this arrive?"),
            (Outcome.SKIPPED, "Later"),
        ]
    )
    count = forms.IntegerField(min_value=1, max_value=MAX_COUNT, required=False, initial=1)
    place = forms.ChoiceField(label="Where", required=False)
    temp = temp_field()
    given_to = forms.ChoiceField(label="Given to", required=False)
    same_item = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, lab, viewer, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["place"].choices = place_choices(lab, viewer)
        self.fields["given_to"].choices = [("", "Choose…"), *member_choices(lab, exclude=viewer)]


class AddForm(forms.Form):
    product = forms.IntegerField(required=False, widget=forms.HiddenInput)
    name = forms.CharField(label="What is it?", max_length=500, required=False)
    vendor = forms.CharField(max_length=300, required=False)
    catalog_number = forms.CharField(label="Catalog #", max_length=200, required=False)
    item_type = forms.ChoiceField(label="Type", required=False)
    source = forms.ChoiceField(
        label="Where did it come from?",
        choices=[(value, Source(value).label) for value in INTAKE_SOURCES],
    )
    count = forms.IntegerField(
        label="How many tubes, vials or bottles?", min_value=1, max_value=MAX_COUNT, initial=1
    )
    place = forms.ChoiceField(label="Where is it?", required=False)
    temp = temp_field()
    share = forms.ChoiceField(
        label="Who may see where it is?",
        choices=[("", "Lab default (ask first)"), *Visibility.choices],
        required=False,
    )
    lot_number = forms.CharField(label="Lot", max_length=200, required=False)
    expiry = forms.CharField(label="Expiry (YYYY-MM or YYYY-MM-DD)", required=False)
    mta = forms.BooleanField(label="Received under an MTA", required=False)
    remarks = forms.CharField(label="Notes", widget=forms.Textarea, required=False)
    client_uuid = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, lab, viewer, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["place"].choices = place_choices(lab, viewer)
        types = ItemType.objects.order_by("tier", "label").values_list("key", "label")
        self.fields["item_type"].choices = [("", "Not sure"), *types]

    def clean_expiry(self):
        try:
            return parse_expiry(self.cleaned_data["expiry"])
        except ValueError as exc:
            raise forms.ValidationError("Write it as YYYY-MM or YYYY-MM-DD.") from exc

    def clean(self):
        data = super().clean()
        if not (data.get("product") or data.get("name") or data.get("catalog_number")):
            raise forms.ValidationError("Say what it is: pick a product, or give a name.")
        return data


class BoxForm(forms.Form):
    name = forms.CharField(label="Box name", max_length=200)
    parent = forms.ChoiceField(label="Where is the box?")
    kind = forms.ChoiceField(
        choices=[("box", "Box, 9 by 9"), ("box_10x10", "Box, 10 by 10"), ("bag", "Bag")]
    )
    owner = forms.ChoiceField(label="Whose box is it?")
    tags = forms.MultipleChoiceField(
        label="What's in it?", required=False, widget=forms.CheckboxSelectMultiple
    )
    share = forms.ChoiceField(
        label="Who may see where it is?",
        choices=[("", "Lab default (ask first)"), *Visibility.choices],
        required=False,
    )
    photo = forms.FileField(label="Photo of the lid", required=False)
    remarks = forms.CharField(label="Notes", widget=forms.Textarea, required=False)

    def __init__(self, *args, lab, viewer, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].choices = place_choices(
            lab, viewer, structural_only=True, include_later=False
        )
        self.fields["owner"].choices = member_choices(lab)
        self.fields["owner"].initial = str(viewer.pk)
        types = ItemType.objects.order_by("tier", "label").values_list("key", "label")
        self.fields["tags"].choices = list(types)


class ShareForm(forms.Form):
    share = forms.ChoiceField(choices=Visibility.choices)
    version = forms.IntegerField(widget=forms.HiddenInput)
