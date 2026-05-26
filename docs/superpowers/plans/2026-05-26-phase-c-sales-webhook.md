# Phase C: Sales Feedback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Zoho confirms a sale, PackTrack writes a SalesEvent row and Luma links the sale to the most recent RELEASED lot for that product — closing the genealogy loop. Luma also gains a new internal BOM API endpoint used by the Phase D forecasting dashboard.

**Architecture:** Two independent webhook endpoints (PackTrack + Luma), both auth'd with ZOHO_WEBHOOK_SECRET, both idempotent on zoho_order_id. A new Luma read-only BOM endpoint (GET /api/internal/product-packaging-specs) is added in the same task — it's three lines of Drizzle but Phase D depends on it.

**Pre-condition:** Phase B must be live so Zoho has accurate finished-goods inventory (otherwise the sales data feeds a stale Zoho).

**Tech Stack:** FastAPI · SQLModel · Alembic · Next.js 15 · TypeScript · Drizzle

---

## File Map

| Action | Path | Responsibility |
|---|---|---|
| Modify | `packtrack/models.py` | Add `SalesEvent` model |
| Create | `migrations/versions/e3f4a5b6c7d8_sales_events.py` | Alembic migration for `sales_events` table |
| Create | `packtrack/routes/webhooks.py` | `POST /api/webhooks/zoho-sales` |
| Modify | `packtrack/main.py` | Import and mount `webhooks.router` |
| Modify | `luma/lib/db/schema.ts` | Add `finishedLotSales` table |
| Create | `luma/drizzle/0048_finished_lot_sales.sql` | Drizzle migration SQL |
| Create | `luma/app/api/webhooks/zoho-sales/route.ts` | Luma Zoho sales webhook |
| Create | `luma/app/api/internal/product-packaging-specs/route.ts` | BOM read API for Phase D |

All PackTrack paths are relative to `/Users/kidevu/packtrack-v2/`.
All Luma paths are relative to `/Users/kidevu/luma/`.

---

### Task 1: Read the existing patterns

**Files:**
- Read: `packtrack/routes/receiving.py`
- Read: `luma/app/api/integrations/packtrack/items/route.ts`

No files are created in this task. The purpose is to confirm the auth and session patterns before writing any code.

- [ ] **Step 1: Read the PackTrack receiving route**

  ```bash
  cat /Users/kidevu/packtrack-v2/packtrack/routes/receiving.py | head -50
  ```

  Note the following:
  - `session: Session = Depends(get_session)` — sync SQLAlchemy session injected via FastAPI `Depends`
  - `from packtrack.config import settings` — all env config lives here
  - `from packtrack.db import get_session` — session factory
  - Route handlers are plain functions (not `async def`) except for those that call `await request.form()` or `await request.json()`

- [ ] **Step 2: Read the Luma packtrack/items route**

  ```bash
  cat /Users/kidevu/luma/app/api/integrations/packtrack/items/route.ts | head -90
  ```

  Note the following:
  - `export const runtime = "nodejs"; export const dynamic = "force-dynamic";` — required on every route
  - `process.env.SOME_SECRET` — env vars read directly; no config abstraction
  - Auth pattern: `req.headers.get("x-packtrack-secret")` compared to env var; return `401` on mismatch
  - Body parsing: `await req.json()` inside try/catch, then `zod.safeParse`
  - DB: `import { db } from "@/lib/db"` with Drizzle query builder
  - Response: `NextResponse.json({ ok: true, ... }, { status: 200 })`

- [ ] **Step 3: Confirm `ZOHO_WEBHOOK_SECRET` is not yet in config.py**

  ```bash
  grep -n "ZOHO_WEBHOOK_SECRET" /Users/kidevu/packtrack-v2/packtrack/config.py
  ```

  Expected: no output (field does not exist yet — it will be added in Task 3).

- [ ] **Step 4: Confirm the Phase A migration file exists (this migration depends on it)**

  ```bash
  ls /Users/kidevu/packtrack-v2/migrations/versions/ | grep d2e3f4a5b6c7
  ```

  Expected output: `d2e3f4a5b6c7_material_consumption_events.py`

  If missing, the Phase A plan must be executed first before continuing here. Do not proceed until this file exists.

---

### Task 2: SalesEvent model + Alembic migration

**Files:**
- Modify: `packtrack/models.py`
- Create: `migrations/versions/e3f4a5b6c7d8_sales_events.py`

- [ ] **Step 1: Append `SalesEvent` to `packtrack/models.py`**

  Open `/Users/kidevu/packtrack-v2/packtrack/models.py`. At the very end of the file, after the last model class definition, append:

  ```python
  class SalesEvent(SQLModel, table=True):
      __tablename__ = "sales_events"
      __table_args__ = (UniqueConstraint("zoho_order_id", name="uq_sales_event_order"),)
      id: int | None = Field(default=None, primary_key=True)
      zoho_order_id: str = Field(max_length=128, index=True)
      product_sku: str = Field(max_length=128, index=True)
      qty_sold: int
      sold_at: datetime
      received_at: datetime = Field(default_factory=datetime.utcnow)
  ```

  `UniqueConstraint` is already imported at the top of `models.py` via SQLModel. Verify:

  ```bash
  grep "UniqueConstraint" /Users/kidevu/packtrack-v2/packtrack/models.py | head -3
  ```

  Expected: at least one line showing the import.

  If `UniqueConstraint` is not already imported, add it to the SQLModel import line. The current import is:

  ```python
  from sqlmodel import Field, Relationship, SQLModel
  ```

  It must become:

  ```python
  from sqlmodel import Field, Relationship, SQLModel
  from sqlalchemy import UniqueConstraint
  ```

  (`UniqueConstraint` lives in `sqlalchemy`, not `sqlmodel`.)

- [ ] **Step 2: Create the Alembic migration**

  Create `/Users/kidevu/packtrack-v2/migrations/versions/e3f4a5b6c7d8_sales_events.py`:

  ```python
  """sales_events — record Zoho sales confirmations

  Revision ID: e3f4a5b6c7d8
  Revises: d2e3f4a5b6c7
  Create Date: 2026-05-26

  Creates the sales_events table with a unique constraint on zoho_order_id
  so webhook retries are idempotent.
  """
  from alembic import op
  import sqlalchemy as sa

  revision = "e3f4a5b6c7d8"
  down_revision = "d2e3f4a5b6c7"
  branch_labels = None
  depends_on = None


  def upgrade() -> None:
      op.create_table(
          "sales_events",
          sa.Column("id", sa.Integer(), nullable=False),
          sa.Column("zoho_order_id", sa.String(length=128), nullable=False),
          sa.Column("product_sku", sa.String(length=128), nullable=False),
          sa.Column("qty_sold", sa.Integer(), nullable=False),
          sa.Column("sold_at", sa.DateTime(), nullable=False),
          sa.Column("received_at", sa.DateTime(), nullable=False),
          sa.PrimaryKeyConstraint("id"),
          sa.UniqueConstraint("zoho_order_id", name="uq_sales_event_order"),
      )
      op.create_index("ix_sales_events_zoho_order_id", "sales_events", ["zoho_order_id"])
      op.create_index("ix_sales_events_product_sku", "sales_events", ["product_sku"])


  def downgrade() -> None:
      op.drop_index("ix_sales_events_product_sku", table_name="sales_events")
      op.drop_index("ix_sales_events_zoho_order_id", table_name="sales_events")
      op.drop_table("sales_events")
  ```

- [ ] **Step 3: Verify Alembic history is coherent**

  ```bash
  cd /Users/kidevu/packtrack-v2 && python -m alembic history --verbose 2>&1 | tail -20
  ```

  Expected: the chain ends with `e3f4a5b6c7d8` as the latest revision. If Alembic complains about an unknown `down_revision`, the Phase A migration (`d2e3f4a5b6c7`) does not exist — stop and run Phase A first.

- [ ] **Step 4: Commit**

  ```bash
  cd /Users/kidevu/packtrack-v2
  git add packtrack/models.py migrations/versions/e3f4a5b6c7d8_sales_events.py
  git commit -m "feat(phase-c): SalesEvent model + Alembic migration e3f4a5b6c7d8"
  ```

---

### Task 3: PackTrack webhook route + main.py wiring

**Files:**
- Create: `packtrack/routes/webhooks.py`
- Modify: `packtrack/main.py`
- Modify: `packtrack/config.py`

- [ ] **Step 1: Add `ZOHO_WEBHOOK_SECRET` to config**

  Open `/Users/kidevu/packtrack-v2/packtrack/config.py`. After the `TELEGRAM_WEBHOOK_SECRET` line, add:

  ```python
      ZOHO_WEBHOOK_SECRET: str = ""
  ```

  The surrounding context looks like:

  ```python
      TELEGRAM_BOT_TOKEN: str = ""
      TELEGRAM_WEBHOOK_SECRET: str = ""
  ```

  It should become:

  ```python
      TELEGRAM_BOT_TOKEN: str = ""
      TELEGRAM_WEBHOOK_SECRET: str = ""
      ZOHO_WEBHOOK_SECRET: str = ""
  ```

- [ ] **Step 2: Create `packtrack/routes/webhooks.py`**

  Create `/Users/kidevu/packtrack-v2/packtrack/routes/webhooks.py` with the following complete content:

  ```python
  """Inbound webhooks — currently Zoho sales confirmation.

  POST /api/webhooks/zoho-sales
      Zoho fires this when a sales order is confirmed.  We record a
      SalesEvent row for every sale; Phase D's forecasting logic reads
      this table to compute daily_usage_rate per item.

  Authentication: X-Zoho-Webhook-Secret header must equal
      settings.ZOHO_WEBHOOK_SECRET.  Returns 401 on mismatch.
  Idempotency: unique constraint on zoho_order_id.  A duplicate
      delivery from Zoho returns 200 {"ok": True, "skipped": True}.
  """
  from __future__ import annotations

  import logging
  from datetime import datetime

  from fastapi import APIRouter, Depends, Request
  from fastapi.responses import JSONResponse
  from sqlalchemy.exc import IntegrityError
  from sqlmodel import Session

  from packtrack.config import settings
  from packtrack.db import get_session
  from packtrack.models import SalesEvent

  logger = logging.getLogger("packtrack.webhooks")

  router = APIRouter()


  def _unauthorized(reason: str) -> JSONResponse:
      return JSONResponse({"ok": False, "error": reason}, status_code=401)


  @router.post("/api/webhooks/zoho-sales")
  async def zoho_sales_webhook(
      request: Request,
      session: Session = Depends(get_session),
  ) -> JSONResponse:
      """Record a Zoho sales confirmation and return 200."""

      # ── Auth ────────────────────────────────────────────────────────
      expected = settings.ZOHO_WEBHOOK_SECRET
      if not expected:
          # Server is not configured — fail loudly so ops notices.
          logger.error("ZOHO_WEBHOOK_SECRET is not set; rejecting webhook.")
          return JSONResponse(
              {"ok": False, "error": "Webhook secret not configured on server."},
              status_code=503,
          )
      got = request.headers.get("X-Zoho-Webhook-Secret")
      if not got or got != expected:
          logger.warning("zoho_sales_webhook: bad or missing secret header")
          return _unauthorized("Missing or invalid X-Zoho-Webhook-Secret.")

      # ── Parse body ───────────────────────────────────────────────────
      try:
          raw = await request.json()
      except Exception:
          return JSONResponse({"ok": False, "error": "Body must be JSON."}, status_code=400)

      zoho_order_id: str | None = raw.get("zoho_order_id")
      product_sku: str | None = raw.get("product_sku")
      qty_sold_raw = raw.get("qty_sold")
      sold_at_raw: str | None = raw.get("sold_at")

      if not zoho_order_id or not product_sku or qty_sold_raw is None or not sold_at_raw:
          return JSONResponse(
              {"ok": False, "error": "Missing required fields: zoho_order_id, product_sku, qty_sold, sold_at."},
              status_code=400,
          )

      try:
          qty_sold = int(qty_sold_raw)
      except (TypeError, ValueError):
          return JSONResponse({"ok": False, "error": "qty_sold must be an integer."}, status_code=400)

      try:
          sold_at = datetime.fromisoformat(sold_at_raw.replace("Z", "+00:00"))
      except (ValueError, AttributeError):
          return JSONResponse({"ok": False, "error": "sold_at must be an ISO 8601 datetime string."}, status_code=400)

      # ── Idempotent insert ────────────────────────────────────────────
      event = SalesEvent(
          zoho_order_id=zoho_order_id,
          product_sku=product_sku,
          qty_sold=qty_sold,
          sold_at=sold_at,
      )
      try:
          session.add(event)
          session.commit()
      except IntegrityError:
          session.rollback()
          logger.info("zoho_sales_webhook: duplicate order %s — skipped", zoho_order_id)
          return JSONResponse({"ok": True, "skipped": True}, status_code=200)

      logger.info(
          "zoho_sales_webhook: recorded order=%s sku=%s qty=%d",
          zoho_order_id, product_sku, qty_sold,
      )
      return JSONResponse({"ok": True}, status_code=200)
  ```

- [ ] **Step 3: Mount the router in `packtrack/main.py`**

  In `/Users/kidevu/packtrack-v2/packtrack/main.py`, update the import line from:

  ```python
  from packtrack.routes import admin, auth, inbox, inventory, purchase_orders, receiving, search, telegram_webhook
  ```

  to:

  ```python
  from packtrack.routes import admin, auth, inbox, inventory, purchase_orders, receiving, search, telegram_webhook, webhooks
  ```

  Then after:

  ```python
  app.include_router(telegram_webhook.router)
  ```

  add:

  ```python
  app.include_router(webhooks.router)
  ```

- [ ] **Step 4: Commit**

  ```bash
  cd /Users/kidevu/packtrack-v2
  git add packtrack/config.py packtrack/routes/webhooks.py packtrack/main.py
  git commit -m "feat(phase-c): PackTrack POST /api/webhooks/zoho-sales"
  ```

---

### Task 4: Restart PackTrack and smoke test

**Files:** None created.

- [ ] **Step 1: Apply the Alembic migration on the server**

  ```bash
  ssh root@192.168.1.206 'cd /opt/packtrack && python -m alembic upgrade e3f4a5b6c7d8'
  ```

  Expected output ends with:
  ```
  Running upgrade d2e3f4a5b6c7 -> e3f4a5b6c7d8, sales_events — record Zoho sales confirmations
  ```

- [ ] **Step 2: Add `ZOHO_WEBHOOK_SECRET` to the server env file and restart**

  ```bash
  ssh root@192.168.1.206 "grep -q ZOHO_WEBHOOK_SECRET /etc/packtrack/.env || echo 'ZOHO_WEBHOOK_SECRET=change-me-strong-secret' >> /etc/packtrack/.env"
  ssh root@192.168.1.206 "systemctl restart packtrack"
  ssh root@192.168.1.206 "systemctl is-active packtrack"
  ```

  Expected: `active`

- [ ] **Step 3: Verify the route exists (bad secret → 401, not 404)**

  ```bash
  curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://192.168.1.206/api/webhooks/zoho-sales \
    -H "Content-Type: application/json" \
    -H "X-Zoho-Webhook-Secret: wrong-secret" \
    -d '{"zoho_order_id":"TEST-001","product_sku":"HN-001","qty_sold":10,"sold_at":"2026-05-26T10:00:00Z"}'
  ```

  Expected: `401`

- [ ] **Step 4: Verify a valid payload returns 200 and writes a row**

  Replace `ACTUAL_SECRET` with the value set in `/etc/packtrack/.env`.

  ```bash
  ACTUAL_SECRET=$(ssh root@192.168.1.206 "grep ZOHO_WEBHOOK_SECRET /etc/packtrack/.env | cut -d= -f2")

  curl -s \
    -X POST http://192.168.1.206/api/webhooks/zoho-sales \
    -H "Content-Type: application/json" \
    -H "X-Zoho-Webhook-Secret: $ACTUAL_SECRET" \
    -d '{"zoho_order_id":"SMOKE-001","product_sku":"HN-001","qty_sold":100,"sold_at":"2026-05-26T10:00:00Z"}'
  ```

  Expected response body: `{"ok":true}`

- [ ] **Step 5: Verify row is in the DB**

  ```bash
  ssh root@192.168.1.206 "psql -U packtrack -d packtrack -c \"SELECT zoho_order_id, product_sku, qty_sold, sold_at FROM sales_events WHERE zoho_order_id='SMOKE-001';\""
  ```

  Expected: one row with `SMOKE-001 | HN-001 | 100 | 2026-05-26 10:00:00`.

- [ ] **Step 6: Verify idempotency — same payload returns 200 with skipped=true**

  ```bash
  curl -s \
    -X POST http://192.168.1.206/api/webhooks/zoho-sales \
    -H "Content-Type: application/json" \
    -H "X-Zoho-Webhook-Secret: $ACTUAL_SECRET" \
    -d '{"zoho_order_id":"SMOKE-001","product_sku":"HN-001","qty_sold":100,"sold_at":"2026-05-26T10:00:00Z"}'
  ```

  Expected response body: `{"ok":true,"skipped":true}`

---

### Task 5: Luma schema + Drizzle migration

**Files:**
- Modify: `luma/lib/db/schema.ts`
- Create: `luma/drizzle/0048_finished_lot_sales.sql`

- [ ] **Step 1: Confirm the latest migration on disk is 0047**

  ```bash
  ls /Users/kidevu/luma/drizzle/*.sql | sort | tail -3
  ```

  Expected: last file is `0047_*.sql`. If it's already `0048`, stop — the table was already added.

- [ ] **Step 2: Append `finishedLotSales` table to `luma/lib/db/schema.ts`**

  Open `/Users/kidevu/luma/lib/db/schema.ts`. At the very end of the file, after the last `export type` line, append:

  ```typescript
  // ─────────────────────────────────────────────────────────────────────────────
  // Phase C — Sales Feedback
  // ─────────────────────────────────────────────────────────────────────────────

  /**
   * finished_lot_sales — links a Zoho sales order to the RELEASED finished lot
   * that supplied the units.  Written by the Zoho sales webhook.  Idempotent
   * on (finished_lot_id, zoho_order_id) via the unique index below.
   */
  export const finishedLotSales = pgTable(
    "finished_lot_sales",
    {
      id: uuid("id").primaryKey().defaultRandom(),
      finishedLotId: uuid("finished_lot_id")
        .notNull()
        .references(() => finishedLots.id, { onDelete: "cascade" }),
      zohoOrderId: text("zoho_order_id").notNull(),
      productSku: text("product_sku").notNull(),
      qtySold: integer("qty_sold").notNull(),
      soldAt: timestamp("sold_at", { withTimezone: true }).notNull(),
      linkedAt: timestamp("linked_at", { withTimezone: true }).notNull().defaultNow(),
    },
    (t) => [
      uniqueIndex("finished_lot_sales_pair_unique").on(t.finishedLotId, t.zohoOrderId),
      index("finished_lot_sales_order_idx").on(t.zohoOrderId),
      index("finished_lot_sales_lot_idx").on(t.finishedLotId),
    ],
  );

  export type FinishedLotSale = typeof finishedLotSales.$inferSelect;
  export type FinishedLotSaleInsert = typeof finishedLotSales.$inferInsert;
  ```

- [ ] **Step 3: Generate the Drizzle migration SQL**

  ```bash
  cd /Users/kidevu/luma && npx drizzle-kit generate
  ```

  Expected: Drizzle creates `drizzle/0048_finished_lot_sales.sql` (the name Drizzle assigns may differ slightly — that's fine). Verify:

  ```bash
  ls /Users/kidevu/luma/drizzle/0048_*.sql
  ```

  If the file was not created, Drizzle found no schema changes — the table was already present or the export was not picked up. Recheck the append in Step 2.

- [ ] **Step 4: Inspect the generated SQL to confirm it looks correct**

  ```bash
  cat /Users/kidevu/luma/drizzle/0048_*.sql
  ```

  Expected to contain:
  ```sql
  CREATE TABLE IF NOT EXISTS "finished_lot_sales" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
    "finished_lot_id" uuid NOT NULL,
    "zoho_order_id" text NOT NULL,
    "product_sku" text NOT NULL,
    "qty_sold" integer NOT NULL,
    "sold_at" timestamp with time zone NOT NULL,
    "linked_at" timestamp with time zone DEFAULT now() NOT NULL
  );
  ```

  And three index/unique index lines. If it looks wrong, do not apply — fix the schema and re-generate.

- [ ] **Step 5: Apply the migration on the Luma server**

  ```bash
  ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && docker compose exec -T app npx drizzle-kit migrate"'
  ```

  Expected output ends with something like:
  ```
  [✓] migrations applied successfully
  ```

- [ ] **Step 6: Commit**

  ```bash
  cd /Users/kidevu/luma
  git add lib/db/schema.ts drizzle/0048_*.sql drizzle/meta/
  git commit -m "feat(phase-c): add finished_lot_sales table (migration 0048)"
  ```

---

### Task 6: Luma webhook + BOM endpoint

**Files:**
- Create: `luma/app/api/webhooks/zoho-sales/route.ts`
- Create: `luma/app/api/internal/product-packaging-specs/route.ts`

- [ ] **Step 1: Create the directory structure**

  ```bash
  mkdir -p /Users/kidevu/luma/app/api/webhooks/zoho-sales
  mkdir -p /Users/kidevu/luma/app/api/internal/product-packaging-specs
  ```

- [ ] **Step 2: Create `luma/app/api/webhooks/zoho-sales/route.ts`**

  Create `/Users/kidevu/luma/app/api/webhooks/zoho-sales/route.ts` with the following complete content:

  ```typescript
  // Luma — Zoho sales confirmation webhook.
  //
  // Zoho fires POST here when a sales order is confirmed.
  // We find the most recent RELEASED finished lot for the sold product,
  // then insert a finished_lot_sales row to link the sale to that lot.
  //
  // Auth:      X-Zoho-Webhook-Secret header == process.env.ZOHO_WEBHOOK_SECRET
  // Idempotent: unique index on (finished_lot_id, zoho_order_id).
  //             On duplicate key: return 200 { ok: true, skipped: true }.
  // Independent: failures here do not affect PackTrack's own endpoint.

  import { NextResponse } from "next/server";
  import { db } from "@/lib/db";
  import { eq, desc, and } from "drizzle-orm";
  import {
    finishedLots,
    finishedLotSales,
    products,
  } from "@/lib/db/schema";

  export const runtime = "nodejs";
  export const dynamic = "force-dynamic";

  function unauthorized(reason: string) {
    return NextResponse.json({ ok: false, error: reason }, { status: 401 });
  }

  export async function POST(req: Request) {
    // ── Auth ──────────────────────────────────────────────────────────
    const expected = process.env.ZOHO_WEBHOOK_SECRET;
    if (!expected) {
      console.error("[zoho-sales] ZOHO_WEBHOOK_SECRET is not set — rejecting.");
      return NextResponse.json(
        { ok: false, error: "Webhook secret not configured on server." },
        { status: 503 },
      );
    }
    const got = req.headers.get("x-zoho-webhook-secret");
    if (!got || got !== expected) {
      return unauthorized("Missing or invalid X-Zoho-Webhook-Secret.");
    }

    // ── Parse body ────────────────────────────────────────────────────
    let raw: unknown;
    try {
      raw = await req.json();
    } catch {
      return NextResponse.json(
        { ok: false, error: "Body must be JSON." },
        { status: 400 },
      );
    }

    if (
      typeof raw !== "object" ||
      raw === null ||
      !("zoho_order_id" in raw) ||
      !("product_sku" in raw) ||
      !("qty_sold" in raw) ||
      !("sold_at" in raw)
    ) {
      return NextResponse.json(
        {
          ok: false,
          error:
            "Missing required fields: zoho_order_id, product_sku, qty_sold, sold_at.",
        },
        { status: 400 },
      );
    }

    const body = raw as {
      zoho_order_id: string;
      product_sku: string;
      qty_sold: number;
      sold_at: string;
    };

    const soldAt = new Date(body.sold_at);
    if (isNaN(soldAt.getTime())) {
      return NextResponse.json(
        { ok: false, error: "sold_at must be a valid ISO 8601 datetime string." },
        { status: 400 },
      );
    }

    // ── Find product ──────────────────────────────────────────────────
    const [product] = await db
      .select({ id: products.id })
      .from(products)
      .where(eq(products.sku, body.product_sku))
      .limit(1);

    if (!product) {
      // Product not in Luma yet — log and return 200 so Zoho doesn't retry.
      console.warn(
        "[zoho-sales] product not found in Luma for sku=%s order=%s",
        body.product_sku,
        body.zoho_order_id,
      );
      return NextResponse.json(
        { ok: true, linked_lot: null, reason: "product_not_found" },
        { status: 200 },
      );
    }

    // ── Find most recent RELEASED finished lot for this product ───────
    const [lot] = await db
      .select({ id: finishedLots.id, finishedLotNumber: finishedLots.finishedLotNumber })
      .from(finishedLots)
      .where(
        and(
          eq(finishedLots.productId, product.id),
          eq(finishedLots.status, "RELEASED"),
        ),
      )
      .orderBy(desc(finishedLots.producedOn))
      .limit(1);

    if (!lot) {
      console.warn(
        "[zoho-sales] no RELEASED lot for product=%s order=%s",
        body.product_sku,
        body.zoho_order_id,
      );
      return NextResponse.json(
        { ok: true, linked_lot: null, reason: "no_released_lot" },
        { status: 200 },
      );
    }

    // ── Idempotent insert ─────────────────────────────────────────────
    try {
      await db.insert(finishedLotSales).values({
        finishedLotId: lot.id,
        zohoOrderId: body.zoho_order_id,
        productSku: body.product_sku,
        qtySold: body.qty_sold,
        soldAt: soldAt,
      });
    } catch (err) {
      // Unique constraint violation = duplicate delivery — safe to skip.
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("finished_lot_sales_pair_unique")) {
        console.info(
          "[zoho-sales] duplicate delivery order=%s — skipped",
          body.zoho_order_id,
        );
        return NextResponse.json(
          { ok: true, skipped: true, linked_lot: lot.finishedLotNumber },
          { status: 200 },
        );
      }
      console.error("[zoho-sales] insert failed:", err);
      return NextResponse.json(
        { ok: false, error: "Database insert failed." },
        { status: 500 },
      );
    }

    console.info(
      "[zoho-sales] linked order=%s sku=%s qty=%d lot=%s",
      body.zoho_order_id,
      body.product_sku,
      body.qty_sold,
      lot.finishedLotNumber,
    );

    return NextResponse.json(
      { ok: true, linked_lot: lot.finishedLotNumber },
      { status: 200 },
    );
  }

  export function GET() {
    return NextResponse.json({ ok: false, error: "POST only." }, { status: 405 });
  }
  ```

- [ ] **Step 3: Create `luma/app/api/internal/product-packaging-specs/route.ts`**

  Create `/Users/kidevu/luma/app/api/internal/product-packaging-specs/route.ts` with the following complete content:

  ```typescript
  // Luma — internal BOM read API for Phase D (PackTrack forecasting).
  //
  // Returns all products with their packaging specs joined to
  // packaging_materials.sku (= material_code in PackTrack, e.g. "PT-00095").
  //
  // Auth: X-Luma-PackTrack-Secret header == process.env.LUMA_PACKTRACK_SECRET
  //
  // Response shape:
  // [
  //   {
  //     "product_sku": "HN-001",
  //     "components": [
  //       { "material_code": "PT-00095", "qty_per_unit": 1, "per_scope": "UNIT" }
  //     ]
  //   }
  // ]

  import { NextResponse } from "next/server";
  import { db } from "@/lib/db";
  import { eq } from "drizzle-orm";
  import { productPackagingSpecs, packagingMaterials, products } from "@/lib/db/schema";

  export const runtime = "nodejs";
  export const dynamic = "force-dynamic";

  function unauthorized(reason: string) {
    return NextResponse.json({ ok: false, error: reason }, { status: 401 });
  }

  export async function GET(req: Request) {
    // ── Auth ──────────────────────────────────────────────────────────
    const expected = process.env.LUMA_PACKTRACK_SECRET;
    if (!expected) {
      console.error(
        "[product-packaging-specs] LUMA_PACKTRACK_SECRET is not set — rejecting.",
      );
      return NextResponse.json(
        { ok: false, error: "LUMA_PACKTRACK_SECRET not configured on server." },
        { status: 503 },
      );
    }
    const got = req.headers.get("x-luma-packtrack-secret");
    if (!got || got !== expected) {
      return unauthorized("Missing or invalid X-Luma-PackTrack-Secret.");
    }

    // ── Query: all specs joined to product SKU and material SKU ───────
    const rows = await db
      .select({
        productSku: products.sku,
        materialCode: packagingMaterials.sku,
        qtyPerUnit: productPackagingSpecs.qtyPerUnit,
        perScope: productPackagingSpecs.perScope,
      })
      .from(productPackagingSpecs)
      .innerJoin(
        products,
        eq(productPackagingSpecs.productId, products.id),
      )
      .innerJoin(
        packagingMaterials,
        eq(productPackagingSpecs.packagingMaterialId, packagingMaterials.id),
      )
      .orderBy(products.sku);

    // ── Group by product ──────────────────────────────────────────────
    const byProduct = new Map<
      string,
      Array<{ material_code: string; qty_per_unit: number; per_scope: string }>
    >();

    for (const row of rows) {
      if (!byProduct.has(row.productSku)) {
        byProduct.set(row.productSku, []);
      }
      byProduct.get(row.productSku)!.push({
        material_code: row.materialCode,
        qty_per_unit: row.qtyPerUnit,
        per_scope: row.perScope,
      });
    }

    const result = Array.from(byProduct.entries()).map(
      ([product_sku, components]) => ({ product_sku, components }),
    );

    return NextResponse.json(result, { status: 200 });
  }

  export function POST() {
    return NextResponse.json({ ok: false, error: "GET only." }, { status: 405 });
  }
  ```

- [ ] **Step 4: Commit**

  ```bash
  cd /Users/kidevu/luma
  git add \
    app/api/webhooks/zoho-sales/route.ts \
    app/api/internal/product-packaging-specs/route.ts
  git commit -m "feat(phase-c): Luma sales webhook + internal BOM API"
  ```

---

### Task 7: TypeCheck, deploy Luma, and smoke test both endpoints

**Files:** None created.

- [ ] **Step 1: TypeCheck Luma**

  ```bash
  cd /Users/kidevu/luma && npx tsc --noEmit 2>&1
  ```

  Expected: no output (zero errors). Fix any type errors before deploying. Common issues:
  - `finishedLotSales` not exported from schema — ensure the export is present in `lib/db/schema.ts`
  - `products.sku` not available in the join — confirm `products` is imported in both route files

- [ ] **Step 2: Push to main and wait for Luma to deploy**

  ```bash
  cd /Users/kidevu/luma
  git push origin main
  ```

  The systemd timer on LXC 122 pulls `main` every 60 seconds and runs `docker compose up -d --build` if HEAD changed. Wait 90 seconds, then check:

  ```bash
  ssh root@192.168.1.190 'pct exec 122 -- bash -c "docker compose -f /opt/luma/docker-compose.yml ps"'
  ```

  Expected: the `app` container shows `Up` status.

- [ ] **Step 3: Verify `ZOHO_WEBHOOK_SECRET` is set in Luma's env**

  ```bash
  ssh root@192.168.1.190 'pct exec 122 -- bash -c "grep ZOHO_WEBHOOK_SECRET /etc/luma/.env"'
  ```

  If missing, add it:

  ```bash
  ssh root@192.168.1.190 'pct exec 122 -- bash -c "echo ZOHO_WEBHOOK_SECRET=change-me-strong-secret >> /etc/luma/.env && docker compose -f /opt/luma/docker-compose.yml restart app"'
  ```

  The value must match the one set in `/etc/packtrack/.env` on LXC 200.

- [ ] **Step 4: Smoke test Luma webhook — bad secret → 401**

  ```bash
  curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://192.168.1.134/api/webhooks/zoho-sales \
    -H "Content-Type: application/json" \
    -H "X-Zoho-Webhook-Secret: wrong-secret" \
    -d '{"zoho_order_id":"TEST-001","product_sku":"HN-001","qty_sold":10,"sold_at":"2026-05-26T10:00:00Z"}'
  ```

  Expected: `401`

- [ ] **Step 5: Smoke test Luma webhook — valid payload**

  Replace `ACTUAL_SECRET` with the shared secret value.

  ```bash
  ACTUAL_SECRET=$(ssh root@192.168.1.190 'pct exec 122 -- bash -c "grep ZOHO_WEBHOOK_SECRET /etc/luma/.env | cut -d= -f2"')

  curl -s \
    -X POST http://192.168.1.134/api/webhooks/zoho-sales \
    -H "Content-Type: application/json" \
    -H "X-Zoho-Webhook-Secret: $ACTUAL_SECRET" \
    -d '{"zoho_order_id":"SMOKE-001","product_sku":"HN-001","qty_sold":100,"sold_at":"2026-05-26T10:00:00Z"}'
  ```

  Expected: `{"ok":true,"linked_lot":"<LOT_NUMBER>"}` if a RELEASED lot exists for `HN-001`, or `{"ok":true,"linked_lot":null,"reason":"no_released_lot"}` if none exists yet. Both are correct responses.

- [ ] **Step 6: Smoke test Luma BOM API — bad secret → 401**

  ```bash
  curl -s -o /dev/null -w "%{http_code}" \
    -X GET http://192.168.1.134/api/internal/product-packaging-specs \
    -H "X-Luma-PackTrack-Secret: wrong-secret"
  ```

  Expected: `401`

- [ ] **Step 7: Smoke test Luma BOM API — valid secret**

  ```bash
  LUMA_PT_SECRET=$(ssh root@192.168.1.190 'pct exec 122 -- bash -c "grep LUMA_PACKTRACK_SECRET /etc/luma/.env | cut -d= -f2"')

  curl -s \
    -X GET http://192.168.1.134/api/internal/product-packaging-specs \
    -H "X-Luma-PackTrack-Secret: $LUMA_PT_SECRET" | python3 -m json.tool | head -30
  ```

  Expected: a JSON array. Each element looks like:
  ```json
  {
    "product_sku": "HN-001",
    "components": [
      { "material_code": "PT-00095", "qty_per_unit": 1, "per_scope": "UNIT" }
    ]
  }
  ```

  An empty array `[]` is also valid if no BOM specs have been entered yet.

- [ ] **Step 8: Final commit tagging Phase C complete**

  ```bash
  cd /Users/kidevu/packtrack-v2
  git tag phase-c-sales-webhook
  git push origin phase-c-sales-webhook
  ```

  ```bash
  cd /Users/kidevu/luma
  git tag phase-c-sales-webhook
  git push origin phase-c-sales-webhook
  ```
