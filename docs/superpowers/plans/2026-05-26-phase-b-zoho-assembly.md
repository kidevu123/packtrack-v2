# Phase B: Finished Goods Assembly — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Luma releases a finished lot, it creates a Zoho Manufacture Order via the Zoho gateway, converting packaging material stock into finished goods in Zoho's inventory.

**Architecture:** Three components. The Zoho integration service (FastAPI, LXC 9503) gets a new `POST /zoho/manufacturing_orders/create` route that wraps the Zoho Inventory API. Luma (Next.js 15/TypeScript, LXC 122) gets a new `lib/integrations/zoho/manufacturing.ts` file that calls the gateway. The trigger is the same `setFinishedLotStatus()` RELEASED hook as Phase A — both fire in sequence.

**Pre-condition before starting:** Run `grep -r "zoho_item_id\|zohoItemId" /opt/nexus-resolve/apps/crm/models.py` on LXC 119 and verify that `packaging_materials.zohoItemId` is populated for the materials you intend to use. Phase B silently skips BOM components with no `zohoItemId`.

**Tech Stack:** FastAPI · httpx · Zoho Inventory API v1 (Zoho gateway) · Next.js 15 · TypeScript · Drizzle (Luma)

---

## File map

| Action | Path | Responsibility |
|---|---|---|
| Read first | gateway route file in LXC 9503 | Understand existing route pattern |
| Create | `zoho-integration-service/routers/manufacturing.py` | Gateway route for manufacture orders |
| Modify | `zoho-integration-service/main.py` | Mount new router |
| Create | `luma/lib/integrations/zoho/manufacturing.ts` | Luma outbound call to gateway |
| Modify | `luma/lib/db/schema.ts` | Two new fields on `finishedLots` |
| Create | `luma/drizzle/0028_zoho_manufacture_fields.sql` | Drizzle migration |
| Modify | `luma/lib/db/queries/finished-lots.ts` | Wire push at RELEASED (after Phase A push) |

---

### Task 1: Read the gateway structure before writing anything

**Files:** read-only

- [ ] **SSH into LXC 9503 and read the existing purchase-receives route**
```bash
ssh root@192.168.1.190 'pct exec 9503 -- bash -c "find /opt -name \"*.py\" | xargs grep -l \"purchase_receive\" 2>/dev/null | head -3"'
```

- [ ] **Read that file to understand the exact pattern (auth, Zoho API call, error handling)**
```bash
ssh root@192.168.1.190 'pct exec 9503 -- bash -c "cat $(find /opt -name \"*.py\" | xargs grep -l \"purchase_receive\" 2>/dev/null | head -1)"'
```

- [ ] **Read main.py to see how routers are mounted**
```bash
ssh root@192.168.1.190 'pct exec 9503 -- bash -c "cat /opt/zoho-integration-service/main.py 2>/dev/null || find /opt -name main.py | head -1 | xargs cat"'
```

- [ ] **Note the exact**: base URL for Zoho Inventory API, how the org_id is passed, how the access token is refreshed, and the response error handling pattern. Use these in Task 2.

---

### Task 2: Gateway route for manufacture orders

**Files:**
- Create in gateway repo: `routers/manufacturing.py` (exact path depends on Task 1 findings)

- [ ] **Create the route file following the exact pattern found in Task 1**

The route must implement this contract:

```python
"""
POST /zoho/manufacturing_orders/create

Body:
{
  "composite_item_id": "string",   # Zoho item ID of the finished product
  "quantity_to_manufacture": 1000, # int
  "manufacture_date": "2024-01-15",# YYYY-MM-DD
  "bill_of_materials": [           # packaging components consumed
    {"item_id": "string", "quantity": 1000}
  ]
}

Response 200:
{ "ok": true, "manufacture_order_id": "string", "manufacture_order_number": "string" }

Response 4xx/5xx:
{ "ok": false, "error": "string", "zoho_code": int | null }
"""
```

The Zoho Inventory API endpoint is:
`POST https://www.zohoapis.com/inventory/v1/manufacturingorders?organization_id={org_id}`

Payload Zoho expects:
```json
{
  "composite_item_id": "...",
  "quantity_to_manufacture": 1000,
  "manufacture_date": "2024-01-15",
  "consumed_items": [
    {"item_id": "...", "quantity": 1000}
  ]
}
```

**Note:** Verify `consumed_items` vs `bill_of_materials` in the actual Zoho API docs for your Zoho region before sending. Zoho India uses `.in` domains; US uses `.com`.

- [ ] **Add the router to gateway main.py** following the same pattern as other routers.

- [ ] **Restart the gateway service**
```bash
ssh root@192.168.1.190 'pct exec 9503 -- bash -c "systemctl restart zoho-integration-service 2>/dev/null || supervisorctl restart all 2>/dev/null"'
```

- [ ] **Smoke test the new route (dry run with invalid data — expect Zoho auth error, not 404)**
```bash
GATEWAY_URL=$(ssh root@192.168.1.190 'pct exec 9503 -- bash -c "grep GATEWAY_URL /etc/*.env 2>/dev/null | head -1 | cut -d= -f2"')
# Just verify the route exists and auth works:
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://192.168.1.9503_IP/zoho/manufacturing_orders/create \
  -H "Content-Type: application/json" \
  -d '{"composite_item_id":"test","quantity_to_manufacture":1,"manufacture_date":"2026-01-01","bill_of_materials":[]}'
```
Expected: not `404` (route exists); may be `401` or `422` depending on auth.

---

### Task 3: Luma schema additions

**Files:**
- Modify: `luma/lib/db/schema.ts`

- [ ] **Add two columns to `finishedLots`** (after the `packtrackConsumptionError` from Phase A):
```typescript
// Phase B — Zoho manufacture order push state
zohoManufactureOrderId: text("zoho_manufacture_order_id"),
zohoManufactureError: text("zoho_manufacture_error"),
```

- [ ] **Generate and apply migration**
```bash
cd /Users/kidevu/luma
npx drizzle-kit generate
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && docker compose exec -T app npx drizzle-kit migrate"'
```

- [ ] **Commit**
```bash
git add lib/db/schema.ts drizzle/
git commit -m "feat(phase-b): add zoho_manufacture fields to finished_lots"
```

---

### Task 4: Luma manufacturing integration file

**Files:**
- Create: `luma/lib/integrations/zoho/manufacturing.ts`

- [ ] **Read the gateway.ts file for the base URL and auth header pattern**
```bash
head -40 /Users/kidevu/luma/lib/integrations/zoho/gateway.ts
```

- [ ] **Create `luma/lib/integrations/zoho/manufacturing.ts`**

```typescript
/**
 * Phase B — Zoho manufacture order creation.
 *
 * Called when a finishedLot transitions to RELEASED.
 * Creates a Zoho Manufacture Order to convert packaging material stock
 * into finished goods in Zoho's inventory.
 *
 * Auth: X-Internal-Token + X-Brand headers (same as all Zoho gateway calls).
 * Never throws — returns {ok, error} so failures don't block lot release.
 */

type BomItem = {
  item_id: string;   // Zoho item ID of the packaging material
  quantity: number;
};

export type ManufactureOrderPayload = {
  composite_item_id: string;    // Zoho item ID of the finished product
  quantity_to_manufacture: number;
  manufacture_date: string;     // YYYY-MM-DD
  bill_of_materials: BomItem[];
};

export type ManufactureOrderResult =
  | { ok: true; manufacture_order_id: string; manufacture_order_number: string }
  | { ok: false; reason: string };

export function isManufacturingConfigured(): boolean {
  return !!(
    process.env.ZOHO_INTEGRATION_URL &&
    process.env.ZOHO_INTEGRATION_SECRET &&
    process.env.ZOHO_BRAND
  );
}

export async function createManufactureOrder(
  payload: ManufactureOrderPayload,
): Promise<ManufactureOrderResult> {
  if (!isManufacturingConfigured()) {
    return { ok: false, reason: "Zoho gateway not configured" };
  }
  if (payload.bill_of_materials.length === 0) {
    return { ok: false, reason: "No BOM items — all packaging materials missing zohoItemId" };
  }

  const base = process.env.ZOHO_INTEGRATION_URL!.replace(/\/$/, "");
  const secret = process.env.ZOHO_INTEGRATION_SECRET!;
  const brand = process.env.ZOHO_BRAND!;

  try {
    const res = await fetch(`${base}/zoho/manufacturing_orders/create`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Token": secret,
        "X-Brand": brand,
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(30_000),
    });

    const body = await res.json().catch(() => ({})) as Record<string, unknown>;
    if (!res.ok) {
      return { ok: false, reason: `HTTP ${res.status}: ${String(body.error ?? res.statusText).slice(0, 200)}` };
    }

    return {
      ok: true,
      manufacture_order_id: String(body.manufacture_order_id ?? ""),
      manufacture_order_number: String(body.manufacture_order_number ?? ""),
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return { ok: false, reason: msg.replace(secret, "[REDACTED]") };
  }
}
```

- [ ] **Commit**
```bash
cd /Users/kidevu/luma
git add lib/integrations/zoho/manufacturing.ts
git commit -m "feat(phase-b): Zoho manufacture order integration module"
```

---

### Task 5: Build the BOM helper for finished lots

**Files:**
- Modify: `luma/lib/db/queries/finished-lots.ts`

- [ ] **Add a helper to build the BOM for a finished lot** before `setFinishedLotStatus`:

```typescript
import type { BomItem } from "@/lib/integrations/zoho/manufacturing";

// Re-use the type from the manufacturing module (adjust import path if needed)
type ManufactureBomItem = { item_id: string; quantity: number };

/**
 * Build the Zoho BOM for a finished lot.
 * Skips packaging materials without a zohoItemId (logged as warning).
 */
async function buildZohoBom(
  finishedLotId: string,
): Promise<ManufactureBomItem[]> {
  const rows = await db
    .select({
      zohoItemId: packagingMaterials.zohoItemId,
      qtyConsumed: finishedLotInputs.qtyConsumed,
      materialName: packagingMaterials.name,
    })
    .from(finishedLotInputs)
    .innerJoin(batches, eq(batches.id, finishedLotInputs.batchId))
    .innerJoin(packagingMaterials, eq(packagingMaterials.id, batches.packagingMaterialId))
    .where(eq(finishedLotInputs.finishedLotId, finishedLotId));

  const bom: ManufactureBomItem[] = [];
  for (const r of rows) {
    if (!r.zohoItemId) {
      console.warn(`[zoho.manufacturing] skipping ${r.materialName} — no zohoItemId`);
      continue;
    }
    bom.push({ item_id: r.zohoItemId, quantity: r.qtyConsumed ?? 0 });
  }
  return bom;
}
```

- [ ] **Wire into `setFinishedLotStatus`** — add after the Phase A push block (before `return row;`):

```typescript
    // Phase B — Zoho manufacture order (fire-and-forget, never blocks lot release).
    if (next === "RELEASED" && before.status !== "RELEASED") {
      void (async () => {
        try {
          const { isManufacturingConfigured, createManufactureOrder } = await import(
            "@/lib/integrations/zoho/manufacturing"
          );
          if (!isManufacturingConfigured()) return;

          // Need composite_item_id (Zoho ID of the finished product)
          const [lotMeta] = await db
            .select({
              unitsProduced: finishedLots.unitsProduced,
              zohoItemId: products.zohoItemId,
              producedOn: finishedLots.producedOn,
            })
            .from(finishedLots)
            .innerJoin(products, eq(products.id, finishedLots.productId))
            .where(eq(finishedLots.id, id));

          if (!lotMeta?.zohoItemId) {
            await db.update(finishedLots)
              .set({ zohoManufactureError: "Product has no zohoItemId" })
              .where(eq(finishedLots.id, id));
            return;
          }

          const bom = await buildZohoBom(id);
          const result = await createManufactureOrder({
            composite_item_id: lotMeta.zohoItemId,
            quantity_to_manufacture: lotMeta.unitsProduced ?? 0,
            manufacture_date: (lotMeta.producedOn ?? new Date()).toISOString().slice(0, 10),
            bill_of_materials: bom,
          });

          await db
            .update(finishedLots)
            .set(
              result.ok
                ? { zohoManufactureOrderId: result.manufacture_order_id, zohoManufactureError: null }
                : { zohoManufactureError: result.reason },
            )
            .where(eq(finishedLots.id, id));
        } catch (err) {
          console.error("[zoho.manufacturing] fire-and-forget error:", err);
        }
      })();
    }
```

- [ ] **TypeScript check**
```bash
cd /Users/kidevu/luma
npx tsc --noEmit 2>&1 | head -20
```
Expected: no errors. Add any missing imports (`products`, `batches`, `packagingMaterials`) from `@/lib/db/schema`.

- [ ] **Bump Luma version** in `package.json` (e.g. `0.2.43`)

- [ ] **Commit and push**
```bash
git add lib/db/queries/finished-lots.ts package.json
git commit -m "feat(phase-b): wire Zoho manufacture order on finishedLot RELEASED — v0.2.43"
git push
```

- [ ] **Deploy Luma**
```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && git pull && docker compose up -d --build"'
```

---

### Task 6: Smoke test

- [ ] **Release a finished lot in Luma admin, then verify**
```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "docker compose exec -T db psql -U luma -c \"SELECT finished_lot_number, zoho_manufacture_order_id, zoho_manufacture_error FROM finished_lots ORDER BY created_at DESC LIMIT 5;\""'
```
Expected: `zoho_manufacture_order_id` is set (not null), `zoho_manufacture_error` is null.

- [ ] **In Zoho Inventory, verify the manufacture order was created**

Log into Zoho → Manufacturing → Manufacturing Orders. The new order should appear with the correct finished product and consumed quantities.
