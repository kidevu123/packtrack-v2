# Route Pack Track Receives Through zoho-integration-service

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. Steps use `- [ ]` checkboxes.

**Goal:** Replace Pack Track's direct/legacy-gateway Zoho purchase-receive write path with the new zoho-integration-service `/zoho/pack_track/receive/preview` + `/commit` endpoints.

**Architecture:** New client `packtrack/services/zoho_integration.py` owns HTTP + typed errors. The receive route (`POST /receive/{zoho_po_id}`) saves a `BoxReceipt` per line as today, then submits one `commit_receive` call per line keyed by `BoxReceipt.packtrack_receipt_id` (Idempotency-Key). LIVE_WRITE_DISABLED is treated as "blocked" — receipt remains in Pack Track + Luma; Zoho mirror sync will eventually reconcile once writes are enabled. The legacy `create_zoho_receive` / `/zoho/purchase_receives/create` path is deleted.

**Tech Stack:** Python 3.13, FastAPI, httpx, SQLModel, pytest.

## Global Constraints

- Bearer auth with `X-Brand: <brand>`. **Never** send legacy `X-Internal-Token` against the new endpoints.
- `Idempotency-Key` header MUST be sent and stable per Pack Track receipt — use `BoxReceipt.packtrack_receipt_id`.
- Pack Track must not call `zohoapis.com` or `accounts.zoho.com` for receive writes — verified by tests.
- No hardcoded LAN IP, public host, brand, org id, bearer token, warehouse id, item id, or PO id in code.
- Version bump to `2.2.0` (minor — new integration capability).
- Tests under `tests/`; ruff target `py312`; line-length 100.

---

### Task 1: Branch + config

**Files:**
- Modify: `packtrack/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `settings.ZOHO_INTEGRATION_BASE_URL`, `ZOHO_INTEGRATION_APP_TOKEN`, `ZOHO_INTEGRATION_BRAND`, `ZOHO_INTEGRATION_TIMEOUT_SECONDS: float = 30.0`, `ZOHO_INTEGRATION_RECEIVE_ENABLED: bool = True`, property `settings.zoho_integration_configured`.

- [ ] **Branch:** `git checkout -b feature/use-zoho-service-receives`
- [ ] **Config:** add the five settings above; `zoho_integration_configured` is True iff base_url + token + brand are all truthy.
- [ ] **.env.example:** add a section with all five, clearly marked "do not hardcode".

### Task 2: Integration client module

**Files:**
- Create: `packtrack/services/zoho_integration.py`
- Test: `tests/test_zoho_integration.py`

**Interfaces:**
- Produces:
  ```python
  class ZohoIntegrationError(Exception): ...
  class ZohoIntegrationNotConfigured(ZohoIntegrationError): ...
  class ZohoIntegrationLiveWriteDisabled(ZohoIntegrationError): ...
  class ZohoIntegrationValidationError(ZohoIntegrationError):  # 400/422
      code: str  # e.g. "ITEM_PO_MISMATCH"
      detail: str
  class ZohoIntegrationIdempotencyConflict(ZohoIntegrationError): ...
  class ZohoIntegrationAuthError(ZohoIntegrationError): ...
  class ZohoIntegrationConfigError(ZohoIntegrationError):     # 404 brand/org/product
      code: str
  class ZohoIntegrationRateLimited(ZohoIntegrationError): ...
  class ZohoIntegrationGatewayError(ZohoIntegrationError): ...

  @dataclass(frozen=True)
  class ReceivePayload:
      pack_track_receipt_id: str
      purchaseorder_id: str
      purchaseorder_line_item_id: str
      item_id: str
      received_quantity: float
      received_date: str  # ISO YYYY-MM-DD
      warehouse_id: str | None = None
      notes: str | None = None
      pack_track_operator_id: str | None = None
      pack_track_workflow_session_id: str | None = None

  def preview_receive(payload: ReceivePayload, *, client: httpx.Client | None = None) -> dict
  def commit_receive(payload: ReceivePayload, *, client: httpx.Client | None = None) -> dict
  ```
- Internals: shared `_request(path, payload, *, client)` builds headers (`Authorization: Bearer …`, `X-Brand`, `Idempotency-Key: PACK_TRACK_RECEIVE_<pack_track_receipt_id>`, `Content-Type: application/json`). Maps status+code to typed exceptions. 5xx and network errors → `ZohoIntegrationGatewayError`.

### Task 3: Capture `line_item_id` in mirror

**Files:**
- Modify: `packtrack/zoho.py` (`sync_open_pos`, ~line 346)

**Interfaces:** none new.

- [ ] In the `line_items_payload` dict literal, add `"line_item_id": str(li.get("line_item_id") or "")`. The receiving template already reads this via `li.get("line_item_id")` → `zoho_line_item_id[]` hidden input.

### Task 4: Receive-service orchestration helper

**Files:**
- Modify: `packtrack/services/receiving.py`
- Delete: `create_zoho_receive` (old gateway path)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class LineSubmission:
      box_receipt_id: int          # PK
      packtrack_receipt_id: str
      zoho_item_id: str
      zoho_line_item_id: str
      quantity: float
      unit: str | None

  @dataclass(frozen=True)
  class LineResult:
      ok: bool
      status: Literal["committed", "blocked", "validation_failed", "idempotency_conflict",
                      "gateway_error", "skipped", "not_configured"]
      message: str | None

  def submit_zoho_receives(
      mirror: ZohoMirror,
      submissions: list[LineSubmission],
      *, user: User, session_id: str, notes: str | None,
  ) -> list[LineResult]
  ```
- One commit per submission. `not_configured` short-circuits before HTTP. Caller logs POEvents.

### Task 5: Route + template

**Files:**
- Modify: `packtrack/routes/receiving.py` (`submit_receiving`, ~line 322-346)
- Modify: `packtrack/templates/receiving_result.html` (zoho status block, ~line 81-103)

**Interfaces:**
- `submit_receiving` collects `LineSubmission`s (built right after creating each `BoxReceipt`), calls `submit_zoho_receives`, attaches per-line results to `results[]` (`r["zoho_status"]`, `r["zoho_msg"]`), writes one `POEvent(kind="zoho_receive")` per blocked/failed line, returns aggregate `zoho_committed_count`, `zoho_blocked_count`, `zoho_failed_count` to the template.
- Template: replace the binary "Zoho purchase receive created/failed" block with a grouped summary showing counts; copy explains blocked = will reconcile when live writes enabled.

### Task 6: Tests

**Files:**
- Create: `tests/test_zoho_integration.py`

**Coverage (each its own test):**
1. `commit_receive` raises `ZohoIntegrationNotConfigured` when settings empty.
2. Preview success returns parsed JSON, sends correct headers (Bearer + X-Brand + Idempotency-Key) and body.
3. Preview validation failure (`422 INSUFFICIENT_PO_REMAINING`) raises `ZohoIntegrationValidationError` with `.code`.
4. Commit success returns parsed JSON.
5. Commit `403 LIVE_WRITE_DISABLED` raises `ZohoIntegrationLiveWriteDisabled`.
6. Commit `502/503` raises `ZohoIntegrationGatewayError`.
7. Same payload retried with same Idempotency-Key returns 200 twice (mock responds idempotently); ensures Pack Track does not crash on retry.
8. `409` raises `ZohoIntegrationIdempotencyConflict`.
9. **Direct-Zoho guard:** the MockTransport asserts no request goes to `*zohoapis.com` or `*accounts.zoho.com`.

Use `httpx.MockTransport` + `httpx.Client(transport=mock_transport)` injected via the optional `client` kwarg — keeps tests synchronous, no monkeypatching, no real env.

### Task 7: Docs + version bump

**Files:**
- Create: `docs/PACKTRACK_ZOHO_INTEGRATION_RECEIVES.md`
- Modify: `README.md` (one-line link)
- Modify: `pyproject.toml` (version 2.1.2 → 2.2.0)

Docs cover: env vars, preview vs commit, failure states (table), deployment notes, and a safe smoke-test recipe (`curl` examples with `ENABLE_LIVE_INVENTORY_WRITES=false`).

### Task 8: Verify

- [ ] `ruff check packtrack tests`
- [ ] `pytest -q`
- [ ] Commit + push + open PR.

### Task 9: Deploy

- [ ] Append `ZOHO_INTEGRATION_BASE_URL=http://192.168.1.205:8000`, `ZOHO_INTEGRATION_APP_TOKEN=<from ops>`, `ZOHO_INTEGRATION_BRAND=haute_brands` to `/etc/packtrack/packtrack.env` on `192.168.1.206` (LXC 200) — keep config out of code.
- [ ] `bash deploy/deploy.sh` from the workstation.
- [ ] `systemctl status packtrack`, `curl /healthz`.
- [ ] Manual preview against an open PO — expect 200 with preview payload echoed.
- [ ] Manual commit against the same PO — expect `403 LIVE_WRITE_DISABLED`, no Zoho writes.
- [ ] `journalctl -u packtrack` — confirm only `192.168.1.205:8000` is contacted; no `zohoapis.com` / `accounts.zoho.com` traffic from the receive write path.
