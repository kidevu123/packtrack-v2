# Deployment

PackTrack deploys to LXC `200` through Proxmox at `192.168.1.190`.

## First Run

On the LXC, create `/etc/packtrack/packtrack.env` from `.env.example` and fill in secrets. Keep it owned by `root:packtrack` with mode `640`.

Minimum required values:

```bash
PACKTRACK_SECRET_KEY=...
DATABASE_URL=postgresql+psycopg://packtrack:...@127.0.0.1:5432/packtrack
APP_BASE_URL=http://<lxc-or-proxy-url>
UPLOAD_DIR=/opt/packtrack/uploads
LOG_DIR=/var/log/packtrack
```

Deploy from this repo:

```bash
PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh --first-run
```

Seed the first owner after the app is installed:

```bash
ssh root@192.168.1.190 "pct exec 200 -- sudo -u packtrack bash -lc 'cd /opt/packtrack/app && . .venv/bin/activate && set -a && source /etc/packtrack/packtrack.env && set +a && python scripts/seed_owner.py'"
```

## Updates

```bash
PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh
```

The deploy script:

- Checks SSH access to Proxmox and `pct status 200`.
- Bundles source without `.git`, `.venv`, caches, logs, or uploads.
- Pushes the bundle into the LXC with `pct push`.
- Preserves `/opt/packtrack/uploads`.
- Installs dependencies in `/opt/packtrack/app/.venv`.
- Builds Tailwind CSS.
- Runs Alembic migrations.
- Installs systemd and backup units.
- Restarts PackTrack and Caddy.
- Verifies `/healthz`.

## Direct Container SSH

If the container has its own reachable SSH address:

```bash
LXC_HOST=<container-ip> bash deploy/deploy.sh
```

## Rollback

Use Proxmox snapshots/backups for full rollback. For database/file rollback, see `docs/RUNBOOK.md`.
