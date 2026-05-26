# Phase D: Logistics Forecasting Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the logistics team one page — `/inventory/forecast` — that answers "what do I need to order, how much, and by when?" using real consumption and sales velocity data, no manual estimates.

**Architecture:** New `packtrack/services/forecast.py` computes a `ForecastRow` per material from `sales_events` (Phase C), `material_consumption_events` (Phase A), and a BOM fetched from Luma's `GET /api/internal/product-packaging-specs` (Phase C). New `packtrack/routes/forecast.py` serves the page. New Jinja2 template `forecast.html` renders four panels. Telegram alert fires when `reorder_by_sea` enters the 7-day window, deduplicated per restock cycle.

**Pre-conditions:**
- Phase A must be live (`material_consumption_events` populated)
- Phase C must be live (`sales_events` populated + Luma BOM API live)
- `LUMA_URL` added to PackTrack `.env` (Luma base URL, e.g. `http://192.168.1.134:3000`)
- `LUMA_PACKTRACK_SECRET` already exists — used to authenticate the BOM API call

**Tech Stack:** FastAPI · SQLModel · Jinja2 · HTMX · Tailwind (via base.html) · httpx · Python 3.13

---

## File map

| Action | Path | Responsibility |
|---|---|---|
| Read first | `packtrack/routes/inventory.py` | Understand route + template pattern |
| Read first | `packtrack/templates/inventory.html` | Understand Jinja2 template pattern |
| Read first | `packtrack/config.py` | Understand how env vars are declared |
| Create | `packtrack/services/forecast.py` | ForecastRow dataclass + compute_forecast() |
| Create | `packtrack/routes/forecast.py` | GET /inventory/forecast |
| Create | `packtrack/templates/forecast.html` | 4-panel Jinja2 template |
| Create | `packtrack/templates/_partials/forecast_row.html` | Single table row partial (HTMX drill-down) |
| Modify | `packtrack/config.py` | Add LUMA_URL field |
| Modify | `packtrack/main.py` | Import + mount forecast router; add nav link |
| Modify | `packtrack/templates/base.html` | Add "Forecast" nav link |
| Modify | `packtrack/notifications.py` | Add stock.forecast_urgent event routing |

---

### Task 1: Read the existing patterns before writing anything

**Files:** read-only

- [ ] **Read the inventory route to understand the route + template render pattern**
```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -c "cat /opt/packtrack/app/packtrack/routes/inventory.py"'
```

- [ ] **Read the inventory template to understand Jinja2 block structure and Tailwind conventions**
```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -c "cat /opt/packtrack/app/packtrack/templates/inventory.html"'
```

- [ ] **Read config.py to understand how env vars are declared**
```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -c "cat /opt/packtrack/app/packtrack/config.py"'
```

- [ ] **Note the exact**: router pattern, `templates.TemplateResponse(request, "...", {...})` signature, how `require_user` and `get_session` are used as Depends.

---

### Task 2: Add LUMA_URL to PackTrack config

**Files:**
- Modify: `packtrack/config.py`

- [ ] **Add `LUMA_URL` field to the `Settings` class** after `LUMA_PACKTRACK_SECRET`:

```python
# In packtrack/config.py, inside class Settings(BaseSettings):
LUMA_URL: str = ""          # e.g. http://192.168.1.134:3000
```

- [ ] **Add `LUMA_URL` to `/etc/packtrack/.env` on LXC 200**
```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -c "echo \"LUMA_URL=http://192.168.1.134:3000\" >> /etc/packtrack/.env"'
# Verify it's there:
ssh root@192.168.1.190 'pct exec 200 -- bash -c "grep LUMA_URL /etc/packtrack/.env"'
```

- [ ] **Commit**
```bash
cd /opt/packtrack/app  # or wherever local dev is: /Users/kidevu/packtrack-v2
git add packtrack/config.py
git commit -m "feat(phase-d): add LUMA_URL to Settings"
```

---

### Task 3: Forecast service

**Files:**
- Create: `packtrack/services/forecast.py`

This module fetches the BOM from Luma (cached 1 hour), reads `sales_events` and `material_consumption_events` from the local DB, and returns a sorted list of `ForecastRow` objects.

- [ ] **Create `packtrack/services/forecast.py`**

```python
"""
Phase D — Logistics forecasting service.

compute_forecast(session) -> list[ForecastRow]

Returns one ForecastRow per Item that has a material_code, sorted:
  1. reorder_by_sea ASC (most urgent first)
  2. Items with no sales velocity at the end (days_of_stock = ∞)

Cached: BOM fetch from Luma is cached for 1 hour (module-level dict).
Never raises — logs warnings and skips unmapped items.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from sqlmodel import Session, func, select

from packtrack.config import settings
from packtrack.models import Item, MaterialConsumptionEvent, SalesEvent

logger = logging.getLogger("packtrack.forecast")

# ---------------------------------------------------------------------------
# BOM cache — {product_sku: {material_code: qty_per_unit}}, refreshed every hour
# ---------------------------------------------------------------------------

_BOM_CACHE: dict[str, dict[str, float]] = {}
_BOM_FETCHED_AT: float = 0.0
_BOM_TTL: float = 3600.0  # seconds


def _fetch_bom() -> dict[str, dict[str, float]]:
    """Fetch BOM from Luma. Returns {} on failure (logged)."""
    if not settings.LUMA_URL or not settings.LUMA_PACKTRACK_SECRET:
        logger.warning("LUMA_URL or LUMA_PACKTRACK_SECRET not configured — forecast BOM empty")
        return {}
    url = f"{settings.LUMA_URL.rstrip('/')}/api/internal/product-packaging-specs"
    try:
        resp = httpx.get(
            url,
            headers={"X-Luma-PackTrack-Secret": settings.LUMA_PACKTRACK_SECRET},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        bom: dict[str, dict[str, float]] = {}
        for entry in data:
            sku = entry.get("product_sku", "")
            comps: dict[str, float] = {}
            for c in entry.get("components", []):
                mc = c.get("material_code", "")
                qty = float(c.get("qty_per_unit", 0))
                if mc and qty > 0:
                    comps[mc] = qty
            if sku and comps:
                bom[sku] = comps
        return bom
    except Exception:
        logger.exception("Failed to fetch BOM from Luma at %s", url)
        return {}


def _get_bom() -> dict[str, dict[str, float]]:
    global _BOM_CACHE, _BOM_FETCHED_AT
    now = time.monotonic()
    if now - _BOM_FETCHED_AT > _BOM_TTL:
        _BOM_CACHE = _fetch_bom()
        _BOM_FETCHED_AT = now
    return _BOM_CACHE


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class ForecastRow:
    item: Item
    daily_demand: float          # units/day from sales velocity × BOM
    days_of_stock: float         # current_stock / daily_demand (inf if demand == 0)
    reorder_by_sea: date | None  # today + days_of_stock - sea_lead_days (None if inf)
    suggested_qty: float         # (sea_lead_days + 30) × daily_demand - current_stock
    panel: str                   # "order_now" | "watch" | "ok" | "no_velocity"
    sales_drivers: list[tuple[str, float]] = field(default_factory=list)  # [(sku, daily_qty)]


# ---------------------------------------------------------------------------
# Main compute function
# ---------------------------------------------------------------------------

def compute_forecast(session: Session) -> list[ForecastRow]:
    today = date.today()
    cutoff = datetime.utcnow() - timedelta(days=60)

    # 1. Sales velocity per product_sku: avg daily qty, rolling 60 days
    sales_rows = session.exec(
        select(
            SalesEvent.product_sku,
            func.coalesce(func.sum(SalesEvent.qty_sold), 0).label("total_sold"),
        )
        .where(SalesEvent.sold_at >= cutoff)
        .group_by(SalesEvent.product_sku)
    ).all()
    sales_velocity: dict[str, float] = {
        row.product_sku: float(row.total_sold) / 60.0
        for row in sales_rows
    }

    # 2. BOM from Luma (cached)
    bom = _get_bom()

    # 3. Compute daily_demand per material_code
    #    daily_demand[mc] = Σ (sales_velocity[P] × bom[P][mc]) for all products P
    daily_demand_by_code: dict[str, float] = {}
    sales_drivers_by_code: dict[str, list[tuple[str, float]]] = {}
    for product_sku, velocity in sales_velocity.items():
        if velocity <= 0:
            continue
        for mc, qty_per_unit in bom.get(product_sku, {}).items():
            contribution = velocity * qty_per_unit
            daily_demand_by_code[mc] = daily_demand_by_code.get(mc, 0.0) + contribution
            if mc not in sales_drivers_by_code:
                sales_drivers_by_code[mc] = []
            sales_drivers_by_code[mc].append((product_sku, round(contribution, 2)))

    # 4. Load all items with material_code (PackTrack packaging items)
    items = session.exec(
        select(Item).where(Item.material_code.is_not(None)).order_by(Item.name)
    ).all()

    # 5. Build ForecastRow per item
    rows: list[ForecastRow] = []
    for item in items:
        mc = item.material_code
        demand = daily_demand_by_code.get(mc, 0.0)
        stock = float(item.current_stock)

        if demand <= 0:
            rows.append(ForecastRow(
                item=item,
                daily_demand=0.0,
                days_of_stock=float("inf"),
                reorder_by_sea=None,
                suggested_qty=0.0,
                panel="no_velocity",
                sales_drivers=[],
            ))
            continue

        days_of_stock = stock / demand
        reorder_by_sea = today + timedelta(days=days_of_stock) - timedelta(days=item.sea_lead_days)
        suggested_qty = max(0.0, (item.sea_lead_days + 30) * demand - stock)
        days_until_reorder = (reorder_by_sea - today).days

        if days_until_reorder <= 7:
            panel = "order_now"
        elif days_until_reorder <= 30:
            panel = "watch"
        else:
            panel = "ok"

        rows.append(ForecastRow(
            item=item,
            daily_demand=round(demand, 2),
            days_of_stock=round(days_of_stock, 1),
            reorder_by_sea=reorder_by_sea,
            suggested_qty=round(suggested_qty),
            panel=panel,
            sales_drivers=sorted(
                sales_drivers_by_code.get(mc, []),
                key=lambda x: x[1],
                reverse=True,
            ),
        ))

    # Sort: order_now first, then watch, then ok, then no_velocity; within each group by reorder_by_sea
    panel_order = {"order_now": 0, "watch": 1, "ok": 2, "no_velocity": 3}
    rows.sort(key=lambda r: (
        panel_order[r.panel],
        r.reorder_by_sea or date(9999, 12, 31),
    ))

    return rows
```

- [ ] **Commit**
```bash
git add packtrack/services/forecast.py
git commit -m "feat(phase-d): add forecast service with BOM-based demand computation"
```

---

### Task 4: Forecast route

**Files:**
- Create: `packtrack/routes/forecast.py`

- [ ] **Create `packtrack/routes/forecast.py`**

```python
"""Phase D — /inventory/forecast page."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlmodel import Session

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import User
from packtrack.services.forecast import compute_forecast

router = APIRouter()


@router.get("/inventory/forecast", response_class=HTMLResponse)
def inventory_forecast(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rows = compute_forecast(session)
    order_now = [r for r in rows if r.panel == "order_now"]
    watch = [r for r in rows if r.panel == "watch"]
    ok = [r for r in rows if r.panel == "ok"]
    no_velocity = [r for r in rows if r.panel == "no_velocity"]
    from packtrack.main import templates
    return templates.TemplateResponse(
        request,
        "forecast.html",
        {
            "user": user,
            "order_now": order_now,
            "watch": watch,
            "ok": ok,
            "no_velocity": no_velocity,
            "all_rows": rows,
        },
    )
```

- [ ] **Mount the router in `packtrack/main.py`**

Add `forecast` to the import and `include_router` lines:

```python
# In packtrack/main.py, modify the import line:
from packtrack.routes import admin, auth, forecast, inbox, inventory, purchase_orders, receiving, search, telegram_webhook

# Add after telegram_webhook.router:
app.include_router(forecast.router)
```

- [ ] **Commit**
```bash
git add packtrack/routes/forecast.py packtrack/main.py
git commit -m "feat(phase-d): add /inventory/forecast route and mount router"
```

---

### Task 5: Forecast template

**Files:**
- Create: `packtrack/templates/forecast.html`
- Create: `packtrack/templates/_partials/forecast_row.html`

- [ ] **Create `packtrack/templates/forecast.html`**

```html
{% extends "base.html" %}
{% block title %}Forecast · PackTrack{% endblock %}
{% block container_class %}max-w-6xl{% endblock %}
{% block content %}

<div class="flex items-end justify-between gap-4 mb-6">
  <div>
    <h1 class="text-2xl font-semibold">Inventory Forecast</h1>
    <p class="text-sm text-stone-500 mt-1">Based on 60-day sales velocity × BOM. Sea lead times applied.</p>
  </div>
  <a href="/inventory" class="text-sm text-stone-500 hover:text-stone-900">← Back to Inventory</a>
</div>

{% if order_now %}
<section class="mb-6">
  <div class="flex items-center gap-2 mb-3">
    <span class="size-2.5 rounded-full bg-red-500 shrink-0"></span>
    <h2 class="font-semibold text-red-700">Order Now ({{ order_now|length }})</h2>
    <span class="text-xs text-red-500">Reorder date within 7 days</span>
  </div>
  <div class="bg-white border border-red-200 rounded-xl overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-red-50 text-red-800 text-xs uppercase tracking-wide">
        <tr>
          <th class="px-4 py-2.5 text-left">Material</th>
          <th class="px-4 py-2.5 text-right">On Hand</th>
          <th class="px-4 py-2.5 text-right">Days Left</th>
          <th class="px-4 py-2.5 text-right">Reorder By</th>
          <th class="px-4 py-2.5 text-right">Suggested Qty</th>
          <th class="px-4 py-2.5 text-right">Action</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-red-100">
        {% for row in order_now %}
        <tr class="hover:bg-red-50">
          <td class="px-4 py-3 font-medium">
            {{ row.item.name }}
            <div class="text-xs text-stone-400">{{ row.item.material_code }}</div>
          </td>
          <td class="px-4 py-3 text-right text-stone-700">{{ row.item.current_stock | int | format_thousands }}</td>
          <td class="px-4 py-3 text-right font-semibold text-red-600">{{ row.days_of_stock | int }}</td>
          <td class="px-4 py-3 text-right text-red-600 font-medium">{{ row.reorder_by_sea.strftime('%b %d') if row.reorder_by_sea else '—' }}</td>
          <td class="px-4 py-3 text-right text-stone-700">{{ row.suggested_qty | int | format_thousands }} {{ row.item.unit }}</td>
          <td class="px-4 py-3 text-right">
            <a href="/po/new?item_id={{ row.item.id }}&suggested_qty={{ row.suggested_qty | int }}"
               class="inline-flex items-center gap-1 rounded-lg bg-red-600 text-white px-3 py-1.5 text-xs font-medium hover:bg-red-700">
              Order →
            </a>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endif %}

{% if watch %}
<section class="mb-6">
  <div class="flex items-center gap-2 mb-3">
    <span class="size-2.5 rounded-full bg-amber-400 shrink-0"></span>
    <h2 class="font-semibold text-amber-700">Watch ({{ watch|length }})</h2>
    <span class="text-xs text-amber-600">Reorder date within 30 days</span>
  </div>
  <div class="bg-white border border-amber-200 rounded-xl overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-amber-50 text-amber-800 text-xs uppercase tracking-wide">
        <tr>
          <th class="px-4 py-2.5 text-left">Material</th>
          <th class="px-4 py-2.5 text-right">On Hand</th>
          <th class="px-4 py-2.5 text-right">Days Left</th>
          <th class="px-4 py-2.5 text-right">Reorder By</th>
          <th class="px-4 py-2.5 text-right">Suggested Qty</th>
          <th class="px-4 py-2.5 text-right">Action</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-amber-100">
        {% for row in watch %}
        <tr class="hover:bg-amber-50">
          <td class="px-4 py-3 font-medium">
            {{ row.item.name }}
            <div class="text-xs text-stone-400">{{ row.item.material_code }}</div>
          </td>
          <td class="px-4 py-3 text-right text-stone-700">{{ row.item.current_stock | int | format_thousands }}</td>
          <td class="px-4 py-3 text-right text-amber-600">{{ row.days_of_stock | int }}</td>
          <td class="px-4 py-3 text-right text-amber-700 font-medium">{{ row.reorder_by_sea.strftime('%b %d') if row.reorder_by_sea else '—' }}</td>
          <td class="px-4 py-3 text-right text-stone-700">{{ row.suggested_qty | int | format_thousands }} {{ row.item.unit }}</td>
          <td class="px-4 py-3 text-right">
            <a href="/po/new?item_id={{ row.item.id }}&suggested_qty={{ row.suggested_qty | int }}"
               class="inline-flex items-center gap-1 rounded-lg border border-amber-400 text-amber-700 px-3 py-1.5 text-xs font-medium hover:bg-amber-50">
              Plan PO →
            </a>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endif %}

<section class="mb-6">
  <div class="flex items-center gap-2 mb-3">
    <span class="size-2.5 rounded-full bg-stone-300 shrink-0"></span>
    <h2 class="font-semibold text-stone-700">Full Forecast ({{ all_rows|length }} materials)</h2>
  </div>
  <div class="bg-white border border-stone-200 rounded-xl overflow-hidden">
    <table class="w-full text-sm">
      <thead class="bg-stone-50 text-stone-600 text-xs uppercase tracking-wide">
        <tr>
          <th class="px-4 py-2.5 text-left">Material</th>
          <th class="px-4 py-2.5 text-right">On Hand</th>
          <th class="px-4 py-2.5 text-right">Daily Demand</th>
          <th class="px-4 py-2.5 text-right">Days of Stock</th>
          <th class="px-4 py-2.5 text-right">Reorder By (Sea)</th>
          <th class="px-4 py-2.5 text-right">Suggested Qty</th>
          <th class="px-4 py-2.5 text-right">Status</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-stone-100">
        {% for row in all_rows %}
        <tr class="hover:bg-stone-50 cursor-pointer"
            hx-get="/inventory/forecast/detail/{{ row.item.id }}"
            hx-target="#detail-panel"
            hx-swap="innerHTML">
          <td class="px-4 py-3 font-medium">
            {{ row.item.name }}
            <div class="text-xs text-stone-400">{{ row.item.material_code }}</div>
          </td>
          <td class="px-4 py-3 text-right">{{ row.item.current_stock | int | format_thousands }}</td>
          <td class="px-4 py-3 text-right text-stone-600">
            {% if row.daily_demand > 0 %}{{ row.daily_demand }}/day{% else %}<span class="text-stone-400">no data</span>{% endif %}
          </td>
          <td class="px-4 py-3 text-right
            {% if row.panel == 'order_now' %}text-red-600 font-semibold
            {% elif row.panel == 'watch' %}text-amber-600
            {% else %}text-stone-700{% endif %}">
            {% if row.days_of_stock == row.days_of_stock and row.days_of_stock < 9999 %}{{ row.days_of_stock | int }}
            {% else %}&infin;{% endif %}
          </td>
          <td class="px-4 py-3 text-right
            {% if row.panel == 'order_now' %}text-red-600 font-medium
            {% elif row.panel == 'watch' %}text-amber-700
            {% else %}text-stone-500{% endif %}">
            {{ row.reorder_by_sea.strftime('%b %d, %Y') if row.reorder_by_sea else '—' }}
          </td>
          <td class="px-4 py-3 text-right text-stone-600">
            {% if row.suggested_qty > 0 %}{{ row.suggested_qty | int | format_thousands }} {{ row.item.unit }}{% else %}—{% endif %}
          </td>
          <td class="px-4 py-3 text-right">
            {% if row.panel == 'order_now' %}
              <span class="rounded-full bg-red-100 text-red-700 px-2 py-0.5 text-xs font-medium">Order Now</span>
            {% elif row.panel == 'watch' %}
              <span class="rounded-full bg-amber-100 text-amber-700 px-2 py-0.5 text-xs font-medium">Watch</span>
            {% elif row.panel == 'ok' %}
              <span class="rounded-full bg-green-100 text-green-700 px-2 py-0.5 text-xs font-medium">OK</span>
            {% else %}
              <span class="rounded-full bg-stone-100 text-stone-500 px-2 py-0.5 text-xs font-medium">No Data</span>
            {% endif %}
          </td>
        </tr>
        {% else %}
        <tr>
          <td colspan="7" class="px-4 py-8 text-center text-sm text-stone-400">
            No packaging materials with material_code found. Ensure Phase A is live and materials have been consumed.
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>

<div id="detail-panel" class="mb-6"></div>

{% endblock %}
```

- [ ] **Register the `format_thousands` Jinja2 filter in `packtrack/main.py`**

The template uses `| format_thousands`. Add this filter alongside the existing `_qty` and `_money` filters:

```python
# In packtrack/main.py, in the create_app function or at module level after templates is defined:
def _format_thousands(value) -> str:
    """Render an integer with thousands separators."""
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)

templates.env.filters["format_thousands"] = _format_thousands
```

- [ ] **Create `packtrack/templates/_partials/forecast_row.html`** (HTMX drill-down detail panel)

```html
<div class="bg-white border border-stone-200 rounded-xl p-4">
  <div class="flex items-start justify-between mb-3">
    <div>
      <h3 class="font-semibold">{{ row.item.name }}</h3>
      <div class="text-xs text-stone-400">{{ row.item.material_code }} · {{ row.item.unit }} · Sea: {{ row.item.sea_lead_days }}d / Express: {{ row.item.express_lead_days }}d</div>
    </div>
    <a href="/po/new?item_id={{ row.item.id }}&suggested_qty={{ row.suggested_qty | int }}"
       class="text-sm text-blue-600 hover:underline">Create PO →</a>
  </div>

  <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4 text-sm">
    <div class="bg-stone-50 rounded-lg p-3">
      <div class="text-xs text-stone-500 mb-1">On Hand</div>
      <div class="font-semibold">{{ row.item.current_stock | int | format_thousands }} {{ row.item.unit }}</div>
    </div>
    <div class="bg-stone-50 rounded-lg p-3">
      <div class="text-xs text-stone-500 mb-1">Daily Demand</div>
      <div class="font-semibold">{{ row.daily_demand }}/day</div>
    </div>
    <div class="bg-stone-50 rounded-lg p-3">
      <div class="text-xs text-stone-500 mb-1">Days of Stock</div>
      <div class="font-semibold {% if row.panel == 'order_now' %}text-red-600{% elif row.panel == 'watch' %}text-amber-600{% endif %}">
        {% if row.days_of_stock < 9999 %}{{ row.days_of_stock | int }}{% else %}&infin;{% endif %}
      </div>
    </div>
    <div class="bg-stone-50 rounded-lg p-3">
      <div class="text-xs text-stone-500 mb-1">Reorder By (Sea)</div>
      <div class="font-semibold {% if row.panel == 'order_now' %}text-red-600{% elif row.panel == 'watch' %}text-amber-600{% endif %}">
        {{ row.reorder_by_sea.strftime('%b %d, %Y') if row.reorder_by_sea else '—' }}
      </div>
    </div>
  </div>

  {% if row.sales_drivers %}
  <div class="text-xs text-stone-500 mb-2">Sales drivers (60-day velocity):</div>
  <ul class="flex flex-wrap gap-2">
    {% for sku, qty in row.sales_drivers %}
    <li class="rounded-md bg-stone-100 px-2 py-1 text-xs">
      <span class="font-medium">{{ sku }}</span>
      <span class="text-stone-500">{{ qty }}/day</span>
    </li>
    {% endfor %}
  </ul>
  {% endif %}
</div>
```

- [ ] **Add a drill-down route to `packtrack/routes/forecast.py`**

```python
@router.get("/inventory/forecast/detail/{item_id}", response_class=HTMLResponse)
def forecast_detail(
    item_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    rows = compute_forecast(session)
    row = next((r for r in rows if r.item.id == item_id), None)
    if row is None:
        return HTMLResponse("<p class='text-sm text-stone-400 px-4 py-3'>Item not found in forecast.</p>")
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "_partials/forecast_row.html", {"row": row, "user": user}
    )
```

- [ ] **Add "Forecast" link to the nav in `packtrack/templates/base.html`**

Find the nav section that contains `<a href="/inventory"` and add after it:
```html
<a href="/inventory/forecast" class="text-stone-600 hover:text-stone-900 hidden sm:inline">Forecast</a>
```

- [ ] **Commit**
```bash
git add packtrack/templates/forecast.html packtrack/templates/_partials/forecast_row.html packtrack/routes/forecast.py packtrack/templates/base.html packtrack/main.py
git commit -m "feat(phase-d): add forecast template, drill-down partial, nav link, and format_thousands filter"
```

---

### Task 6: Telegram alert for 7-day window

**Files:**
- Modify: `packtrack/notifications.py`
- Modify: `packtrack/routes/forecast.py`

The Telegram alert fires when `reorder_by_sea` enters the 7-day window. It must not repeat until `current_stock` rises back above `reorder_point` (i.e., only one alert per restock cycle).

The deduplication key uses a simple approach: store the last-alerted `current_stock` level as a `forecast_alert_sent_stock` float on `Item`. If the alert was already sent at this approximate stock level, skip it.

- [ ] **Add `forecast_alert_sent_stock` field to `Item` model in `packtrack/models.py`**

```python
# In packtrack/models.py, in the Item class, after daily_usage_rate:
forecast_alert_sent_stock: float | None = Field(default=None)
```

- [ ] **Create an Alembic migration for the new field**

The migration file goes in `migrations/versions/`. It depends on the Phase C migration (`e3f4a5b6c7d8`):

```python
# File: migrations/versions/f4a5b6c7d8e9_forecast_alert_sent_stock.py
"""Add forecast_alert_sent_stock to item

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = "f4a5b6c7d8e9"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "item",
        sa.Column("forecast_alert_sent_stock", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("item", "forecast_alert_sent_stock")
```

- [ ] **Apply the migration**
```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -c "cd /opt/packtrack/app && .venv/bin/alembic upgrade head"'
```
Expected: `Running upgrade e3f4a5b6c7d8 -> f4a5b6c7d8e9, Add forecast_alert_sent_stock to item`

- [ ] **Add `stock.forecast_urgent` event to `_ROUTING` in `packtrack/notifications.py`**

```python
# In packtrack/notifications.py, add to _ROUTING dict:
"stock.forecast_urgent": (Role.OWNER,),
```

- [ ] **Add `notify_forecast_alert` function to `packtrack/notifications.py`**

```python
def notify_forecast_alert(session: Session, row: "ForecastRow") -> None:
    """Fire once per restock cycle when reorder_by_sea enters the 7-day window."""
    from packtrack.services.forecast import ForecastRow  # avoid circular at module level
    item = row.item

    # Deduplication: if current_stock hasn't risen since last alert, skip.
    # "risen" = current_stock > last_alerted_stock + 10% of reorder_point (hysteresis)
    if item.forecast_alert_sent_stock is not None:
        hysteresis = max(1.0, item.reorder_point * 0.10)
        if item.current_stock <= item.forecast_alert_sent_stock + hysteresis:
            return  # already alerted this restock cycle

    recipients = list(
        session.exec(
            select(User).where(
                User.role.in_([Role.OWNER]),
                User.is_active == True,  # noqa: E712
                User.telegram_chat_id.is_not(None),
            )
        )
    )

    days = int(row.days_of_stock) if row.days_of_stock < 9999 else 0
    reorder_str = row.reorder_by_sea.strftime("%b %d") if row.reorder_by_sea else "overdue"
    text = (
        f"🔴 Order Now: {item.name}\n"
        f"On hand: {int(item.current_stock):,} {item.unit}\n"
        f"Days of stock: {days}\n"
        f"Reorder by (sea): {reorder_str}\n"
        f"Suggested: {int(row.suggested_qty):,} {item.unit}\n"
        f"Sea lead: {item.sea_lead_days}d"
    )

    from packtrack.telegram import send
    for user in recipients:
        send(
            chat_id=user.telegram_chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [[
                    {"text": "Create PO", "url": f"{settings.APP_BASE_URL}/po/new?item_id={item.id}&suggested_qty={int(row.suggested_qty)}"}
                ]]
            },
        )

    # Mark sent at current stock level
    item.forecast_alert_sent_stock = item.current_stock
    session.add(item)
    session.commit()
    logger.info("Forecast alert sent for item %s (stock=%s)", item.material_code, item.current_stock)
```

- [ ] **Fire alerts from the forecast route in `packtrack/routes/forecast.py`**

Add after `rows = compute_forecast(session)`:

```python
    # Fire Telegram alerts for order_now items (deduplicated in notify_forecast_alert)
    from packtrack.notifications import notify_forecast_alert
    for r in rows:
        if r.panel == "order_now":
            try:
                notify_forecast_alert(session, r)
            except Exception:
                import logging
                logging.getLogger("packtrack.forecast").exception(
                    "Failed to send forecast alert for %s", r.item.material_code
                )
```

- [ ] **Commit**
```bash
git add packtrack/models.py migrations/versions/f4a5b6c7d8e9_forecast_alert_sent_stock.py packtrack/notifications.py packtrack/routes/forecast.py
git commit -m "feat(phase-d): Telegram forecast alert on 7-day reorder window, deduped per restock cycle"
```

---

### Task 7: Deploy and smoke test

- [ ] **Push to main**
```bash
git push
```

- [ ] **Deploy on LXC 200**
```bash
ssh root@192.168.1.190 'pct exec 200 -- bash -c "cd /opt/packtrack/app && git pull && .venv/bin/alembic upgrade head && systemctl restart packtrack 2>/dev/null || supervisorctl restart packtrack 2>/dev/null"'
```

- [ ] **Verify the route is live**
```bash
curl -s -o /dev/null -w "%{http_code}" http://192.168.1.206:8000/inventory/forecast
```
Expected: `200` (redirect to login) or `302` if not authenticated. Not `500`.

- [ ] **Visit the forecast page in a browser and verify**

Navigate to `http://192.168.1.206:8000/inventory/forecast` (log in as owner).

Expected:
- Page loads without error
- If Phase A and Phase C are live and have data: materials appear with demand, days-of-stock, reorder dates
- If no sales_events or material_consumption_events yet: "No Data" panels — this is correct
- Clicking a row in the full table loads the drill-down panel below

- [ ] **Verify the nav link appears**

The base nav should now show: `Pipeline | Inventory | Forecast | Receiving | Admin`

- [ ] **Bump PackTrack version if applicable**
```bash
# In packtrack/__init__.py or wherever __version__ is set — increment minor version
grep -n "__version__" /opt/packtrack/app/packtrack/__init__.py
# Then update accordingly
```

- [ ] **Final commit**
```bash
git add packtrack/__init__.py  # if version bumped
git commit -m "feat(phase-d): deploy logistics forecasting dashboard — complete"
git push
```
