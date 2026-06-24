# PackTrack v2

A focused PO + inventory workflow on top of Zoho Inventory. Built for one
owner sourcing packaging from China through an agent — design approves art,
agent uploads PI, owner approves, production, ship, receive.

## Stack

- Python 3.13 · FastAPI · SQLModel · PostgreSQL 17
- Jinja + htmx + Alpine + Tailwind v4
- APScheduler (Zoho sync + push retry, in-process)
- Telegram bot for non-owner roles

## Layout

```
packtrack/      app source
static/         CSS, JS (CSS rebuilt during deploy)
migrations/     Alembic
scripts/        seed_owner.py
deploy/         systemd unit + Caddyfile + deploy.sh
```

## Deploy to the LXC

```bash
bash deploy/deploy.sh --first-run    # initial bootstrap
bash deploy/deploy.sh                # subsequent updates
```

The deploy script:

1. Stages source through Proxmox `192.168.1.190` into LXC `200`.
2. Ensures `/opt/packtrack/app/.venv`, installs deps.
3. Builds Tailwind CSS on the host.
4. Runs Alembic migrations.
5. (Re)installs systemd unit and Caddyfile.
6. Restarts `packtrack.service` + `caddy.service`.
7. Hits `/healthz` to verify.

After first deploy:

```bash
ssh root@192.168.1.190 "pct exec 200 -- sudo -u packtrack bash -lc 'cd /opt/packtrack/app && . .venv/bin/activate && set -a && source /etc/packtrack/packtrack.env && set +a && python scripts/seed_owner.py'"
```

See `docs/DEPLOYMENT.md` and `docs/RUNBOOK.md` for the production runbook.

## Configuration

All secrets live in `/etc/packtrack/packtrack.env` on the LXC, owned
`root:packtrack` 640. See `.env.example` for the full list.

Fill in:

- `ZOHO_*` — Zoho Inventory OAuth (client id/secret + refresh token + org id).
- `TELEGRAM_BOT_TOKEN` — from `@BotFather`. `TELEGRAM_WEBHOOK_SECRET` is
  optional but strongly recommended; if set, Telegram will only invoke the
  webhook with that header.

Restart after changes: `systemctl restart packtrack`.

## URLs

- `/`         — role-aware inbox (the home page).
- `/po`       — flat list of all POs.
- `/po/new`   — owner creates a PO.
- `/po/<id>`  — PO detail with timeline + actions.
- `/inventory`— stock list.
- `/admin/*`  — owner-only admin screens.
- `/healthz`  — health check (db + config status).
- `/telegram/webhook` — set via `setWebhook` once exposed.

## Luma integration

PackTrack v2 is the authoritative packaging/material inventory
system. [Luma](https://github.com/kidevu123/luma) is the authoritative
tablet-production system. The boundary between them is documented in
[`docs/PACKTRACK_LUMA_BOUNDARY.md`](docs/PACKTRACK_LUMA_BOUNDARY.md):

- Luma **pulls** PackTrack data on a schedule (every 15 min during
  10:00–19:00 America/New_York, 03:59 overnight, on page load,
  manual refresh, and JIT pre-finalize) for inventory / forecast /
  BOM views.
- Luma has **two narrow write paths** back to PackTrack:
  Luma-initiated generic-material receipts
  (`POST /api/luma/material-receipts`, planned) and
  post-finalization consumption events
  (`POST /api/luma/consumption-events`, planned). See
  [`docs/PACKTRACK_API_SURFACE.md`](docs/PACKTRACK_API_SURFACE.md).
- Confidence / validation lives on **four separate axes** — never
  collapse them. See
  [`docs/PACKTRACK_CONFIDENCE_MODEL.md`](docs/PACKTRACK_CONFIDENCE_MODEL.md).

> Today's code still uses the legacy PackTrack → Luma push path
> (`packtrack/services/receiving.py::push_luma_receipt`). The pull
> APIs and Luma write paths above are planned (Phase 1+), not built.
> Phase order is in
> [`docs/PACKTRACK_BUILD_QUEUE.md`](docs/PACKTRACK_BUILD_QUEUE.md).

## Zoho receive writes

Pack Track does not call Zoho directly for purchase receives. Receives go
through
[zoho-integration-service](https://github.com/sahiwal283/zoho-integration-service)'s
`/zoho/pack_track/receive/{preview,commit}` endpoints. Configuration, flow,
failure states, and a safe smoke-test recipe live in
[`docs/PACKTRACK_ZOHO_INTEGRATION_RECEIVES.md`](docs/PACKTRACK_ZOHO_INTEGRATION_RECEIVES.md).

## Deployment context

PackTrack v2 runs on Proxmox:

- Proxmox host: `192.168.1.190` (SSH target for ops).
- LXC 200 container: `192.168.1.206`, app path `/opt/packtrack/app`,
  service `packtrack.service`. This is the address
  [`deploy/deploy.sh`](deploy/deploy.sh) rsyncs to.
- Companion: Zoho integration service at LXC 9503,
  `192.168.1.205:8000` (multi-brand FastAPI gateway, read-only).

The older Flask `packtrack` repository is deprecated. **All new work
happens in this v2 repository.** Do not extend or deploy the old repo.

## What this does NOT do

By design — see `plan/spicy-splashing-owl.md`:

- No drag-and-drop kanban (inbox + detail).
- No WhatsApp / SMS (Telegram covers it).
- No multi-tenant or manufacturer scope (single vendor).
- No composite BOM sync.
- No SQLite gymnastics — Postgres handles concurrency.
