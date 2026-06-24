# Follow-up B — Prevent deploy/deploy.sh from silently erasing LXC-only files

**Why now:** During the v2.2.0 deploy, an `rsync -a --delete` from the
workstation to `/opt/packtrack/app` removed three alembic migration source
files (`dc6c48337264_material_consumption_events.py`,
`58b071f4cab1_sales_events.py`,
`f4a5b6c7d8e9_forecast_alert_sent_stock.py`) that had been written
**directly on the LXC** and never pushed to GitHub. The DB was at the
head those migrations defined, so `alembic upgrade head` then failed
with "Can't locate revision identified by 'f4a5b6c7d8e9'" until we
recovered the files from compiled `__pycache__/*.pyc` bytecode.

This is a footgun. The deploy script will eat any other local edits the
same way — secrets, configs (yes, `.env` already has a symlink guard,
but only one), patches, and one-off scripts.

## Where

- `deploy/deploy.sh` line 18-22:
  ```
  rsync -a --delete \
    --exclude '__pycache__' --exclude '.venv' --exclude '.ruff_cache' \
    --exclude '.pytest_cache' --exclude 'uploads' --exclude '.git' \
    --exclude 'tailwindcss' --exclude '*.pyc' \
    ./ "root@${LXC_HOST}:${APP_DIR}/"
  ```
- The `--delete` flag is the source of the destructive behavior. The
  `__pycache__` exclusion is the only reason we could recover.

## Proposed changes

1. **Pre-flight diff.** Before the rsync, run `rsync -a --delete --dry-run` and parse the `deleting ` lines. If any path under `migrations/versions/`, `packtrack/`, `scripts/`, or any non-allowlisted directory would be deleted, abort and print the list. Require `DEPLOY_ACCEPT_DELETIONS=1` to override.
2. **Pull-then-push.** Before any rsync, pull the LXC's current `migrations/versions/` (and any other "owned-by-LXC" directories) back to a `/tmp/lxc-recovered/` path locally so they can be diffed and committed.
3. **Runbook entry.** Add `docs/RUNBOOKS/deploy.md` (currently absent) covering:
   - Never edit files directly on the LXC for any module the deploy script `--delete`s.
   - If you must hot-fix on the LXC, run `bash deploy/pull-from-lxc.sh` and commit the result before the next deploy.
   - The list of directories the deploy script will wipe.

## Acceptance

- A simulated test: create a file `migrations/versions/__test_local.py` only on the LXC, run `bash deploy/deploy.sh`. Without `DEPLOY_ACCEPT_DELETIONS=1`, the deploy aborts and lists the file. With the env var, it deletes and warns.
- Runbook merged.
- Existing `feature/use-zoho-service-receives` already includes reconstructed migrations; this issue is preventative only.
