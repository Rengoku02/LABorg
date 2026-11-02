"""Helpers for the quartzy management commands (Django skips modules starting with "_")."""

from django.core.management.base import CommandError

from apps.core.models import Lab
from apps.quartzy.models import QuartzyLab


def get_lab(slug):
    """The Lab for --lab; optional while there is only one lab."""
    if slug:
        lab = Lab.objects.filter(slug=slug).first()
        if lab is None:
            raise CommandError(f"No lab with slug {slug!r}; run bootstrap_lab first")
        return lab
    labs = list(Lab.objects.all()[:2])
    if len(labs) == 1:
        return labs[0]
    raise CommandError("Pass --lab <slug>" if labs else "No lab yet; run bootstrap_lab first")


def describe(run):
    where = f" {run.quartzy_lab}" if run.quartzy_lab_id else ""
    line = (
        f"{run.get_kind_display()}{where}: {run.status}; {run.calls} calls, {run.pages} pages, "
        f"{run.seen} seen, {run.new} new, {run.changed} changed, {run.errors} errors"
    )
    # An aborted or failed run's note says why.
    return f"{line}\n  {run.note}" if run.note and run.status != "ok" else line


def inactive_labs(lab):
    """What to say about the Quartzy labs mapped to ``lab`` that are not mirrored (e.g. a
    collaborator's lab the token also sees), and how to mirror one; "" when there are none."""
    left = QuartzyLab.objects.for_lab(lab).filter(is_active=False).order_by("name", "pk")
    names = ", ".join(f"{qlab} [{qlab.quartzy_id}]" for qlab in left)
    if not names:
        return ""
    return (
        f"Quartzy labs left inactive, so not mirrored: {names}. To mirror one, tick "
        "“Is active” on it in Admin → Quartzy mirror → Quartzy labs, then run "
        "quartzy_backfill again."
    )
