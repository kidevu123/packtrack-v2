"""Audit PackTrack items for Luma-registration readiness.

Read-only. Reports — for items with a material_code:

* total
* count with zoho_item_id
* count missing zoho_item_id
* problematic rows (missing zoho_item_id, missing material_code, etc.)

Use this before/after a backfill run to verify the gap closed.

Pair this with the SQL below on the Luma side to verify finished-lot
Zoho assembly plans no longer surface "Missing Zoho item ID":

    -- Luma: packaging_materials missing zoho_item_id
    SELECT sku, name, kind, zoho_item_id
    FROM packaging_materials
    WHERE is_active = true
      AND (zoho_item_id IS NULL OR trim(zoho_item_id) = '')
    ORDER BY name;

    -- Luma: BOM materials missing zoho_item_id
    SELECT p.name AS product_name, pm.sku AS material_code,
           pm.name AS material_name, pm.kind, pm.zoho_item_id
    FROM product_packaging_specs pps
    JOIN products p ON p.id = pps.product_id
    JOIN packaging_materials pm ON pm.id = pps.packaging_material_id
    WHERE pm.is_active = true
      AND (pm.zoho_item_id IS NULL OR trim(pm.zoho_item_id) = '')
    ORDER BY p.name, pm.name;
"""
from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, select

from packtrack.db import engine
from packtrack.models import Item


def _is_blank(v: str | None) -> bool:
    return v is None or v.strip() == ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--show-rows", action="store_true",
                        help="Print the problematic rows in detail.")
    parser.add_argument("--limit", type=int, default=200,
                        help="Cap row listings (default 200).")
    args = parser.parse_args()

    with Session(engine) as session:
        items = session.exec(select(Item).order_by(Item.id)).all()

    total = len(items)
    with_code = [it for it in items if not _is_blank(it.material_code)]
    without_code = [it for it in items if _is_blank(it.material_code)]
    with_both = [it for it in with_code if not _is_blank(it.zoho_item_id)]
    missing_zoho = [it for it in with_code if _is_blank(it.zoho_item_id)]
    missing_zoho_with_sku = [it for it in missing_zoho if not _is_blank(it.sku_code)]
    has_zoho_no_code = [it for it in items
                        if _is_blank(it.material_code) and not _is_blank(it.zoho_item_id)]

    print("PackTrack item readiness for Luma registration")
    print("-" * 56)
    print(f"Total items:                              {total}")
    print(f"  with material_code:                     {len(with_code)}")
    print(f"  with material_code AND zoho_item_id:    {len(with_both)} (eligible for Luma)")
    print(f"  with material_code, missing zoho_item_id:{len(missing_zoho)}")
    print(f"  with zoho_item_id, missing material_code:{len(has_zoho_no_code)}")
    print(f"  missing material_code entirely:         {len(without_code)}")

    print()
    print("Eligible for `backfill_luma_packaging_material_zoho_ids.py --apply`:")
    print(f"  {len(with_both)} item(s)")

    if missing_zoho:
        print()
        print(f"⚠ Items with material_code but NO zoho_item_id ({len(missing_zoho)} total):")
        print(f"  ({len(missing_zoho_with_sku)} of these have a sku_code — possibly fixable by re-syncing Zoho items)")
        if args.show_rows:
            for it in missing_zoho[:args.limit]:
                print(f"    #{it.id:<6} {it.material_code:<14} sku={it.sku_code or '-':<14} {it.name[:60]}")
            if len(missing_zoho) > args.limit:
                print(f"    … {len(missing_zoho) - args.limit} more (raise --limit to see)")
    if has_zoho_no_code:
        print()
        print(f"⚠ Items with zoho_item_id but NO material_code ({len(has_zoho_no_code)} total):")
        print( "  These won't reach Luma until ensure_material_code() runs (typically at first receive).")
        if args.show_rows:
            for it in has_zoho_no_code[:args.limit]:
                print(f"    #{it.id:<6} zoho={it.zoho_item_id:<14} sku={it.sku_code or '-':<14} {it.name[:60]}")
            if len(has_zoho_no_code) > args.limit:
                print(f"    … {len(has_zoho_no_code) - args.limit} more")

    print()
    if missing_zoho or has_zoho_no_code:
        print("Run with --show-rows to see the problematic rows.")
    else:
        print("✓ All items with material_code also have zoho_item_id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
