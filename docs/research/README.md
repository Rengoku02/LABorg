# Research behind the plan (2026-10-27)

Raw outputs, kept for reference:

- `2026-10-27-research-workflow.json`: six research agents plus synthesis and critique.
  - `.result.research[]` holds one entry per angle: `commercial`, `opensource`, `quartzy`
    (with an adversarial `verification` block), `domain`, `dupes_adoption`, `tech`.
  - `.result.synthesis` and `.result.critique` hold the feature catalog and the critic's corrections.
- `2026-10-27-plan-review.json`: a two-lens review of the draft plan (engineering and lab outcome).
  The fixes are already in `../plan.md`.
- `quartzy-public-api.yaml`: Quartzy's OpenAPI spec as downloaded on 2026-10-27
  (sha256 `a889377c…cb73`). A weekly job diffs it against the live copy.

Pull out one part, for example the Quartzy verification:

```bash
python3 -c "import json; r=json.load(open('docs/research/2026-10-27-research-workflow.json'))['result']; \
q=[x for x in r['research'] if x['key']=='quartzy'][0]; print(json.dumps(q['verification'], indent=1))"
```

## Findings that shaped the design

- **No existing product covers the gap.** None checks a new Quartzy request against personal
  stock, other open requests and past receipts together.
  - Quartzy's own warning fires only on the same catalog # among *open* requests in the same
    Quartzy Lab.
  - Its Add Request lookup can be skipped. Quartzy Connect (Chrome) creates requests with no
    documented duplicate check.
- **Quartzy API:** 77 claims confirmed, 6 refuted and 7 unverifiable against the official spec.
  - Reads cover labs, types, order requests and inventory items.
  - Writes are limited to creating requests, changing a request's status, and setting
    inventory instance quantity.
  - Missing entirely: comments, users and roles, inventory create, DELETE.
  - There are no webhooks for approved, cancelled or updated, so polling is the primary mechanism.
  - Page base, size, sort order, rate limits and the Money unit are undocumented. The Phase-0
    probe measures them.
- **Roles:** only Lab Admins can mark a request Ordered, and Members can cancel only their own
  *New* requests. In a lab where everyone orders for themselves, statuses are therefore
  unreliable, and "probably bought" is inferred from a request's age.
- **Build vs adopt:**
  - InvenTree is the only credible open-source base, but its manufacturing vocabulary, lack of
    box grids and plugin churn rule it out.
  - Commercial tools duplicate Quartzy's purchasing, price per seat, or hide their APIs behind
    enterprise tiers.
- **Adoption:**
  - Inventory has to be created as a by-product of receiving, and personal stock surfaces
    through taps on your own past receipts rather than typing.
  - Alerts must be rare and precise: shadow mode first, with per-tier precision gates.
- **Bench reality:**
  - BSL-2 glove rules conflict with using phones.
  - iOS ships BarcodeDetector disabled, and iOS home-screen apps don't share Safari's login.
  - Hence Safari-first URL-QR labels and lab-owned stations.
- **Honest numbers:** credible public figures on duplicate spend don't exist. The lab's own
  baseline (`notebooks/baseline_duplicates.ipynb`) is the "before" figure, reviewed by a
  wet-lab co-owner.
