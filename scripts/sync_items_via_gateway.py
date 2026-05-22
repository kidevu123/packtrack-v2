"""Pull packaging items from Zoho via the existing Zoho Integration Service.

Why this script and not the existing ``packtrack/zoho.py``?

PackTrack's local Zoho client expects ``ZOHO_CLIENT_ID``, ``ZOHO_CLIENT_SECRET``,
``ZOHO_REFRESH_TOKEN``, and ``ZOHO_ORG_ID`` in the environment, hits Zoho
directly, and runs full OAuth there. As of P1.5 those vars are blank in
``/etc/packtrack/packtrack.env`` — the operator chose to consolidate Zoho
access through the dedicated **Zoho Integration Service** at
``ZOHO_GATEWAY_URL`` (LXC 9503, 192.168.1.205:8000).

This script reads from that gateway. It uses HTTP + a service token, never
holds Zoho OAuth state, and never writes to Zoho. The existing
``packtrack/zoho.py`` is left alone so the boundary between "gateway-managed
Zoho access" and "PackTrack's old direct path" stays explicit until P8 does
the full migration.

Filter: ``cf_item_type == "Packaging"``. PackTrack v2 is a packaging-only
app; pulling every Zoho inventory item would flood the table with non-
packaging SKUs.

Idempotent: keys on ``zoho_item_id``. Re-running updates in place; never
duplicates.

Usage:

    sudo -u packtrack bash -lc 'cd /opt/packtrack/app && . .venv/bin/activate \\
        && set -a && source /etc/packtrack/packtrack.env && set +a \\
        && python scripts/sync_items_via_gateway.py'

    # Dry-run (count + sample, no DB writes)
    python scripts/sync_items_via_gateway.py --dry-run

    # Restrict to a different cf_item_type tag
    python scripts/sync_items_via_gateway.py --filter-type 'Packaging'
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import httpx
from sqlmodel import Session, select

from packtrack.db import engine
from packtrack.models import Item, SyncRun

# ---- Gateway config -----------------------------------------------------


def _gateway_config() -> tuple[str, str, str]:
    """Return ``(base_url, token, brand)`` from env, raising if any missing."""
    url = os.environ.get("ZOHO_GATEWAY_URL", "").strip()
    token = os.environ.get("ZOHO_GATEWAY_TOKEN", "").strip()
    brand = os.environ.get("ZOHO_GATEWAY_BRAND", "").strip()
    missing = [k for k, v in {
        "ZOHO_GATEWAY_URL": url,
        "ZOHO_GATEWAY_TOKEN": token,
        "ZOHO_GATEWAY_BRAND": brand,
    }.items() if not v]
    if missing:
        raise SystemExit(
            "Missing required env: " + ", ".join(missing)
            + "\nAdd them to /etc/packtrack/packtrack.env."
        )
    return url.rstrip("/"), token, brand


def _headers(token: str, brand: str) -> dict:
    return {
        "X-Brand": brand,
        "X-Internal-Token": token,
        "Accept": "application/json",
    }


# ---- Pull ----------------------------------------------------------------


def fetch_items(
    base_url: str, token: str, brand: str, filter_type: str | None,
) -> list[dict]:
    """Pull all pages of items, optionally filtering by ``cf_item_type``.

    The gateway proxies Zoho Inventory's ``/items`` endpoint via
    ``/zoho/items/list``. Pagination is Zoho-style: ``page_context.has_more_page``.
    """
    out: list[dict] = []
    page = 1
    with httpx.Client(timeout=60.0) as client:
        while True:
            r = client.get(
                f"{base_url}/zoho/items/list",
                headers=_headers(token, brand),
                params={"page": page, "per_page": 200},
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") or payload  # gateway wraps under data
            batch = data.get("items") or []
            for item in batch:
                if filter_type and (item.get("cf_item_type") or "") != filter_type:
                    continue
                out.append(item)
            ctx = data.get("page_context") or {}
            if not ctx.get("has_more_page"):
                break
            page += 1
            if page > 100:  # safety brake
                print(
                    "Stopping pagination at page 100 — investigate if you have "
                    "more than 20,000 items.", file=sys.stderr,
                )
                break
    return out


# ---- Map + upsert -------------------------------------------------------


def _f(value, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _vendor_from(item: dict) -> str | None:
    v = (item.get("vendor_name") or "").strip()
    if v:
        return v
    pv = item.get("preferred_vendors")
    if isinstance(pv, list) and pv and isinstance(pv[0], dict):
        return (pv[0].get("vendor_name") or "").strip() or None
    return None


def _apply(item_row: Item, payload: dict) -> None:
    """Map a Zoho-Inventory item dict onto an Item row. Conservative: never
    overwrites ``material_code`` (P1 owner-controlled column) or
    ``reorder_point`` when ``reorder_point_locked`` is set."""
    item_row.name = (payload.get("name") or "")[:240]
    sku = (payload.get("sku") or "").strip()
    item_row.sku_code = sku[:120] if sku else None
    item_row.vendor = (_vendor_from(payload) or "")[:200] or None
    item_row.unit = (payload.get("unit") or "units")[:40]
    item_row.description = (payload.get("description") or "")[:50000] or None
    item_row.current_stock = _f(payload.get("actual_available_stock"))
    rl = payload.get("reorder_level")
    if rl not in (None, "") and not item_row.reorder_point_locked:
        item_row.reorder_point = _f(rl)
    item_row.last_synced_at = datetime.utcnow()
    # material_code is intentionally NOT touched. Owner sets it via the
    # P1 audit script. Re-pulling Zoho must never silently rename a
    # Luma-mapped material.


def upsert(session: Session, items: list[dict]) -> tuple[int, int]:
    updated = created = 0
    for raw in items:
        zid = str(raw.get("item_id") or "")
        if not zid:
            continue
        row = session.exec(select(Item).where(Item.zoho_item_id == zid)).first()
        if row is None:
            row = Item(zoho_item_id=zid, name=(raw.get("name") or "")[:240])
            session.add(row)
            session.flush()
            _apply(row, raw)
            created += 1
        else:
            _apply(row, raw)
            updated += 1
    session.commit()
    return updated, created


# ---- CLI ----------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--filter-type", default="Packaging",
        help="Match Zoho cf_item_type. Empty string = no filter (pull all items).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Pull + report counts, no DB writes.",
    )
    args = ap.parse_args()

    base, token, brand = _gateway_config()
    filter_type = args.filter_type or None

    print(f"Gateway: {base}")
    print(f"Brand:   {brand}")
    if filter_type:
        print(f"Filter:  cf_item_type == {filter_type!r}")
    else:
        print("Filter:  none (all inventory items)")
    print()

    items = fetch_items(base, token, brand, filter_type)
    print(f"Pulled {len(items)} item(s) from gateway.")
    if not items:
        print("Nothing to import. Exiting.")
        return 0

    sample = items[:5]
    print(f"Sample of first {len(sample)}:")
    for it in sample:
        print(f"  zoho_item_id={it.get('item_id')} "
              f"name={(it.get('name') or '')!r} "
              f"sku={(it.get('sku') or '')!r} "
              f"cf_item_type={it.get('cf_item_type')!r} "
              f"vendor_name={(it.get('vendor_name') or '')!r}")
    print()

    if args.dry_run:
        print("Dry run — no DB writes.")
        return 0

    with Session(engine) as session:
        run = SyncRun(started_at=datetime.utcnow())
        session.add(run)
        session.commit()
        session.refresh(run)
        try:
            updated, created = upsert(session, items)
            run.items_updated = updated
            run.items_created = created
            run.status = "ok"
        except Exception as e:
            run.status = "error"
            run.error_message = str(e)[:1000]
            run.finished_at = datetime.utcnow()
            session.add(run)
            session.commit()
            raise
        run.finished_at = datetime.utcnow()
        session.add(run)
        session.commit()

    print(f"Done. {created} created, {updated} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
