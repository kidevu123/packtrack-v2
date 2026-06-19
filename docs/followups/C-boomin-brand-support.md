# Follow-up C — Multi-brand support (add Boomin) for Pack Track receives

**Why now:** v2.2.0 wired Pack Track to zoho-integration-service for
receives but only ever sends `X-Brand: haute_brands` (set once on the
LXC via `ZOHO_INTEGRATION_BRAND`). Boomin will eventually flow through
the same Pack Track install — we should not hardcode brand routing.

## Guard rails already in place

- The brand is **already config-driven** — `packtrack/services/zoho_integration.py::_headers` reads `settings.ZOHO_INTEGRATION_BRAND`. No grep hit for `"haute_brands"` in production code.
- The integration-service registration only granted Pack Track the `haute_brands` brand permission (`app_brand_permissions`). No silent cross-brand writes are possible.

## Work required

1. **Decide the dispatch model.**
   - Option A: One Pack Track LXC per brand (matches the existing Haute-only LXC 200; add LXC 201 for Boomin). Pro: hard isolation. Con: duplicated UI, separate DB.
   - Option B: Single Pack Track instance, brand picked per PO/per item via a column. Pro: shared inbox. Con: new column on `purchase_orders`, RLS-ish guard, every receive call needs the right `X-Brand`.

   Recommend Option A for the first Boomin cutover (mirrors the LXC isolation pattern already used). Revisit once we have two brands live.

2. **Provision a `pack_track` (or `pack_track_boomin`) app on zoho-integration-service** with brand permission `boomin_brands` and the same two capabilities. Issue a new credential — do not reuse Haute's.

3. **Document the brand source.** Add to `docs/PACKTRACK_ZOHO_INTEGRATION_RECEIVES.md`:
   - `ZOHO_INTEGRATION_BRAND` is the per-LXC default.
   - If/when we move to Option B, the per-PO brand selector overrides at call time — Pack Track must pass `X-Brand` per request, not per process.

## Acceptance

- Boomin LXC stands up with its own env file, secret, and `ZOHO_INTEGRATION_BRAND=boomin_brands`.
- The same Pack Track tree on Boomin LXC reaches zoho-integration-service and sees `LIVE_INVENTORY_WRITE_ALLOWED_APPS` listing **only** Pack Track (no Boomin-specific app needed unless the team wants per-brand allowlisting).
- A receive submitted on the Haute LXC never reaches Boomin's Zoho org, and vice versa — verified by the service's audit log.

## Non-goals

- No multi-tenancy in the database for v2 unless Option B is chosen.
- No Boomin credential provisioning in this issue (depends on Option A vs B).
