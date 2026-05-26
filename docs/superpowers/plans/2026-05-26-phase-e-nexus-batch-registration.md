# Phase E: Nexus Batch Registration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every finishedLot released in Luma is automatically registered in Nexus Resolve, giving complaint agents a dropdown of real production batches instead of free-text guessing.

**Architecture:** Fire-and-forget push from Luma's `setFinishedLotStatus()` RELEASED hook to a new `POST /api/batches/import` endpoint in Nexus. New DRF permission class + serializer + APIView in the existing `apps/api` app. Completely separate from the existing Luma → Nexus shipment-traceability flow.

**Pre-condition:** `NEXUS_URL` and `LUMA_NEXUS_SECRET` must be set in Luma's `.env` on LXC 122. `LUMA_NEXUS_SECRET` must be set in Nexus's `.env` on LXC 119. These are new env vars — check `/etc/luma/.env` and `/etc/nexus/.env` before starting.

**Tech Stack:** Django 4.2 · Django REST Framework · Next.js 15 · TypeScript · Drizzle

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Read first | `apps/api/permissions.py` (Nexus LXC 119) | Understand existing DRF permission pattern |
| Read first | `apps/api/views.py` (Nexus LXC 119) | Understand existing DRF view pattern |
| Read first | `apps/api/serializers.py` (Nexus LXC 119) | Understand existing DRF serializer pattern |
| Read first | `apps/api/urls.py` (Nexus LXC 119) | Understand existing URL registration pattern |
| Read first | `nexus/settings.py` (Nexus LXC 119) | Understand how env vars are loaded |
| Modify | `apps/api/permissions.py` (Nexus LXC 119) | Add `IsLumaNexusSecret` permission class |
| Modify | `apps/api/serializers.py` (Nexus LXC 119) | Add `PackagingInputSerializer`, `BatchImportSerializer` |
| Modify | `apps/api/views.py` (Nexus LXC 119) | Add `BatchImportView` (APIView) |
| Modify | `apps/api/urls.py` (Nexus LXC 119) | Register `batches/import` URL |
| Modify | `nexus/settings.py` (Nexus LXC 119) | Add `LUMA_NEXUS_SECRET` env var |
| Create | `luma/lib/integrations/nexus/batch-registration.ts` | Outbound HTTP call to Nexus, result type, config check |
| Modify | `luma/lib/db/schema.ts` | Add `nexusBatchRegisteredAt`, `nexusBatchRegisterError` to `finishedLots` |
| Create | `luma/drizzle/0049_nexus_batch_fields.sql` | Drizzle migration for the two new columns |
| Modify | `luma/lib/db/queries/finished-lots.ts` | Add fire-and-forget block at RELEASED |

---

## Task 1: Read Nexus API structure

**Files:**
- Read: `apps/api/permissions.py` on LXC 119
- Read: `apps/api/views.py` on LXC 119
- Read: `apps/api/serializers.py` on LXC 119
- Read: `apps/api/urls.py` on LXC 119
- Read: `nexus/settings.py` on LXC 119

- [ ] **Step 1: Read permissions.py**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /opt/nexus-resolve/apps/api/permissions.py"'
```

Note the class name for the existing permission (`IsStaffRole`), what base class it uses, and how it checks credentials. This informs how `IsLumaNexusSecret` should be structured.

- [ ] **Step 2: Read views.py**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /opt/nexus-resolve/apps/api/views.py"'
```

Note how imports are organized, whether there are existing `APIView` subclasses or only `ViewSet`s, and how `permission_classes` is set.

- [ ] **Step 3: Read serializers.py**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /opt/nexus-resolve/apps/api/serializers.py"'
```

Note how existing serializers are structured. Identify whether `ModelSerializer` or plain `Serializer` is used and how nested serializers are written.

- [ ] **Step 4: Read urls.py**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /opt/nexus-resolve/apps/api/urls.py"'
```

Confirm the `urlpatterns` list structure and the router usage.

- [ ] **Step 5: Check settings.py for env var loading pattern**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "grep -n \"env\|environ\|getenv\|SECRET\|ZOHO\|NEXUS\" /opt/nexus-resolve/nexus/settings.py | head -40"'
```

Determine whether the project uses `django-environ` (`env("VAR")`) or raw `os.environ.get("VAR", "")`. This controls exactly how to add `LUMA_NEXUS_SECRET`.

- [ ] **Step 6: Verify Batch and Product models are in apps/crm/models.py**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "grep -n \"class Batch\|class Product\|class Manufacturer\" /opt/nexus-resolve/apps/crm/models.py"'
```

Confirm model names match the spec. Note line numbers so the import in views.py is correct.

- [ ] **Step 7: Check existing /etc/nexus/.env for current vars**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /etc/nexus/.env 2>/dev/null || cat /opt/nexus-resolve/.env 2>/dev/null || echo \"not found\""'
```

Note whether `LUMA_NEXUS_SECRET` already exists. If not, you will add it in Task 2. Record the actual file path for use in later steps.

---

## Task 2: Nexus batch import endpoint

**Files:**
- Modify: `apps/api/permissions.py` on LXC 119
- Modify: `apps/api/serializers.py` on LXC 119
- Modify: `apps/api/views.py` on LXC 119
- Modify: `apps/api/urls.py` on LXC 119
- Modify: `nexus/settings.py` on LXC 119
- Modify: `/etc/nexus/.env` on LXC 119

- [ ] **Step 1: Add IsLumaNexusSecret to permissions.py**

Append the following class to the end of `apps/api/permissions.py`. Do not remove or modify `IsStaffRole`.

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat >> /opt/nexus-resolve/apps/api/permissions.py << '\''PYEOF'\''


class IsLumaNexusSecret(BasePermission):
    \"\"\"
    Allows access only when the request carries the correct X-Luma-Nexus-Secret header.
    Used by the batch import endpoint called by Luma on finishedLot RELEASED.
    \"\"\"

    def has_permission(self, request, view):
        from django.conf import settings
        expected = getattr(settings, \"LUMA_NEXUS_SECRET\", \"\")
        if not expected:
            return False
        return request.headers.get(\"X-Luma-Nexus-Secret\", \"\") == expected
PYEOF
"'
```

Verify it was appended cleanly:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "tail -20 /opt/nexus-resolve/apps/api/permissions.py"'
```

Expected: the `IsLumaNexusSecret` class visible at the end of the file, no syntax errors visible.

- [ ] **Step 2: Add BatchImportSerializer and PackagingInputSerializer to serializers.py**

Append to the end of `apps/api/serializers.py`. Do not remove or modify existing serializers.

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat >> /opt/nexus-resolve/apps/api/serializers.py << '\''PYEOF'\''


class PackagingInputSerializer(serializers.Serializer):
    material_code = serializers.CharField(max_length=128)
    material_name = serializers.CharField(max_length=255)
    supplier_lot_number = serializers.CharField(max_length=128, required=False, allow_blank=True)


class BatchImportSerializer(serializers.Serializer):
    lot_number = serializers.CharField(max_length=128)
    product_sku = serializers.CharField(max_length=128)
    product_description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    produced_on = serializers.DateField()
    units_produced = serializers.IntegerField(min_value=0)
    luma_finished_lot_id = serializers.UUIDField()
    packaging_inputs = PackagingInputSerializer(many=True, default=list)
PYEOF
"'
```

Verify:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "tail -25 /opt/nexus-resolve/apps/api/serializers.py"'
```

Expected: both new serializer classes visible.

- [ ] **Step 3: Add BatchImportView to views.py**

First check the current imports in views.py so you know what is already imported:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "head -20 /opt/nexus-resolve/apps/api/views.py"'
```

Then append the new view. This adds the necessary imports inline at the top of the appended block (safe to have duplicate imports in Python — they are harmless, but if `APIView` and `Response` are already imported you can skip those lines):

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat >> /opt/nexus-resolve/apps/api/views.py << '\''PYEOF'\''


# --- Phase E: Nexus Batch Import ---
from rest_framework.views import APIView
from apps.crm.models import Batch, Manufacturer, Product
from .permissions import IsLumaNexusSecret
from .serializers import BatchImportSerializer


class BatchImportView(APIView):
    \"\"\"
    POST /api/batches/import

    Accepts a finished lot payload from Luma when status changes to RELEASED.
    Upserts (lot_number, product) in the Batch table. Idempotent.
    Auth: X-Luma-Nexus-Secret header.
    \"\"\"

    permission_classes = [IsLumaNexusSecret]

    def post(self, request):
        serializer = BatchImportSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {\"detail\": \"Validation error\", \"errors\": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data

        # 1. Get or create Manufacturer
        manufacturer, _ = Manufacturer.objects.get_or_create(
            code=\"HAUTE\",
            defaults={\"name\": \"Haute Nutrition\", \"quality_score\": \"100.00\"},
        )

        # 2. Get or create Product by SKU
        product, _ = Product.objects.get_or_create(
            sku=data[\"product_sku\"],
            defaults={
                \"description\": data.get(\"product_description\", \"\"),
                \"manufacturer\": manufacturer,
            },
        )

        # 3. Upsert Batch — unique_together is (lot_number, product)
        batch, created = Batch.objects.update_or_create(
            lot_number=data[\"lot_number\"],
            product=product,
            defaults={
                \"manufactured_on\": data[\"produced_on\"],
                \"metadata\": {
                    \"luma_finished_lot_id\": str(data[\"luma_finished_lot_id\"]),
                    \"units_produced\": data[\"units_produced\"],
                    \"packaging_inputs\": data[\"packaging_inputs\"],
                },
            },
        )

        return Response(
            {\"ok\": True, \"batch_id\": batch.pk, \"created\": created},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
PYEOF
"'
```

Verify:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "tail -60 /opt/nexus-resolve/apps/api/views.py"'
```

Expected: `BatchImportView` visible at end of file.

- [ ] **Step 4: Register the URL in apps/api/urls.py**

Read the current file first:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /opt/nexus-resolve/apps/api/urls.py"'
```

The file currently ends with `urlpatterns = [path("", include(router.urls))]`. You need to add the `BatchImportView` import and a new path. Write the full replacement of the file (safest approach — avoids fragile appends to `urlpatterns`):

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat > /opt/nexus-resolve/apps/api/urls.py << '\''PYEOF'\''
from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import BatchImportView, Customer360ViewSet, TicketViewSet

router = DefaultRouter()
router.register(r\"tickets\", TicketViewSet, basename=\"ticket\")
router.register(r\"customers\", Customer360ViewSet, basename=\"customer360\")

urlpatterns = [
    path(\"\", include(router.urls)),
    path(\"batches/import\", BatchImportView.as_view(), name=\"batch_import\"),
]
PYEOF
"'
```

Verify:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cat /opt/nexus-resolve/apps/api/urls.py"'
```

Expected: both router URLs and `batches/import` path present.

- [ ] **Step 5: Add LUMA_NEXUS_SECRET to settings.py**

Check whether the project uses `django-environ` or `os.environ`:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "grep -n \"import environ\|import os\|env = \|os.environ\" /opt/nexus-resolve/nexus/settings.py | head -10"'
```

**If django-environ is used** (`env = environ.Env(...)` pattern), append this line in the env var block of settings.py. Find a good insertion point:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "grep -n \"env(\" /opt/nexus-resolve/nexus/settings.py | tail -5"'
```

Then insert after the last `env(` line. Exact approach depends on what you see. The value to add is:

```python
LUMA_NEXUS_SECRET = env("LUMA_NEXUS_SECRET", default="")
```

**If raw os.environ is used**, append:

```python
LUMA_NEXUS_SECRET = os.environ.get("LUMA_NEXUS_SECRET", "")
```

Use whichever form matches the existing pattern. To append safely to the end of settings.py (works for both patterns — Django will use whichever definition appears last):

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "echo \"\" >> /opt/nexus-resolve/nexus/settings.py && echo \"# Phase E — Luma → Nexus batch registration auth\" >> /opt/nexus-resolve/nexus/settings.py && echo \"LUMA_NEXUS_SECRET = os.environ.get(\\\"LUMA_NEXUS_SECRET\\\", \\\"\\\")\" >> /opt/nexus-resolve/nexus/settings.py"'
```

If the project uses `django-environ` and `os` is not imported in settings.py, use the `env()` form instead:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "echo \"\" >> /opt/nexus-resolve/nexus/settings.py && echo \"# Phase E — Luma -> Nexus batch registration auth\" >> /opt/nexus-resolve/nexus/settings.py && echo \"LUMA_NEXUS_SECRET = env(\\\"LUMA_NEXUS_SECRET\\\", default=\\\"\\\")\" >> /opt/nexus-resolve/nexus/settings.py"'
```

Verify the line was added:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "tail -5 /opt/nexus-resolve/nexus/settings.py"'
```

- [ ] **Step 6: Add LUMA_NEXUS_SECRET to /etc/nexus/.env**

Generate a shared secret (if one does not exist yet) and add it. Use the same secret you will put in Luma's `.env`:

```bash
# Generate a secure random secret (run this locally, note the value)
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Add it to Nexus's env file (replace `<GENERATED_SECRET>` with the value from above):

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "echo \"LUMA_NEXUS_SECRET=<GENERATED_SECRET>\" >> /etc/nexus/.env"'
```

Verify:

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "grep LUMA_NEXUS_SECRET /etc/nexus/.env"'
```

Expected: `LUMA_NEXUS_SECRET=<value>` — not blank.

---

## Task 3: Restart Nexus and smoke test

**Files:** none

- [ ] **Step 1: Check Django syntax before restarting**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cd /opt/nexus-resolve && source venv/bin/activate 2>/dev/null || true && python manage.py check 2>&1"'
```

Expected: `System check identified no issues (0 silenced).`

If you see import errors, read the error carefully. Common causes:
- `APIView` already imported in views.py → the duplicate import is fine in Python, but if there's a circular import issue remove the duplicate from the appended block.
- `Batch`, `Product`, or `Manufacturer` not in `apps.crm.models` → check the exact model location with `grep -rn "class Batch" /opt/nexus-resolve/`.

- [ ] **Step 2: Restart the Nexus service**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "systemctl restart nexus 2>/dev/null || supervisorctl restart nexus 2>/dev/null"'
```

- [ ] **Step 3: Confirm service is running**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "systemctl is-active nexus 2>/dev/null || supervisorctl status nexus 2>/dev/null"'
```

Expected: `active` or `RUNNING`.

- [ ] **Step 4: Smoke test — wrong secret returns 401, not 404**

```bash
curl -s -o /dev/null -w "%{http_code}" \
  -X POST http://192.168.1.190:8000/api/batches/import \
  -H "Content-Type: application/json" \
  -H "X-Luma-Nexus-Secret: wrong-secret" \
  -d '{}'
```

Expected: `401`

If you get `404`, the URL is not registered — recheck `apps/api/urls.py` and confirm `nexus/urls.py` still mounts `apps.api.urls` at `/api/`.

If you get `403`, the DRF default permission is overriding `IsLumaNexusSecret`. Check `DEFAULT_PERMISSION_CLASSES` in settings.py and confirm the view explicitly sets `permission_classes = [IsLumaNexusSecret]`.

- [ ] **Step 5: Smoke test — missing fields returns 400 with correct secret**

```bash
# Replace <GENERATED_SECRET> with your actual secret
curl -s \
  -X POST http://192.168.1.190:8000/api/batches/import \
  -H "Content-Type: application/json" \
  -H "X-Luma-Nexus-Secret: <GENERATED_SECRET>" \
  -d '{}'
```

Expected: HTTP 400 with body containing `{"detail": "Validation error", "errors": {...}}`.

- [ ] **Step 6: Smoke test — full valid payload returns 201**

```bash
curl -s \
  -X POST http://192.168.1.190:8000/api/batches/import \
  -H "Content-Type: application/json" \
  -H "X-Luma-Nexus-Secret: <GENERATED_SECRET>" \
  -d '{
    "lot_number": "TEST-LOT-001",
    "product_sku": "HN-TEST-001",
    "product_description": "Test Product",
    "produced_on": "2026-05-26",
    "units_produced": 100,
    "luma_finished_lot_id": "00000000-0000-0000-0000-000000000001",
    "packaging_inputs": [
      {
        "material_code": "PT-00001",
        "material_name": "Blister Card",
        "supplier_lot_number": "SL-2026-001"
      }
    ]
  }'
```

Expected: `{"ok": true, "batch_id": <some int>, "created": true}`

- [ ] **Step 7: Smoke test — same payload again returns 200 (idempotent)**

Run the exact same `curl` command from Step 6 a second time.

Expected: `{"ok": true, "batch_id": <same int>, "created": false}`

---

## Task 4: Luma batch-registration module

**Files:**
- Create: `/Users/kidevu/luma/lib/integrations/nexus/batch-registration.ts`

- [ ] **Step 1: Confirm the existing nexus integration directory exists**

```bash
ls /Users/kidevu/luma/lib/integrations/nexus/
```

Expected: `finished-lots.ts` present (and possibly other files). You will add `batch-registration.ts` alongside it — do NOT modify `finished-lots.ts`.

- [ ] **Step 2: Create batch-registration.ts**

```bash
cat > /Users/kidevu/luma/lib/integrations/nexus/batch-registration.ts << 'EOF'
/**
 * Phase E — Automatic Nexus batch registration on finishedLot RELEASED.
 *
 * Every production batch is registered in Nexus so complaint agents
 * can select from real production data instead of free-text guessing.
 *
 * This is separate from lib/integrations/nexus/finished-lots.ts,
 * which handles customer-shipment-specific traceability.
 *
 * Never throws — returns { ok, reason } so failures never block lot release.
 *
 * Required env vars:
 *   NEXUS_URL          e.g. http://192.168.1.190:8000
 *   LUMA_NEXUS_SECRET  shared secret, must match /etc/nexus/.env on LXC 119
 */

type PackagingInput = {
  material_code: string;
  material_name: string;
  supplier_lot_number: string;
};

export type BatchRegistrationPayload = {
  lot_number: string;
  product_sku: string;
  product_description: string;
  produced_on: string;        // YYYY-MM-DD
  units_produced: number;
  luma_finished_lot_id: string;
  packaging_inputs: PackagingInput[];
};

export type BatchRegistrationResult =
  | { ok: true; batch_id: number; created: boolean }
  | { ok: false; reason: string };

/**
 * Returns true only when both required env vars are set.
 * Called before attempting registration so the fire-and-forget block can
 * short-circuit without making a network call.
 */
export function isBatchRegistrationConfigured(): boolean {
  return !!(process.env.NEXUS_URL && process.env.LUMA_NEXUS_SECRET);
}

/**
 * POSTs the payload to Nexus /api/batches/import.
 * Never throws — all errors are captured and returned as { ok: false, reason }.
 */
export async function registerBatchInNexus(
  payload: BatchRegistrationPayload,
): Promise<BatchRegistrationResult> {
  if (!isBatchRegistrationConfigured()) {
    return { ok: false, reason: "Nexus not configured (missing NEXUS_URL or LUMA_NEXUS_SECRET)" };
  }

  const base = process.env.NEXUS_URL!.replace(/\/$/, "");
  const secret = process.env.LUMA_NEXUS_SECRET!;

  try {
    const res = await fetch(`${base}/api/batches/import`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Luma-Nexus-Secret": secret,
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(10_000),
    });

    const body = await res.json().catch(() => ({}) as Record<string, unknown>) as Record<string, unknown>;

    if (!res.ok) {
      return {
        ok: false,
        reason: `HTTP ${res.status}: ${String(body.detail ?? res.statusText).slice(0, 200)}`,
      };
    }

    return {
      ok: true,
      batch_id: Number(body.batch_id),
      created: Boolean(body.created),
    };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    // Redact the secret in case it appears in an error message
    return { ok: false, reason: msg.replace(secret, "[REDACTED]") };
  }
}
EOF
```

- [ ] **Step 3: Verify the file was created**

```bash
cat /Users/kidevu/luma/lib/integrations/nexus/batch-registration.ts
```

Expected: full file content visible with no truncation.

- [ ] **Step 4: TypeScript syntax check**

```bash
cd /Users/kidevu/luma && npx tsc --noEmit --project tsconfig.json 2>&1 | head -40
```

Expected: no errors from `batch-registration.ts`. Fix any type errors before proceeding.

---

## Task 5: Luma schema + migration

**Files:**
- Modify: `/Users/kidevu/luma/lib/db/schema.ts`
- Create: `/Users/kidevu/luma/drizzle/0049_nexus_batch_fields.sql`

- [ ] **Step 1: Find the finishedLots table definition in schema.ts**

```bash
grep -n "finishedLots\|nexusBatch\|zohoManufacture\|finishedLotNumber" /Users/kidevu/luma/lib/db/schema.ts | head -20
```

Identify the line where `zohoManufactureError` (the last Phase B field) is defined. The two new Phase E fields go immediately after it.

- [ ] **Step 2: Read the relevant section of schema.ts**

```bash
# Replace <LINE> with the line number of zohoManufactureError from step 1
sed -n '<LINE-5>,<LINE+10>p' /Users/kidevu/luma/lib/db/schema.ts
```

This lets you see the exact column definition format used in this file (column name casing, `timestamp` vs `timestamptz`, nullable syntax).

- [ ] **Step 3: Add the two new fields after zohoManufactureError**

Open `/Users/kidevu/luma/lib/db/schema.ts` and locate the `zohoManufactureError` line. Insert the following two lines immediately after it, matching the indentation of the surrounding fields:

```typescript
  // Phase E — Nexus batch registration
  nexusBatchRegisteredAt: timestamp("nexus_batch_registered_at", { withTimezone: true }),
  nexusBatchRegisterError: text("nexus_batch_register_error"),
```

The result should look like (surrounding context shown for placement):

```typescript
  // Phase B — Zoho manufacture order
  zohoManufactureOrderId: text("zoho_manufacture_order_id"),
  zohoManufactureError: text("zoho_manufacture_error"),
  // Phase E — Nexus batch registration
  nexusBatchRegisteredAt: timestamp("nexus_batch_registered_at", { withTimezone: true }),
  nexusBatchRegisterError: text("nexus_batch_register_error"),
```

- [ ] **Step 4: Verify the schema edit**

```bash
grep -n "nexusBatch\|zohoManufacture" /Users/kidevu/luma/lib/db/schema.ts
```

Expected: `zohoManufactureError` followed immediately by `nexusBatchRegisteredAt` then `nexusBatchRegisterError`.

- [ ] **Step 5: TypeScript check on schema**

```bash
cd /Users/kidevu/luma && npx tsc --noEmit --project tsconfig.json 2>&1 | grep "schema" | head -20
```

Expected: no errors from schema.ts.

- [ ] **Step 6: Create the Drizzle migration SQL file**

```bash
cat > /Users/kidevu/luma/drizzle/0049_nexus_batch_fields.sql << 'EOF'
-- Phase E: Nexus batch registration fields on finished_lots
-- Migration: 0049_nexus_batch_fields

ALTER TABLE "finished_lots"
  ADD COLUMN IF NOT EXISTS "nexus_batch_registered_at" TIMESTAMP WITH TIME ZONE,
  ADD COLUMN IF NOT EXISTS "nexus_batch_register_error" TEXT;
EOF
```

- [ ] **Step 7: Verify the migration file**

```bash
cat /Users/kidevu/luma/drizzle/0049_nexus_batch_fields.sql
```

Expected: two `ADD COLUMN IF NOT EXISTS` statements.

- [ ] **Step 8: Apply the migration on LXC 122 (Luma's database)**

First, check the Luma LXC number and DB connection from your infrastructure notes (LXC 122). Apply via SSH:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && cat drizzle/0049_nexus_batch_fields.sql | psql \$DATABASE_URL"'
```

If that command path is wrong, check the Luma deploy location:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "ls /opt/luma/drizzle/ | tail -5"'
```

- [ ] **Step 9: Confirm the columns exist in the database**

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && psql \$DATABASE_URL -c \"\\d finished_lots\" 2>&1 | grep nexus"'
```

Expected: two rows — `nexus_batch_registered_at` and `nexus_batch_register_error`.

---

## Task 6: Wire at RELEASED, typecheck, deploy, smoke test

**Files:**
- Modify: `/Users/kidevu/luma/lib/db/queries/finished-lots.ts`
- Modify: `/etc/luma/.env` on LXC 122

- [ ] **Step 1: Read the current RELEASED hook section in finished-lots.ts**

```bash
grep -n "RELEASED\|Phase A\|Phase B\|fire-and-forget\|nexus\|zoho" /Users/kidevu/luma/lib/db/queries/finished-lots.ts | head -30
```

Identify: the line where Phase A's fire-and-forget block starts, and the line where Phase B's block ends. Phase E goes immediately after Phase B's closing `})();` line.

Also check the imports at the top of the file:

```bash
head -30 /Users/kidevu/luma/lib/db/queries/finished-lots.ts
```

Note which Drizzle helpers are imported (`eq`, `and`, etc.) — use the same import style. Confirm `finishedLotInputs`, `batches`, `packagingMaterials`, `products` are imported or add them if not.

- [ ] **Step 2: Check the batches table for supplierLotNumber**

```bash
grep -n "supplierLotNumber\|supplier_lot" /Users/kidevu/luma/lib/db/schema.ts | head -10
```

If `supplierLotNumber` exists on the `batches` table, use it in the query. If not, use `sql\`''\`` or an empty string literal. Note the exact field name for use in Step 3.

- [ ] **Step 3: Check which table imports are already in the file**

```bash
grep -n "finishedLotInputs\|packagingMaterials\|products\b" /Users/kidevu/luma/lib/db/queries/finished-lots.ts | head -10
```

If any of `finishedLotInputs`, `packagingMaterials`, `products` are missing from the imports, add them. They come from the schema file, likely imported as:

```typescript
import { ..., finishedLotInputs, packagingMaterials, products } from "@/lib/db/schema";
```

- [ ] **Step 4: Add the Phase E fire-and-forget block to setFinishedLotStatus**

Locate the end of Phase B's fire-and-forget block (the `})();` that closes it). Insert the Phase E block immediately after. The exact insertion depends on what you saw in Step 1, but the block is:

```typescript
    // Phase E — Nexus batch registration (fire-and-forget, never blocks lot release)
    if (next === "RELEASED" && before.status !== "RELEASED") {
      void (async () => {
        try {
          const { isBatchRegistrationConfigured, registerBatchInNexus } = await import(
            "@/lib/integrations/nexus/batch-registration"
          );
          if (!isBatchRegistrationConfigured()) return;

          // Load lot metadata including product SKU and name
          const [lotMeta] = await db
            .select({
              finishedLotNumber: finishedLots.finishedLotNumber,
              producedOn: finishedLots.producedOn,
              unitsProduced: finishedLots.unitsProduced,
              productSku: products.sku,
              productName: products.name,
            })
            .from(finishedLots)
            .innerJoin(products, eq(products.id, finishedLots.productId))
            .where(eq(finishedLots.id, id));

          if (!lotMeta) return;

          // Build packaging_inputs from finishedLotInputs → batches → packagingMaterials
          const inputs = await db
            .select({
              materialCode: packagingMaterials.sku,
              materialName: packagingMaterials.name,
              supplierLotNumber: batches.supplierLotNumber,  // null if field absent — handled below
            })
            .from(finishedLotInputs)
            .innerJoin(batches, eq(batches.id, finishedLotInputs.batchId))
            .innerJoin(packagingMaterials, eq(packagingMaterials.id, batches.packagingMaterialId))
            .where(
              and(
                eq(finishedLotInputs.finishedLotId, id),
                eq(batches.kind, "PACKAGING"),
              )
            );

          const result = await registerBatchInNexus({
            lot_number: lotMeta.finishedLotNumber,
            product_sku: lotMeta.productSku,
            product_description: lotMeta.productName ?? "",
            produced_on:
              typeof lotMeta.producedOn === "string"
                ? lotMeta.producedOn
                : (lotMeta.producedOn as Date).toISOString().slice(0, 10),
            units_produced: lotMeta.unitsProduced ?? 0,
            luma_finished_lot_id: id,
            packaging_inputs: inputs.map((i) => ({
              material_code: i.materialCode,
              material_name: i.materialName,
              supplier_lot_number: i.supplierLotNumber ?? "",
            })),
          });

          await db
            .update(finishedLots)
            .set(
              result.ok
                ? { nexusBatchRegisteredAt: new Date(), nexusBatchRegisterError: null }
                : { nexusBatchRegisterError: result.reason },
            )
            .where(eq(finishedLots.id, id));
        } catch (err) {
          console.error("[nexus.batch-registration] fire-and-forget error:", err);
        }
      })();
    }
```

Note: if `batches.supplierLotNumber` does not exist (you confirmed this in Step 2), replace `supplierLotNumber: batches.supplierLotNumber` with a literal empty string in the select and map:

```typescript
              // supplierLotNumber field not present on batches table
              supplierLotNumber: sql<string>`''`,
```

and in the map:

```typescript
              supplier_lot_number: "",
```

- [ ] **Step 5: Verify the file after edit**

```bash
grep -n "Phase E\|nexusBatch\|batch-registration\|registerBatch" /Users/kidevu/luma/lib/db/queries/finished-lots.ts
```

Expected: the Phase E block is present with the correct function names.

- [ ] **Step 6: Full TypeScript check**

```bash
cd /Users/kidevu/luma && npx tsc --noEmit --project tsconfig.json 2>&1 | head -50
```

Expected: zero errors. Fix any type errors before continuing.

Common issues:
- `batches.supplierLotNumber` does not exist → use `sql<string>\`''\`` as shown in Step 4.
- `products.sku` or `products.name` incorrect field name → check schema.ts with `grep -n "sku\|name" /Users/kidevu/luma/lib/db/schema.ts | grep -A2 "products"`.
- `finishedLots.productId` wrong field name → check with `grep -n "productId" /Users/kidevu/luma/lib/db/schema.ts`.

- [ ] **Step 7: Add NEXUS_URL and LUMA_NEXUS_SECRET to Luma's .env on LXC 122**

Check current Luma env file:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "grep -E \"NEXUS_URL|LUMA_NEXUS_SECRET\" /etc/luma/.env 2>/dev/null || echo \"not set\""'
```

If not set, add them (use the same `LUMA_NEXUS_SECRET` value you added to LXC 119 in Task 2):

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "echo \"NEXUS_URL=http://192.168.1.190:8000\" >> /etc/luma/.env && echo \"LUMA_NEXUS_SECRET=<GENERATED_SECRET>\" >> /etc/luma/.env"'
```

Verify:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "grep -E \"NEXUS_URL|LUMA_NEXUS_SECRET\" /etc/luma/.env"'
```

Expected: both lines present with real values.

- [ ] **Step 8: Deploy Luma to LXC 122**

Check how Luma deploys (systemd-timer auto-deploy or manual):

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "systemctl list-units | grep luma"'
```

If a deploy timer exists, trigger it or wait for the next cycle. For a manual deploy:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && git pull && npm run build && systemctl restart luma"'
```

Confirm the service is running:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "systemctl is-active luma"'
```

Expected: `active`.

- [ ] **Step 9: End-to-end smoke test — release a test lot and verify registration**

Find a finished lot in DRAFT or PENDING state (not yet RELEASED):

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && psql \$DATABASE_URL -c \"SELECT id, finished_lot_number, status FROM finished_lots WHERE status != '\''RELEASED'\'' LIMIT 3;\""'
```

Note the `id` of a test lot. Then use the Luma API or UI to release it, or trigger the status change directly via the API:

```bash
# Use the Luma internal API — adjust URL/port as needed
curl -s -X PATCH http://192.168.1.190:3000/api/finished-lots/<LOT_ID>/status \
  -H "Content-Type: application/json" \
  -d '{"status": "RELEASED"}'
```

Wait ~5 seconds (fire-and-forget is async), then check Luma for the registration timestamp:

```bash
ssh root@192.168.1.190 'pct exec 122 -- bash -c "cd /opt/luma && psql \$DATABASE_URL -c \"SELECT id, finished_lot_number, nexus_batch_registered_at, nexus_batch_register_error FROM finished_lots WHERE id = '\''<LOT_ID>'\'';\""'
```

Expected: `nexus_batch_registered_at` is a timestamp (not null), `nexus_batch_register_error` is null.

- [ ] **Step 10: Verify the batch appears in Nexus**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "cd /opt/nexus-resolve && source venv/bin/activate 2>/dev/null || true && python manage.py shell -c \"from apps.crm.models import Batch; b = Batch.objects.order_by('-id').first(); print(b.lot_number, b.manufactured_on, b.metadata)\""'
```

Expected: the lot number from your test lot appears as the most recent batch, with `metadata` containing `luma_finished_lot_id` and `packaging_inputs`.

- [ ] **Step 11: Verify idempotency — release the same lot again (or re-POST)**

Trigger the same lot's release again, or re-run the curl from Task 3 Step 7 with the real lot number. Confirm the Nexus batch count does not increase and the `batch_id` is unchanged.

- [ ] **Step 12: Check Nexus server logs for any errors**

```bash
ssh root@192.168.1.190 'pct exec 119 -- bash -c "journalctl -u nexus --since \"5 minutes ago\" 2>/dev/null || supervisorctl tail nexus 2>/dev/null || tail -50 /var/log/nexus/error.log 2>/dev/null"'
```

Expected: no 500 errors or tracebacks.

- [ ] **Step 13: Commit Luma changes**

```bash
cd /Users/kidevu/luma && git add \
  lib/integrations/nexus/batch-registration.ts \
  lib/db/schema.ts \
  drizzle/0049_nexus_batch_fields.sql \
  lib/db/queries/finished-lots.ts

git commit -m "feat(phase-e): auto-register finished lots in Nexus on RELEASED

- New batch-registration.ts: fire-and-forget POST /api/batches/import
- finishedLots schema: nexusBatchRegisteredAt, nexusBatchRegisterError
- Migration 0049: two new columns
- setFinishedLotStatus: Phase E block alongside Phase A (PackTrack) and Phase B (Zoho)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
