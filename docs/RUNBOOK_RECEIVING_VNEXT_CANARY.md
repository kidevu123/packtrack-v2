# Runbook — Receiving vNext Stage 2 canary

**Scope:** Controlled, single-receive validation of the Stage 2
`/receive/v2/...` flow in production, with `RECEIVING_VNEXT_ENABLED=true`
flipped on for the duration of the test.

**Pre-conditions:**
* Production is on commit `7ce0af6` (or later), version `>=2.6.0`.
* Alembic head = `e1f2a3b4c5d7`.
* `RECEIVING_VNEXT_ENABLED` is currently unset (default OFF).
* No `v2.6.0` or `v2.6.1` tag exists yet — tagging waits on canary success.

**Out of scope for the canary:** vendor packing-list upload, per-line
photo upload, multi-PO receive, replacing the legacy
`/receive/{zoho_po_id}` flow.

---

## 1. Enable the flag

Edit the prod env file on LXC 200 (Proxmox host `192.168.1.190`):

```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  set -e
  cp /etc/packtrack/packtrack.env /etc/packtrack/packtrack.env.bak.\$(date +%Y%m%d-%H%M%S)
  echo RECEIVING_VNEXT_ENABLED=true >> /etc/packtrack/packtrack.env
  chmod 640 /etc/packtrack/packtrack.env
  chown root:packtrack /etc/packtrack/packtrack.env
  grep RECEIVING_VNEXT_ENABLED /etc/packtrack/packtrack.env
"'
```

A backup is made in case rollback is needed.

## 2. Reload PackTrack

```bash
ssh root@192.168.1.190 'pct exec 200 -- systemctl restart packtrack.service'
ssh root@192.168.1.190 'pct exec 200 -- systemctl status packtrack.service --no-pager | head -10'
```

Expected: `active (running)`, no errors in the first ~3 lines of the
journal tail.

```bash
ssh root@192.168.1.190 'pct exec 200 -- journalctl -u packtrack.service --no-pager --since "30 seconds ago" | tail -20'
```

## 3. Confirm vNext is reachable (auth required)

From a browser logged in as an `OWNER` or `RECEIVING` operator, visit:

```
https://packtrack.booute.duckdns.org/receive/v2/new?po_id=<safe-test-po-id>
```

Expected: **200**, the "Start a receive" confirmation page (NOT a 404).

If you get 404: the flag is not being read. Recheck step 1, restart, then
re-try. Do not proceed.

## 4. Choose a safe test PO

Pick a PO that meets **all** of these:

1. **Small** — 1 PO line, 1 item, low quantity (e.g. `qty=10`). Easy to
   reverse in Zoho if needed.
2. **Has a Zoho mirror** — query in PackTrack: open `/po/<id>` and confirm
   the page shows Zoho line items. If not, run `Settings → Sync now` first.
3. **Item has a `material_code`** — open `/inventory/<item_id>` and
   confirm. If missing, fill it in first (otherwise Luma will park the
   leaf as `NOT_READY` and the receive will be `PUSH_FAILED`, which is
   technically a valid canary signal but noisier than needed).
4. **Low operational risk** — not the wholesale-channel mainstay item
   for the day; not something where a duplicated receive would cause
   accounting cleanup.

Suggested: a small packaging-item PO with ≤2 lines that already had a
clean legacy receive earlier in the same week.

## 5. Run the canary receive

1. Visit `https://packtrack.booute.duckdns.org/receive/v2/new?po_id=<id>`.
   Confirm the start page.
2. Click **Start receive**. URL becomes `/receive/v2/<receive_id>`.
3. **Add one case** with vendor case number (e.g. `"CANARY-001"`).
4. **Add one line** to that case: pick the PO item from the `<select>`,
   set `declared_quantity` (e.g. `10`), optionally set `counted_quantity`.
5. Visit `/receive/v2/<receive_id>/review`.
   * Confirm no blockers.
   * Note any warnings (over/under count vs PO remaining, etc.).
6. **Click Finalize & push** (check `confirm_warnings` if shown).

## 6. Verify

### 6a. BoxReceipts created

```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  set -a; source /etc/packtrack/packtrack.env; set +a
  psql -h 127.0.0.1 -U packtrack -d packtrack -c \"
    SELECT id, packtrack_receipt_id, box_number, submission_id, submission_line_index,
           material_code, declared_quantity, counted_quantity, luma_push_status
    FROM box_receipts WHERE receive_id = <receive_id>
    ORDER BY submission_line_index;
  \"
"'
```

Expected:
* Exactly **one row per `ReceiveCaseLine`** (one row for a one-line canary).
* `box_number` like `PT-<hex>` (= `PT-<packtrack_receipt_id>`).
* `submission_id` populated, matches `Receive.submission_id`.
* `submission_line_index` = `1` for the first leaf.
* `luma_push_status` ∈ `{pushed, duplicate, dry_run_ok, failed, not_ready}`.

### 6b. Receive status

```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  set -a; source /etc/packtrack/packtrack.env; set +a
  psql -h 127.0.0.1 -U packtrack -d packtrack -c \"
    SELECT id, receive_number, status, finalized_at, pushed_at
    FROM receives WHERE id = <receive_id>;
  \"
"'
```

Expected:
* `status = 'pushed_ok'` → ✅ full success.
* `status = 'push_failed'` → look at the result page + POEvents to see
  which integration failed. If Luma `NOT_READY` (missing material_code)
  or Zoho `missing_mirror`, fix the underlying cause and use the
  retry-push button.

### 6c. Result page

`/receive/v2/<receive_id>` after finalize redirects to the result page,
which should show:
* Per-leaf Luma status (e.g. `pushed`) and any error.
* Per-leaf Zoho status (e.g. `committed`, `blocked`, or `missing_mirror`).
* If `PUSH_FAILED`: a **Retry failed pushes** button.

### 6d. POEvents

```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  set -a; source /etc/packtrack/packtrack.env; set +a
  psql -h 127.0.0.1 -U packtrack -d packtrack -c \"
    SELECT id, po_id, kind, message, created_at, actor_id
    FROM po_events
    WHERE message ILIKE '%<receive-number>%'
    ORDER BY id DESC LIMIT 10;
  \"
"'
```

Expected sequence:
* `receive_started` (when the receive was created).
* `case_added` (if emitted by the route — currently not, but Stage 1
  bumps `updated_at`).
* `receive_finalized` with the materialized box-receipt count.
* Then **either** `receive_pushed_ok` **or** `receive_push_failed`
  with a `luma_failed=N, zoho_failed=[...]` reason.

### 6e. Service journal

```bash
ssh root@192.168.1.190 'pct exec 200 -- journalctl -u packtrack.service --no-pager --since "5 minutes ago" | grep -iE "error|traceback|exception|luma|zoho" | tail -30'
```

Expected: at most informational `httpx` request lines from the Luma
push and the Zoho integration call. No `Traceback`, no
`OperationalError`, no `Internal Server Error`.

### 6f. Zoho confirmation (out-of-band)

Open the same PO in Zoho Inventory and confirm a new purchase-receive
appeared with the expected quantity. If the operator pushed
`live_writes_disabled=blocked`, Zoho will not show it — that's expected,
and PackTrack's per-leaf `blocked` status surfaces it on the result page.

## 7. Rollback if anything looks wrong

If the result is `PUSH_FAILED` with a real failure, or any verification
step in §6 looks broken, roll the flag back **immediately**:

```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -lc "
  set -e
  sed -i.bak.rollback /RECEIVING_VNEXT_ENABLED/d /etc/packtrack/packtrack.env
  grep RECEIVING_VNEXT_ENABLED /etc/packtrack/packtrack.env || echo flag-removed-OK
"'
ssh root@192.168.1.190 'pct exec 200 -- systemctl restart packtrack.service'
```

Then:
1. The canary receive's `BoxReceipt` rows are still in the DB; they're
   already pushed where push succeeded. **Do not delete them by hand.**
   Use the retry-push button after fixing the underlying issue, OR
   leave the receive in `PUSH_FAILED` and fix the next deploy.
2. Use the legacy `/receive/{zoho_po_id}` flow for any further real
   receives that day.
3. Report the failure mode (POEvent message + journal excerpt) and
   open a bugfix branch off `main`. **Do NOT tag `v2.6.1`** until
   the canary actually succeeds end-to-end.

## 8. Tagging rule

* **If canary succeeds end-to-end** (`Receive.status = pushed_ok`,
  Zoho shows the receive, no journal errors):
  ```bash
  git tag -a v2.6.1 7ce0af6 -m "PackTrack v2.6.1 — Receiving vNext Stage 2 + richer item detail (canary-verified)"
  git push origin v2.6.1
  ```
  `v2.6.0` is intentionally **skipped** per decision (2026-06-26).
* **If canary fails**: fix-forward on a new branch off `main`, ship
  another deploy, re-run the canary. Tag only after a clean canary.

## 9. Re-enable the flag for ongoing operator use (after canary)

The canary leaves the flag ON for the duration of the test. After
canary success, decide:

* **Keep flag ON** → vNext available to operators alongside legacy.
  Both flows coexist.
* **Flip flag OFF again** → vNext disabled until you're ready to make
  it the default. To flip off, follow the §7 rollback recipe (it's
  the same operation).

There is no harm in leaving the flag ON after a successful canary —
the legacy flow is unchanged either way.
