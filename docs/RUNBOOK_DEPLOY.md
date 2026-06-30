# Deploy runbook

> ⚠️ **`deploy/deploy.sh` is the ONLY approved deploy path.** Every
> other path is "out-of-band" and *must* finish with the same CSS build
> + smoke verification described below. The September/v2.2.0 unstyled-
> UI incident was caused by an ad-hoc `pct push` + `rsync --delete`
> deploy that skipped the CSS build step — never repeat that pattern
> without the verification recipe in § "Out-of-band deploys" below.

## Safe deploy sequence (v2.16.1+)

The deploy script ships with a **repo-state guard** that refuses
deploys from a non-`main` branch, with a dirty working tree, or when
local `main` differs from `origin/main`. This is a permanent defense
against the v2.7.4 and v2.16.0 incidents where the deploy was
accidentally run from a worktree on a feature branch.

```bash
cd /Users/sahilkhatri/Projects/Work/packtrack-v2
git checkout main
git pull --ff-only origin main
PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh
```

The guard prints a one-screen banner before sending the bundle:

```
  ------------------------------------------------------------
  PackTrack deploy
  branch:  main
  sha:     <12-char SHA>
  version: <pyproject version>
  alembic: <single head>
  target:  PVE_HOST=192.168.1.190  LXC_ID=200
  ------------------------------------------------------------
```

### Override (testing / recovery only — NOT routine prod)

```bash
ALLOW_NON_MAIN_DEPLOY=1 PVE_HOST=192.168.1.190 LXC_ID=200 bash deploy/deploy.sh
```

- Skips the branch + freshness checks
- **Still refuses a dirty working tree** (untracked, staged, or unstaged changes)
- Prints a loud warning + the branch / SHA it's about to ship

Operators should never use the override for a normal production
release. Use cases: rolling back to a tag via `git checkout v2.X.Y`
then deploying; bringing up a fresh LXC from a feature branch during
infra work.

### Why the guard exists

Two prior incidents:
- **v2.7.4** — `gh pr create` heredoc with backticks accidentally
  invoked `bash deploy/deploy.sh` from the feature-branch worktree.
- **v2.16.0** — operator ran `deploy.sh` from a worktree that was on
  the feature branch (post-merge, before checking out `main`).

In both cases the deployed code was *correct* but the audit trail
said "deployed from feature branch", which is not the same as
"deployed from main at origin/main".

### Branch / worktree hygiene

After every release the operator should:

1. Make sure the merged feature branch is deleted on origin (the PR's
   `--delete-branch` flag handles this; `gh pr merge --delete-branch`).
2. Delete the local feature branch (`git branch -D <name>`).
3. Remove the local worktree (`git worktree remove ../<dirname>` then
   `git worktree prune`).

The main repo checkout at `/Users/sahilkhatri/Projects/Work/packtrack-v2`
should always live on `main` between sessions.

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
