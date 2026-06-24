# PACKTRACK-SEPARATION-P0 — Review Draft

> **This is a working draft for your review, not part of the
> permanent docs.** It lists exactly what was changed in this
> Phase 0 docs-only pass, plus the would-be `.env.example` update,
> semver bump, commit message, and push that were **not** performed
> because plan mode is active. Delete this file after Phase 0 is
> committed, or tell me to remove it before commit.

---

## 1. Files changed (docs-only)

Modified:

- `docs/PACKTRACK_LUMA_BOUNDARY.md` — full rewrite. Removes
  "PackTrack never sees burn events", "PackTrack must not decrement
  post-receipt stock", "push-only" language. Adds: new ownership
  split, pull-first model, two narrow Luma write paths, packaging
  items vs materials terminology, four-axis confidence model,
  high-risk Luma lot warning, production-counting rule
  ("packing is counting"), explicit § 10 Supersedes section listing
  the retired claims.
- `docs/CURRENT_PHASE_STATUS.md` — updated header to reflect Phase 0
  active / Phase 1 next; rewrote "Current PackTrack ↔ Luma boundary
  state" section to describe live code honestly (push-only, no
  ledger, two receive paths, single `confidence` enum) and to point
  to the new target architecture via the boundary doc; updated risk
  table entries 2, 3, 5, 6; added new risk entries 9 (single
  confidence enum), 10 (service-token middleware), 11 (Luma lot
  tables); replaced "What is not to be touched yet" with a Phase-0
  specific list.
- `docs/PACKTRACK_BUILD_QUEUE.md` — prepended a new "Master plan
  (Phase 0 → Phase 6)" section as the current direction; preserved
  the original P0/P1/P1.5 entries verbatim under "Historical /
  current-behavior queue"; marked P2 / P3 / P4 / P5 / P6 / P7 / P9
  with status callouts (`[done in code, superseded by master plan]`,
  `[partial, superseded by master plan]`, `[deferred]`); updated the
  historical phase-dependency graph to clarify it is historical only.

Created:

- `docs/PACKTRACK_API_SURFACE.md` — planned-only spec for the four
  read endpoints (`GET /api/luma/items`, `/items/{material_code}`,
  `/receipts`, `/stock-summary`) and the two write endpoints
  (`POST /api/luma/material-receipts`,
  `POST /api/luma/consumption-events`). Includes auth, idempotency,
  pagination, error codes, what each endpoint must not do, and an
  explicit "endpoints intentionally not in scope" section.
- `docs/PACKTRACK_CONFIDENCE_MODEL.md` — definitions for the four
  axes (`receipt_source`, `receipt_validation_status`,
  `production_confidence`, `forecast_confidence`), enums, the FIFO
  validation-status transition rule with worked example, "why
  supplier-declared remains historical" rationale, and the
  packing-is-counting rule.

Updated:

- `README.md` — added a new "Luma integration" section (boundary
  doc link, pull schedule summary, two write paths, four-axis
  confidence reminder, callout that today's code is still
  push-only); added a new "Deployment context" section clarifying
  Proxmox host (`192.168.1.190`) vs LXC 200 container
  (`192.168.1.206`) vs companion Zoho gateway (`192.168.1.205:8000`);
  added an explicit note that the old Flask `packtrack` repo is
  deprecated.

Not changed (per scope):

- No `.py` files.
- No Alembic migration.
- No template.
- No `.env.example` (deferred until you approve — see § 4 below).
- No `pyproject.toml` / `packtrack/__init__.py` version bump
  (deferred until you approve — see § 4 below).
- No Luma repository file.
- No deploy script.
- No `wipe.py` (the unrelated `box_receipts` omission is out of
  scope here).

## 2. Diff stat

```
 README.md                       |  41 ++++
 docs/CURRENT_PHASE_STATUS.md    | 136 +++++++++---
 docs/PACKTRACK_BUILD_QUEUE.md   | 310 ++++++++++++++++++++++++++--
 docs/PACKTRACK_LUMA_BOUNDARY.md | 443 ++++++++++++++++++++++++++++++----------
 4 files changed, 770 insertions(+), 160 deletions(-)
```

Plus three new docs:

- `docs/PACKTRACK_API_SURFACE.md`
- `docs/PACKTRACK_CONFIDENCE_MODEL.md`
- `docs/PACKTRACK_SEPARATION_P0_CHANGES.md` (this file)

## 3. Key architecture decisions now documented

These are the calls I made while writing. Tell me if you want any of
them revised before commit.

- **PackTrack owns the authoritative stock ledger and all
  inventory deductions** — including those triggered by Luma
  consumption events. Reverses the prior "PackTrack never
  decrements" position.
- **Pull-first integration with the user-stated schedule** — every
  15 min during 10:00–19:00 America/New_York, 03:59 overnight, on
  page load, manual refresh, JIT pre-finalize. JIT pull is a soft
  banner (not a hard block) so slow PackTrack does not stall
  production.
- **Exactly two Luma write paths** — Luma-initiated generic-material
  receipt (materials only, never packaging items) and
  post-finalization consumption event. Everything else from Luma to
  PackTrack is read-only.
- **Four-axis confidence model with FIFO validation transitions** —
  `receipt_source` (never rewritten) and `receipt_validation_status`
  (mutates over time) on PackTrack; `production_confidence` on
  Luma; `forecast_confidence` computed by Luma view layer (not
  stored). Consumption events apply to the oldest-non-fully-validated
  receipt for that `material_code` first. Validation-status
  thresholds (95–105% → `VALIDATED`, > 110% → `OVER_CONSUMED`)
  documented as planning numbers and tunable in Phase 5 via
  `app_settings`.
- **Packing-is-counting rule preserved** — consumption-event
  quantities are counted evidence, not estimates. Operators can
  signal weak evidence via `production_confidence: LOW` or the
  damaged/discarded/returned fields, never by downgrading
  `consumed_quantity` itself.
- **`item_class` axis is planned for Phase 1** (PackTrack `Item`
  column with `PACKAGING_ITEM | MATERIAL`). Until then, the
  distinction lives only in the boundary doc.
- **Auth strategy** — reuse the existing `LUMA_PACKTRACK_SECRET`
  symmetrically for inbound calls via a new
  `packtrack/services/api_auth.py::require_service_token`
  dependency (Phase 1). If security review later wants distinct
  inbound/outbound secrets, that is an additive change.
- **Legacy push path is retained for back-compat** — current code
  in `packtrack/services/receiving.py::push_luma_receipt` is
  classified as legacy current behavior; demotion or removal
  decided in Phase 6.
- **Zoho `adjust_stock` push for receipts stays** — financial /
  COGS purposes. **Consumption is never pushed to Zoho.**
- **Luma production tables are out of scope for the entire
  project** — `workflow_events`, `batches`, `finished_lots`,
  `finished_lot_inputs`, `finished_lot_raw_bags`,
  `finished_lot_packaging_lots`, and `packaging_lots.qtyOnHand`
  writers. PackTrack never writes them.

## 4. Skipped because plan mode is active (awaiting your approval)

If you re-allow agent mode I will, in this order:

### 4.A `.env.example` update (planned content)

Add commented placeholders for currently-set-on-LXC variables that
are missing from the example file. No real secret values. Exact
intended additions:

```
# Luma integration (current — used by push_luma_receipt + register_material_with_luma)
LUMA_RECEIPT_WEBHOOK_URL=
LUMA_PACKTRACK_SECRET=

# Zoho Integration Service gateway (current — used by sync_items / sync_open_pos)
ZOHO_GATEWAY_URL=
ZOHO_GATEWAY_TOKEN=
ZOHO_GATEWAY_BRAND=

# Future (planned for Phase 1+): same LUMA_PACKTRACK_SECRET will be
# reused inbound via require_service_token on /api/luma/* endpoints.
```

`APP_BASE_URL=http://192.168.1.206` line stays as-is (still the
correct container IP per the deployment context section in README).

### 4.B Semver bump

- Current version: `2.1.2` (in `pyproject.toml` line 3 and
  `packtrack/__init__.py::__version__`).
- Proposed: **`2.1.3`** — docs-only patch bump. Per semver, docs do
  not strictly require a release but you explicitly asked for one.
- Files to bump: `pyproject.toml`, `packtrack/__init__.py`.

### 4.C Commit

Suggested message (per your task spec, verbatim):

```
docs: realign Luma PackTrack ownership boundary
```

Single commit covering: all the docs in § 1, README.md, .env.example
update from § 4.A, version bump from § 4.B.

### 4.D Push

`git push origin main` (no tags unless you want one — please confirm
whether `v2.1.3` tag is desired).

## 5. Open / unresolved questions

Surface for explicit decision before Phase 1 starts. Not blocking
the Phase 0 commit.

- **Tag the release?** A `v2.1.3` tag is conventional but not in
  your task spec. Default to no tag unless you say otherwise.
- **Delete `docs/PACKTRACK_SEPARATION_P0_CHANGES.md` before
  commit?** It is meta-documentation about this commit; once
  reviewed, it can either (a) be deleted before commit, (b) be
  committed as a "what landed in Phase 0" companion, or (c) be
  renamed to `docs/_drafts/` and gitignored. Default: **delete
  before commit** unless you say otherwise.
- **`plan/spicy-splashing-owl.md` reference in README line 115.**
  That file does not exist in this repo. Not changed in this pass
  (out of Phase 0 scope) but flagged for a tiny follow-up cleanup.
- **`wipe.py` omission of `box_receipts`.** Pre-existing bug
  flagged in the audit; not in Phase 0 scope. Carry into a separate
  hygiene PR.
- **Validation-status threshold tuning** (95–105% / > 110%) is in
  the confidence doc as planning numbers. Final values land in
  Phase 5; confirm before then.
- **Push path fate in Phase 6** — demote to admin "force push" tool
  vs remove. Documented as a Phase 6 decision; flag here for
  visibility.

## 6. Verification commands

To double-check no non-doc files moved:

```bash
git status --short
git diff --stat -- ':!docs' ':!README.md' ':!.env.example'   # should be empty after § 4.A
```

The first command (run during this pass) shows:

```
 M README.md
 M docs/CURRENT_PHASE_STATUS.md
 M docs/PACKTRACK_BUILD_QUEUE.md
 M docs/PACKTRACK_LUMA_BOUNDARY.md
?? docs/PACKTRACK_API_SURFACE.md
?? docs/PACKTRACK_CONFIDENCE_MODEL.md
?? docs/PACKTRACK_SEPARATION_P0_CHANGES.md
```

No `.py`, no migration, no template, no Luma file changed. No
production access. No deploy. No restart. No commit yet.
