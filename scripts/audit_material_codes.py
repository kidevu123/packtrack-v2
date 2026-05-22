"""Audit + (optionally) safe-default-backfill the material identity columns.

Usage:

    # Read-only report
    sudo -u packtrack bash -lc 'cd /opt/packtrack/app && . .venv/bin/activate \
        && set -a && source /etc/packtrack/packtrack.env && set +a \
        && python scripts/audit_material_codes.py'

    # Apply safe defaults: copy sku_code → material_code where sku_code is
    # both unique and non-empty AND material_code is currently null. Never
    # overwrites an existing material_code. Prints every row changed.
    sudo -u packtrack bash -lc '... python scripts/audit_material_codes.py --apply-safe-defaults'

The script prints:

  * Totals (items, missing sku, missing zoho id, missing material_code)
  * Duplicate sku_code groups, with item ids + vendors
  * Duplicate material_code groups
  * (vendor, material_code) collisions
  * The list of items requiring manual review (no sku_code, or duplicate sku)
  * Proposed safe-default copies — preview only unless --apply-safe-defaults

It does **not** mutate rows without ``--apply-safe-defaults``.
"""
from __future__ import annotations

import argparse
import sys

from sqlmodel import Session, select

from packtrack.db import engine
from packtrack.models import Item
from packtrack.services.material_audit import (
    ItemView,
    audit,
)


def _to_view(it: Item) -> ItemView:
    return ItemView(
        id=it.id or 0,
        zoho_item_id=it.zoho_item_id,
        name=it.name or "",
        sku_code=it.sku_code,
        vendor=it.vendor,
        material_code=it.material_code,
    )


def _line(it: ItemView) -> str:
    bits = [
        f"id={it.id}",
        f"name={it.name!r}",
    ]
    if it.sku_code is not None:
        bits.append(f"sku={it.sku_code!r}")
    if it.zoho_item_id:
        bits.append(f"zoho={it.zoho_item_id}")
    if it.vendor:
        bits.append(f"vendor={it.vendor!r}")
    if it.material_code:
        bits.append(f"mat={it.material_code!r}")
    return "  " + " · ".join(bits)


def _print_report(items: list[ItemView]) -> None:
    rep = audit(items)
    print("=" * 72)
    print("PackTrack — Material identity audit")
    print("=" * 72)
    print(f"Total items: {rep.total}")
    print(f"  with sku_code: {rep.with_sku_code}")
    print(f"  blank sku_code: {len(rep.blank_sku_code)}")
    print(f"  missing zoho_item_id: {len(rep.missing_zoho_id)}")
    print(f"  missing material_code: {len(rep.missing_material_code)}")
    print()

    if rep.duplicate_sku_code:
        print(f"Duplicate sku_code groups ({len(rep.duplicate_sku_code)}):")
        for sku, group in sorted(rep.duplicate_sku_code.items()):
            print(f"  sku_code={sku!r} — {len(group)} items")
            for it in group:
                print(_line(it))
        print()
    else:
        print("Duplicate sku_code groups: none")
        print()

    if rep.duplicate_material_code:
        print(f"Duplicate material_code groups ({len(rep.duplicate_material_code)}):")
        for code, group in sorted(rep.duplicate_material_code.items()):
            print(f"  material_code={code!r} — {len(group)} items")
            for it in group:
                print(_line(it))
        print()

    if rep.vendor_material_conflicts:
        print("(vendor, material_code) conflicts:")
        for (v, c), group in sorted(rep.vendor_material_conflicts.items()):
            print(f"  vendor={v!r} material_code={c!r} — {len(group)} items")
            for it in group:
                print(_line(it))
        print()

    if rep.blank_sku_code:
        print(f"Items with blank sku_code requiring manual review ({len(rep.blank_sku_code)}):")
        for it in rep.blank_sku_code[:20]:
            print(_line(it))
        if len(rep.blank_sku_code) > 20:
            print(f"  … and {len(rep.blank_sku_code) - 20} more")
        print()

    print(f"Safe-default proposals ({len(rep.safe_defaults)}):")
    if rep.safe_defaults:
        for s in rep.safe_defaults[:50]:
            print(f"  id={s.item_id} → material_code={s.proposed_material_code!r} (from {s.source})")
        if len(rep.safe_defaults) > 50:
            print(f"  … and {len(rep.safe_defaults) - 50} more")
        print("  (run with --apply-safe-defaults to write these.)")
    else:
        print("  None — every item with a non-empty unique sku_code already has material_code, "
              "or the population is too sparse / collision-prone to be safe.")
    print()


def _apply_defaults(session: Session, items: list[Item]) -> int:
    rep = audit([_to_view(it) for it in items])
    if not rep.safe_defaults:
        print("No safe defaults to apply.")
        return 0
    by_id = {it.id: it for it in items}
    n = 0
    for proposal in rep.safe_defaults:
        target = by_id.get(proposal.item_id)
        if target is None:
            continue
        # Defensive: never overwrite a value the user already set, even if
        # the snapshot said it was empty (race window).
        if target.material_code and target.material_code.strip():
            continue
        target.material_code = proposal.proposed_material_code
        print(
            f"  set id={target.id} material_code={proposal.proposed_material_code!r} "
            f"(was sku={target.sku_code!r})"
        )
        n += 1
    if n:
        session.commit()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit material identity for PackTrack items.")
    ap.add_argument(
        "--apply-safe-defaults",
        action="store_true",
        help=(
            "Copy sku_code into material_code only when sku_code is unique + "
            "non-empty AND material_code is currently null. Never overwrites."
        ),
    )
    args = ap.parse_args()

    with Session(engine) as session:
        items = list(session.exec(select(Item).order_by(Item.id)).all())
        views = [_to_view(it) for it in items]
        _print_report(views)
        if args.apply_safe_defaults:
            print("Applying safe defaults …")
            wrote = _apply_defaults(session, items)
            print(f"Done. {wrote} row(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
