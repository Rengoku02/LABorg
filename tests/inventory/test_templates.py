"""No template reads a stock item's or a location's internals directly.

Templates render presenter views (apps.inventory.presenters), and by convention bind them to
names ending in ``_view``: ``{{ item_view.location_path }}``, ``{% for box_view in
box_views %}`` (docs/design/mvp-a.md §0). The scan fails a template, whatever the object:

- on a read of an attribute that names a place (anything with "location" in it:
  ``.location``, ``.location_id``, ``.to_location``), a place's path or temperature
  (``.path_text``, ``.ancestors``, ``.get_parent``…, ``.temperature``), a position, a lot,
  an expiry, a quantity or notes, from a name that doesn't end in ``_view``;
- on a stock row bound to a ``_view`` name: ``{% with item_view=item %}``, ``{% for
  item_view in items %}``, ``{% include … with item_view=item %}``, ``{% regroup items … as
  item_views %}``. A ``_view`` name takes only a ``_view`` or ``_views`` value (or part of
  one: ``match_view.item``, ``item_views.0``).

So the bench, email and digest templates can only show stock the presenter vetted. A view
test also checks what reached the template: ``stock_rows_in(response.context) == []``.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context

from apps.inventory.models import Location, StockEvent, StockItem

# What the presenter redacts, and the model API that walks to a place.
FORBIDDEN = (
    r"(?:\w+_)?locations?(?:_\w+)?",  # location, location_id, to_location; not allocation
    "position_label",
    "position_index",
    "lot",
    "notes",
    "path_text",
    "display_name",
    "ancestors",
    "get_ancestors",
    "get_parent",
    "get_root",
    "get_children",
    "get_descendants",
    "get_siblings",
    "effective_temperature",
    "temperature",
    "quantity",
    "qty_delta",
    "unit",
    "expiry_date",
    "expiry_precision",
    "opened_at",
    "container_count",
    "stock_items",
    "stock_held",
    "aliquots",
)
_COMMENTS = re.compile(r"\{#.*?#\}|\{%\s*comment\b.*?%\}.*?\{%\s*endcomment\s*%\}", re.DOTALL)
_TAGS = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_STRINGS = re.compile(r"\"[^\"]*\"|'[^']*'")
_ACCESS = re.compile(rf"(\w+)\s*\.\s*({'|'.join(FORBIDDEN)})\b")
_VIEW = re.compile(r"\w+_views?$")
# Tags that bind names: {% with a=b %} / {% with b as a %}, {% for a, b in c %},
# {% include "x" with a=b %}, {% regroup c by d as a %}.
_WITH = re.compile(r"\{%\s*with\s+(.*?)\s*%\}", re.DOTALL)
_INCLUDE = re.compile(r"\{%\s*include\s+\S+\s+with\s+(.*?)\s*%\}", re.DOTALL)
_FOR = re.compile(r"\{%\s*for\s+(.+?)\s+in\s+(\S+)", re.DOTALL)
_REGROUP = re.compile(r"\{%\s*regroup\s+(\S+)\s+by\s+.+?\s+as\s+(\w+)\s*%\}", re.DOTALL)
_ASSIGNMENT = re.compile(r"(\w+)\s*=\s*(\S+)")
_LEGACY_WITH = re.compile(r"^(\S+)\s+as\s+(\w+)$")


def _from_a_view(expression):
    """Is ``expression`` a ``_view``/``_views`` value or part of one (its first name)?"""
    root = expression.split("|")[0].split(".")[0]
    return bool(_VIEW.match(root))


def _bindings(tag):
    """``(name, expression)`` for every name the tag binds."""
    if match := _WITH.fullmatch(tag):
        legacy = _LEGACY_WITH.match(match[1])
        if legacy:
            return [(legacy[2], legacy[1])]
        return _ASSIGNMENT.findall(match[1])
    if match := _INCLUDE.fullmatch(tag):
        return _ASSIGNMENT.findall(match[1])
    if match := _FOR.match(tag):
        return [(name.strip(), match[2]) for name in match[1].split(",")]
    if match := _REGROUP.fullmatch(tag):
        return [(match[2], match[1])]
    return []


def violations(source):
    """The ``name.attribute`` reads in a template that only a ``*_view`` may make, and the
    ``name=expression`` bindings that give a ``_view`` name something that isn't a view."""
    found = []
    for tag in _TAGS.findall(_COMMENTS.sub("", source)):
        tag = _STRINGS.sub('""', tag)
        for name, attribute in _ACCESS.findall(tag):
            if not _VIEW.match(name):
                found.append(f"{name}.{attribute}")
        for name, expression in _bindings(tag):
            if _VIEW.match(name) and not _from_a_view(expression):
                found.append(f"{name}={expression}")
    return found


def stock_rows_in(context):
    """The stock items, locations and stock events in a template context (a Context, a
    ``response.context`` list of them, or a dict), also inside lists and querysets. A view
    hands templates presenter views only, so a view test asserts this is ``[]``."""
    if context is None:
        return []
    contexts = context if isinstance(context, list) else [context]
    values = []
    for one in contexts:
        values.extend((one.flatten() if isinstance(one, Context) else dict(one)).values())
    found, seen = [], set()

    def walk(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        model = getattr(value, "model", None)  # a queryset
        if isinstance(value, StockItem | Location | StockEvent):
            found.append(value)
        elif model in (StockItem, Location, StockEvent):
            found.append(value)
        elif isinstance(value, list | tuple | set | frozenset):
            for part in value:
                walk(part)
        elif isinstance(value, dict):
            for part in value.values():
                walk(part)

    for value in values:
        walk(value)
    return found


def template_files():
    """Every file under templates/ and apps/*/templates/, but hidden ones (.DS_Store)."""
    base = Path(settings.BASE_DIR)
    roots = [base / "templates", *base.glob("apps/*/templates")]
    return sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and not path.name.startswith(".")
    )


def test_no_template_reads_stock_internals():
    files = template_files()
    assert any(path.name == "base.html" for path in files)  # the scan finds the templates
    problems = {
        str(path.relative_to(settings.BASE_DIR)): found
        for path in files
        if (found := violations(path.read_text(encoding="utf-8")))
    }
    assert problems == {}, (
        "Render stock through apps.inventory.presenters and bind the views to names ending "
        f"in _view: {problems}"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{{ item.location }}", ["item.location"]),
        ("{{ item.location.name }}", ["item.location"]),
        ("{% if stock_item.notes %}x{% endif %}", ["stock_item.notes"]),
        ("{{ x.lot.lot_raw }}", ["x.lot"]),
        ("{{ i.position_label }}{{ i.position_index }}", ["i.position_label", "i.position_index"]),
        ("{% for item in items %}{{ item.notes|linebreaks }}{% endfor %}", ["item.notes"]),
        ("{% with where=match.candidate_stock_item.location %}", ["candidate_stock_item.location"]),
        ("{{\n  item.location\n}}", ["item.location"]),
        ("{{ request.lot }}", ["request.lot"]),  # any object: the convention, not the type
        ("{{ view.notes }}", ["view.notes"]),  # the suffix is _view
        ("{{ item_view.notes }} {{ box_view.location_path }} {{ item_view.lot }}", []),
        ("{% for row_view in item_views %}{{ row_view.position_index }}{% endfor %}", []),
        ("{{ item.lot_text }} {{ item.notes_count }} {{ request.unit_price }}", []),
        ("{{ grant.allocation }} {{ row.relocated }}", []),
        ("{# item.location #}{% comment %}{{ item.notes }}{% endcomment %}", []),
        ("Plain text mentioning item.location and box.notes.", []),
        ('{{ value|default:"see item.location" }}', []),
        # Anything naming a place, its path or its temperature, on any object.
        ("{{ item.location_path }}", ["item.location_path"]),
        ("{{ item.location_id }}", ["item.location_id"]),
        ("{% for e in item.events.all %}{{ e.to_location }}{% endfor %}", ["e.to_location"]),
        ("{{ event.from_location.name }}", ["event.from_location"]),
        ("{{ instance.location_path }}", ["instance.location_path"]),  # Quartzy's mirror
        ("{{ box.path_text }} {{ box.display_name }}", ["box.path_text", "box.display_name"]),
        ("{% for a in box.ancestors %}{% endfor %}", ["box.ancestors"]),
        ("{{ box.get_parent }} {{ shelf.get_children }}", ["box.get_parent", "shelf.get_children"]),
        ("{{ loc.temperature }}", ["loc.temperature"]),
        ("{{ loc.effective_temperature }}", ["loc.effective_temperature"]),
        # What the presenter redacts besides the place.
        ("{{ item.quantity }} {{ item.unit }}", ["item.quantity", "item.unit"]),
        ("{{ event.qty_delta }}", ["event.qty_delta"]),
        ("{{ i.expiry_date }}{{ i.expiry_precision }}", ["i.expiry_date", "i.expiry_precision"]),
        ("{{ i.opened_at }}{{ i.container_count }}", ["i.opened_at", "i.container_count"]),
        # Stock rows listed without the presenter.
        ("{% for i in box.stock_items.all %}{% endfor %}", ["box.stock_items"]),
        ("{{ user.stock_held.count }} {{ item.aliquots }}", ["user.stock_held", "item.aliquots"]),
        # A stock row bound to a _view name.
        ("{% with item_view=item %}{{ item_view.location_path }}{% endwith %}", ["item_view=item"]),
        ("{% with item as item_view %}{% endwith %}", ["item_view=item"]),
        ("{% include 'bench/row.html' with item_view=item only %}", ["item_view=item"]),
        ("{% include 'bench/row.html' with box_view=box_view n=3 %}", []),
        ("{% for item_view in items %}{% endfor %}", ["item_view=items"]),
        ("{% for item_view in item_views reversed %}{% endfor %}", []),
        ("{% for key, group_view in groups.items %}{% endfor %}", ["group_view=groups.items"]),
        ("{% for key, group_view in group_views.items %}{% endfor %}", []),
        ("{% regroup items by product as item_views %}", ["item_views=items"]),
        ("{% regroup item_views by product_name as groups %}", []),
        ("{% with item_view=match_view.item first_view=item_views|first %}{% endwith %}", []),
    ],
)
def test_the_scan_itself(source, expected):
    assert violations(source) == expected


@pytest.mark.django_db
def test_a_view_test_sees_the_stock_rows_a_template_was_given(item, box, raj):
    from apps.inventory.presenters import present_item
    from apps.inventory.selectors import location_contents

    view = present_item(item, raj, channel="page")
    assert stock_rows_in([Context({"item_view": view, "item_views": [view], "n": 1})]) == []
    assert stock_rows_in(Context({"item": item})) == [item]
    assert stock_rows_in({"rows": [{"box": box}]}) == [box]
    contents = location_contents(box)
    assert stock_rows_in([Context({"title": "Box 4"}), Context({"items": contents})]) == [contents]
    assert stock_rows_in({"event": item.events.get()}) == [item.events.get()]
    assert stock_rows_in(None) == []
