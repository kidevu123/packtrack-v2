# Deploy runbook

> ⚠️ **`deploy/deploy.sh` is the ONLY approved deploy path.** Every
> other path is "out-of-band" and *must* finish with the same CSS build
> + smoke verification described below. The September/v2.2.0 unstyled-
> UI incident was caused by an ad-hoc `pct push` + `rsync --delete`
> deploy that skipped the CSS build step — never repeat that pattern
> without the verification recipe in § "Out-of-band deploys" below.

## Canonical path: `bash deploy/deploy.sh`

The official path is `bash deploy/deploy.sh` from the workstation. It:

1. rsyncs source to `${LXC_HOST}:/opt/packtrack/app` (`--delete` semantics — see B below).
2. Ensures `/opt/packtrack/bin/tailwindcss` (v4 CLI) is on the LXC.
3. Builds CSS: `tailwindcss -i static/styles.src.css -o static/styles.css --minify`.
4. **Verifies the build:** refuses to continue if `static/styles.css` is missing, is under 5 KB, or lacks any of the sentinel utilities (`.bg-stone-900`, `.grid`, `.max-w-md`).
5. Runs alembic migrations.
6. Restarts `packtrack.service` and `caddy.service`.
7. Runs `scripts/smoke_test.sh --base http://127.0.0.1` — fails the deploy if `/healthz` or `/static/styles.css` regress.

## Out-of-band deploys (must run the smoke test manually)

Any deploy that bypasses `deploy/deploy.sh` (for example: hot-fix `pct push` from the Proxmox host, manual `rsync`) **must** end with:

```
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  set -e
  cd /opt/packtrack/app
  sudo -u packtrack /opt/packtrack/bin/tailwindcss \
    -i static/styles.src.css -o static/styles.css --minify
  bash scripts/smoke_test.sh --base http://127.0.0.1
"'
# then from the workstation:
bash scripts/smoke_test.sh --base https://packtrack.booute.duckdns.org
```

This is the lesson from the v2.2.0 incident: a `pct push` + `rsync --delete` deploy wiped `static/styles.css` from the LXC and the silent `|| true` rebuild hid the failure. The site rendered un-styled until somebody noticed.

## Why `static/styles.css` is gitignored

It is a build artifact — generated from `static/styles.src.css` on every deploy. Committing it would:

* generate noisy diffs on every UI change
* let two engineers ship inconsistent CSS for the same template source
* mask Tailwind build failures (an old CSS would still ship)

See [follow-up B](followups/B-deploy-migration-erasure-guard.md) for the broader "stop the deploy script silently destroying LXC-only files" issue.

## Manual rebuild (incident recovery)

If the site renders un-styled in prod and you cannot kick a full deploy:

```
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  cd /opt/packtrack/app
  sudo -u packtrack /opt/packtrack/bin/tailwindcss \
    -i static/styles.src.css -o static/styles.css --minify
  ls -la static/styles.css
"'
# then verify externally:
bash scripts/smoke_test.sh --base https://packtrack.booute.duckdns.org
```

No service restart needed — FastAPI's `StaticFiles` re-reads the file on each request.
