# Follow-up A — Move remaining direct-Zoho writes behind zoho-integration-service

**Why now:** Pack Track v2.2.0 routed *receive* writes through
zoho-integration-service. Two direct-OAuth write paths remain in
`packtrack/zoho.py` — they still hold Zoho creds in the LXC env and call
`https://www.zohoapis.com/inventory/v1/...` from Pack Track. Until they
move, the `legacy_zoho_configured` field on `/healthz` cannot go away,
and the Pack Track LXC still needs the four `ZOHO_CLIENT_ID/SECRET/
REFRESH_TOKEN/ORG_ID` secrets.

## Scope

| Function | Today | Target |
|---|---|---|
| `packtrack/zoho.py::push_po` | `POST https://www.zohoapis.com/inventory/v1/purchaseorders` with a direct OAuth token | `POST {ZOHO_INTEGRATION_BASE_URL}/zoho/pack_track/purchase_orders/create` (or the brand-aware equivalent the service exposes) with bearer + `X-Brand` |
| `packtrack/zoho.py::adjust_stock` | `POST https://www.zohoapis.com/inventory/v1/inventoryadjustments` | Same shape as `receive/commit`: `/zoho/pack_track/inventory_adjustment/{preview,commit}` |

Caller sites:

- `push_po`: `packtrack/scheduler.py::_push_retry_job` (APScheduler every 5 min) and PO create path.
- `adjust_stock`: not currently auto-triggered from receives in v2 (receives go through `submit_zoho_receives` already); legacy reference paths should be audited.

## Constraints

- **Do not** introduce a second app credential — reuse `pack_track`'s bearer token on zoho-integration-service. Add the new capabilities (`pack_track.po.create`, `pack_track.adjustment.preview`, `pack_track.adjustment.commit`) to the existing app.
- **Do not** flip `ENABLE_LIVE_INVENTORY_WRITES` to enable POs/adjustments without the same per-app allowlist gate that `pack_track.receive.commit` uses today.
- Preview/commit pattern is mandatory — POs and adjustments must have a dry-run path.
- Pack Track stops shipping `ZOHO_CLIENT_ID/SECRET/REFRESH_TOKEN/ORG_ID` once the migration ships. The four env vars are then deleted from `/etc/packtrack/packtrack.env` and the `legacy_zoho_configured` health field flips `false` permanently.

## Acceptance

1. `grep -rn "zohoapis.com\|accounts.zoho.com" packtrack/` returns no production hits (test mocks OK).
2. `journalctl -u packtrack.service` shows zero direct Zoho hostnames for 7 days post-deploy.
3. `/healthz.legacy_zoho_configured` is `false` AND the legacy env vars are removed from `/etc/packtrack/packtrack.env`.
4. Push-retry + manual-PO-create work against the integration service's dry-run mode.

## Reference

- `docs/ZOHO_API_GATEWAY_PLAN.md` (the original P8 plan — predates the v1.26+ Pack Track receive endpoints; treat as background, not a literal contract).
- `docs/PACKTRACK_ZOHO_INTEGRATION_RECEIVES.md` (the pattern the receive migration set; copy it).
