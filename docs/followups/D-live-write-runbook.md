# Follow-up D — Live-write window runbook for Pack Track receives

**Why now:** During the controlled live proof of `pack_track.receive.commit`,
the global `ENABLE_LIVE_INVENTORY_WRITES=true` flag was flipped on the
service for the proof but **left on for ~15 hours** before being flipped
back. Damage was limited because v1.27.0 added a per-app allowlist
(`LIVE_INVENTORY_WRITE_ALLOWED_APPS=pack_track`) so Luma and any legacy
caller still saw 403 — but the global flag should never have been
left open. This runbook codifies the procedure so the next live window
closes itself.

## The two-key model (today, post v1.27.x)

| Key | Purpose | Lives on |
|---|---|---|
| `ENABLE_LIVE_INVENTORY_WRITES=true` | Global enable — necessary but not sufficient. | zoho-integration-service `.env` |
| `LIVE_INVENTORY_WRITE_ALLOWED_APPS=<csv>` | Per-app allow-list — sufficient only with the global on. | zoho-integration-service `.env` |
| App credential capability (`pack_track.receive.commit`) | Per-action grant. | DB (`app_capability_permissions`) |

A live write requires **all three**. Removing any one disables writes
for that path. The allow-list is the safest knob (no broad-blast
window).

## The procedure

```
# 1.  Pre-flight (read-only)
ssh root@192.168.1.190
pct exec 9503 -- bash -lc '
  set -a; . /opt/zoho-integration-service/.env; set +a
  echo "GLOBAL=$ENABLE_LIVE_INVENTORY_WRITES"
  echo "ALLOWLIST=$LIVE_INVENTORY_WRITE_ALLOWED_APPS"
  curl -sS http://127.0.0.1:8000/health | jq .version,.db_connected
'
# Confirm: GLOBAL=false (start state), ALLOWLIST contains *only* pack_track,
# version is the expected build, db_connected=true.

# 2.  Open the window — keep it as short as humanly possible.
pct exec 9503 -- bash -lc '
  sed -i "s/^ENABLE_LIVE_INVENTORY_WRITES=.*/ENABLE_LIVE_INVENTORY_WRITES=true/" /opt/zoho-integration-service/.env
  systemctl restart zoho-integration.service
  sleep 2
  curl -sS http://127.0.0.1:8000/health | jq .version
'

# 3.  Run the one controlled action (operator-driven, in Pack Track UI
#     OR via the receive endpoint). Capture the response and the Zoho
#     purchase_receive_id. Replay the same Idempotency-Key once to prove
#     it is a 200 + no second receive.

# 4.  CLOSE THE WINDOW IMMEDIATELY.
pct exec 9503 -- bash -lc '
  sed -i "s/^ENABLE_LIVE_INVENTORY_WRITES=.*/ENABLE_LIVE_INVENTORY_WRITES=false/" /opt/zoho-integration-service/.env
  systemctl restart zoho-integration.service
  sleep 2
  curl -sS http://127.0.0.1:8000/health | jq .version
'

# 5.  Verify with one negative-control commit — expect 403 LIVE_WRITE_DISABLED.

# 6.  Log to ops journal: who opened, who closed, why, Zoho receive id,
#     time-window in minutes. Include the negative-control 403 response.
```

## Safeguards we still need

1. **Auto-close timer.** A systemd timer on the service that, every 15 minutes, inspects `.env`. If `ENABLE_LIVE_INVENTORY_WRITES=true` for longer than N minutes (suggest 30), reset it to false and page ops. The first live window stayed open ~15 hours; this is the structural fix.
2. **Audit alert.** Wire the service's existing audit log to push a notification (Slack/Telegram) when `ENABLE_LIVE_INVENTORY_WRITES` flips — both directions.
3. **Pre-window pairing.** Two-person rule for opening the window: one operator runs the command, a second verifies the close.

## Why the allowlist saved us

Even with the global flag stuck open for ~15 hours, Luma's
`luma.production_output.commit` capability was NOT in
`LIVE_INVENTORY_WRITE_ALLOWED_APPS`, so any incoming Luma write was
rejected at the service. Same for the legacy `trade_show_app`. The
blast radius was bounded to `pack_track.receive.commit`, which had no
queued legitimate calls during that window — net effect: zero
accidental writes. The allowlist is what made this a near-miss instead
of an incident. **Do not** rely on the global flag alone for any
future live window.
