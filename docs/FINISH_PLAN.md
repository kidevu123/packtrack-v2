# PackTrack Finish Plan

Date: 2026-06-04

## Council Check

The requested `gcpdev/llm-council-skill` was cloned and invoked. External council calls were unavailable in this shell because neither `OPENAI_API_KEY` nor `GEMINI_API_KEY` was configured, and neither fallback CLI was installed. Result: the plan below was completed with local code review and tests only.

## What v1 Does Better

- Richer Flask-era PO workflow notes, including open-PO coverage logic that combines PackTrack lines with Zoho mirrored PO quantities.
- Mature operator concepts around Zoho push status, Telegram action links, sync logs, and admin troubleshooting screens.
- More historical deployment/setup notes for Zoho and messaging integrations.

## What v2 Does Better

- Cleaner FastAPI/SQLModel architecture with one canonical workflow service.
- Postgres JSON fields, role-aware home/inbox, Jinja/htmx/Alpine/Tailwind UI, and Luma receiving concepts.
- Uploads live outside the source tree through `UPLOAD_DIR`.
- Zoho item sync and PO mirror are already gateway-aware.
- Deployment is LXC/systemd/Caddy oriented instead of PythonAnywhere.

## Missing Or Incomplete Before This Pass

- Inventory filters did not cover material code, vendor-only cleanup, missing material-code cleanup, stock-status filtering, or open-PO coverage.
- PO creation did not support save-draft versus send-to-design-review.
- Test coverage did not include workflow permissions, inventory filtering, reorder suggestions, route-level PO creation, Zoho retry, or receiving duplicate-box rules.
- Deploy defaulted to an old direct LXC IP rather than the production Proxmox host and LXC ID.
- Runbook/deployment docs and CI workflow were missing.
- Scheduler double-run behavior was documented only as a single-worker assumption.

## Implemented

- Added `packtrack.services.inventory` for inventory filtering, reorder suggestions, and open coverage from PackTrack PO lines plus Zoho mirror remaining quantities.
- Wired Inventory UI to show material codes, missing-code warnings, suggested PO quantities, CSV export, and "covered by open PO" status.
- Added owner inline edits for material code and vendor mapping.
- Added PO save-draft support while preserving send-to-design-review as the primary action.
- Made Postgres JSONB model columns portable to SQLite for route/service tests without changing Postgres behavior.
- Added scheduler job file locks for Zoho sync and push retry.
- Reworked `deploy/deploy.sh` for `PVE_HOST=192.168.1.190` and `LXC_ID=200`, with direct `LXC_HOST` override.
- Added `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, CI, and a smoke script.

## Key Files Changed

- `packtrack/services/inventory.py`
- `packtrack/routes/inventory.py`
- `packtrack/routes/purchase_orders.py`
- `packtrack/services/dashboard.py`
- `packtrack/scheduler.py`
- `packtrack/models.py`
- `packtrack/templates/inventory.html`
- `packtrack/templates/_partials/inventory_row.html`
- `packtrack/templates/po_new.html`
- `deploy/deploy.sh`
- `scripts/smoke.sh`
- `.github/workflows/ci.yml`
- `docs/FINISH_PLAN.md`
- `docs/DEPLOYMENT.md`
- `docs/RUNBOOK.md`

## Verification Steps

Run locally:

```bash
python -m compileall packtrack
ruff check .
pytest
bash scripts/smoke.sh
```

Deploy:

```bash
PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh
```

Then verify inside the LXC:

```bash
curl -fsS http://127.0.0.1:8000/healthz
systemctl status packtrack --no-pager
```
