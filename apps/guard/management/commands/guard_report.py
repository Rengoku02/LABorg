from django.core.management.base import BaseCommand

from apps.guard import baseline
from apps.guard.records import lab_records
from apps.guard.reports import precision_report, uncheckable_share
from apps.quartzy.management.commands._shared import get_lab


def _share(value):
    return "—" if value is None else f"{value:.0%}"


class Command(BaseCommand):
    help = (
        "Per-tier precision and go-live gates of the duplicate guard, and the share of "
        "requests it can't check next to the Phase 0 baseline's (plan §9)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--lab", help="Lab slug (optional with one lab)")
        parser.add_argument(
            "--no-baseline", action="store_true", help="Skip the baseline comparison"
        )

    def handle(self, *args, lab, no_baseline, **options):
        lab = get_lab(lab)
        rows = precision_report(lab)
        self.stdout.write("Tier            found  labelled  same  precision  gate")
        for row in rows:
            name = ("  " if row.kind else "") + row.name
            precision = "—" if row.precision is None else f"{row.precision:.2f}"
            gate = "PASSES" if row.gate_ok else "not yet"
            self.stdout.write(
                f"{name:<15} {row.matches:>5}  {row.labelled:>8}  {row.true_dup:>4}  "
                f"{precision:>9}  {gate}"
            )
        if not rows:
            self.stdout.write("  (no matches yet: run guard_replay, or wait for new requests)")
        if rows:
            first = rows[0]
            self.stdout.write(
                f"Gate: at least {first.min_labelled} labelled and precision at least "
                f"{first.min_precision:.2f}."
            )

        for replay, title in ((False, "Live checks"), (True, "Replay")):
            share = uncheckable_share(lab, replay=replay)
            reasons = ", ".join(f"{n} {r}" for r, n in share["by_reason"].items() if n)
            self.stdout.write(
                f"{title}: {share['checked']} checked, {share['uncheckable']} can't be checked "
                f"({_share(share['share'])}){': ' + reasons if reasons else ''}"
                + (f"; {share['unresolved']} not resolved yet" if share["unresolved"] else "")
            )
        if no_baseline:
            return
        result = baseline.find_clusters(lab_records(lab))
        uncheckable = {reason: len(rows) for reason, rows in result.uncheckable.items()}
        total = result.compared + sum(uncheckable.values())
        reasons = ", ".join(f"{n} {r}" for r, n in uncheckable.items() if n)
        share = sum(uncheckable.values()) / total if total else None
        self.stdout.write(
            f"Baseline (all history): {total} compared or uncheckable, "
            f"{sum(uncheckable.values())} can't be checked ({_share(share)})"
            f"{': ' + reasons if reasons else ''}; {len(result.pairs)} duplicate pair(s)."
        )
