# LabStock privacy commitments

LabStock helps the lab avoid buying things it already has. It is **not** a tool for monitoring
individual people. These commitments come from the plan (§11). The PI repeats them in the
launch talk.

## What the PI and lab managers see

- **Lab totals only:** duplicates caught, money avoided, coverage, and categories.
- **Who holds what (custodianship):** "Priya's −20 box 4", because finding things is the point.
- **Never individual behaviour:** what someone searched for, how they answered an alert, or why
  they chose to order anyway.
- **No per-person duplicate counts or leaderboards of "who duplicated".** Lenders can be thanked
  by name only if they opt in.

## What is stored, and for how long

| Data | Why | Kept |
|---|---|---|
| Quartzy order requests (read-only copy) | Duplicate checks, "bought before" | As long as the lab uses LabStock |
| Search log | Only to mark "searched before buying" in aggregate | 30 days, then deleted |
| Alert answers (pick-list reasons) | Tuning the matcher | Aggregated; reasons are never shown per person |
| Stock events (who moved/used/emptied an item) | "Who took the last vial?" | Kept; the person's name is replaced with a pseudonym after a set retention period (decided by the PI) |

## Every alert email says

> Only you receive this; the PI sees lab totals only.

## If the lab is in the EU

Software that could monitor staff may need staff-council (or works-council) agreement and a
GDPR notice. Check with the institution before launch.
