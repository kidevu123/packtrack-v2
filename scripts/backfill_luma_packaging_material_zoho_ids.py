"""Backfill Luma packaging_materials.zoho_item_id from PackTrack.

PackTrack already owns the canonical mapping between material_code and
zoho_item_id. Luma's /api/integrations/packtrack/items endpoint accepts
either field but, until v1.4.18, did not update an existing
packaging_materials row's zoho_item_id when one was missing. After the
matching Luma patch lands, this script re-POSTs every eligible PackTrack
item so Luma backfills its mappings — without any receipt push, without
any Zoho write.

Usage:

    # Default dry-run — no writes to Luma.
    python scripts/backfill_luma_packaging_material_zoho_ids.py

    # Apply (calls Luma).
    python scripts/backfill_luma_packaging_material_zoho_ids.py --apply

    # Narrow scope.
    python scripts/backfill_luma_packaging_material_zoho_ids.py --item-id 42
    python scripts/backfill_luma_packaging_material_zoho_ids.py --material-code PT-00095
    python scripts/backfill_luma_packaging_material_zoho_ids.py --limit 25

Eligibility:

* item.material_code is not null/blank
* item.zoho_item_id is not null/blank
  (items missing zoho_item_id are reported but not pushed — they have
   nothing useful to backfill on Luma's side.)

Outcomes are read from Luma's structured response (REGISTERED / UPDATED /
ALREADY_MAPPED / ZOHO_ID_CONFLICT_REVIEW_REQUIRED). Conflicts are NEVER
auto-overwritten — they are listed for operator review.

This script does NOT push BoxReceipts, does NOT touch Zoho directly, and
does NOT mutate PackTrack rows. Side effects are restricted to Luma's
items registration endpoint.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from dataclasses import dataclass

import httpx
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.db import engine
from packtrack.models import Item
from packtrack.services.receiving import (
    LumaRegistrationOutcome,
    LumaRegistrationResult,
    _infer_luma_kind,
    register_item_with_luma,
)

logger = logging.getLogger("packtrack.backfill_luma_zoho_ids")


@dataclass(frozen=True)
class RowReport:
    item_id: int
    material_code: str | None
    name: str
    sku_code: str | None
    zoho_item_id: str | None
    inferred_kind: str
    unit: str
    proposed_action: str
    result: LumaRegistrationResult | None = None


def _eligible(item: Item) -> bool:
    return bool(
        (item.material_code or "").strip()
        and (item.zoho_item_id or "").strip()
    )


def _proposed_action(item: Item) -> str:
    if not (item.material_code or "").strip():
        return "skip — no material_code"
    if not (item.zoho_item_id or "").strip():
        return "skip — no zoho_item_id"
    return "register/update Luma mapping"


def _gather(
    session: Session,
    *,
    item_id: int | None,
    material_code: str | None,
    limit: int | None,
) -> list[Item]:
    q = select(Item).order_by(Item.id)
    if item_id is not None:
        q = q.where(Item.id == item_id)
    if material_code:
        q = q.where(Item.material_code == material_code)
    items = session.exec(q).all()
    if limit is not None:
        items = items[:limit]
    return items


def _format_table(rows: list[RowReport]) -> str:
    headers = ["id", "material_code", "name", "sku_code", "zoho_item_id", "kind", "unit", "action"]
    body: list[list[str]] = [
        [
            str(r.item_id),
            (r.material_code or "")[:14],
            r.name[:40],
            (r.sku_code or "")[:14],
            (r.zoho_item_id or "")[:18],
            r.inferred_kind,
            r.unit,
            r.proposed_action[:48],
        ]
        for r in rows
    ]
    cols = list(zip(headers, *body, strict=False))
    widths = [max(len(c) for c in col) for col in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    out_lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    out_lines.extend(fmt.format(*row) for row in body)
    return "\n".join(out_lines)


def _run(args: argparse.Namespace) -> int:
    if not settings.LUMA_RECEIPT_WEBHOOK_URL or not settings.LUMA_PACKTRACK_SECRET:
        print("Luma is not configured — set LUMA_RECEIPT_WEBHOOK_URL and "
              "LUMA_PACKTRACK_SECRET first.", file=sys.stderr)
        return 2

    with Session(engine) as session:
        items = _gather(
            session,
            item_id=args.item_id,
            material_code=args.material_code,
            limit=args.limit,
        )

    if not items:
        print("No items matched.")
        return 0

    rows: list[RowReport] = [
        RowReport(
            item_id=int(it.id) if it.id is not None else -1,
            material_code=it.material_code,
            name=it.name,
            sku_code=it.sku_code,
            zoho_item_id=it.zoho_item_id,
            inferred_kind=_infer_luma_kind(it.name or ""),
            unit=it.unit,
            proposed_action=_proposed_action(it),
        )
        for it in items
    ]

    eligible = [it for it in items if _eligible(it)]
    skipped_no_code = sum(1 for it in items if not (it.material_code or "").strip())
    skipped_no_zoho = sum(
        1 for it in items
        if (it.material_code or "").strip()
        and not (it.zoho_item_id or "").strip()
    )

    print(_format_table(rows))
    print()
    print(f"Total items inspected:        {len(items)}")
    print(f"Eligible for backfill:        {len(eligible)}")
    print(f"Skipped — no material_code:   {skipped_no_code}")
    print(f"Skipped — no zoho_item_id:    {skipped_no_zoho}")

    if not args.apply:
        print("\nDry-run only — no calls were made to Luma. Re-run with --apply to write.")
        return 0

    print(f"\nApplying — calling Luma for {len(eligible)} item(s)…")
    outcome_counts: Counter[str] = Counter()
    conflicts: list[RowReport] = []
    failures: list[RowReport] = []
    updates: list[RowReport] = []

    with httpx.Client(timeout=30.0) as client:
        for it in eligible:
            result = register_item_with_luma(it, client=client)
            outcome_counts[result.outcome.value] += 1
            report = RowReport(
                item_id=int(it.id) if it.id is not None else -1,
                material_code=it.material_code,
                name=it.name,
                sku_code=it.sku_code,
                zoho_item_id=it.zoho_item_id,
                inferred_kind=_infer_luma_kind(it.name or ""),
                unit=it.unit,
                proposed_action=result.outcome.value,
                result=result,
            )
            if result.outcome is LumaRegistrationOutcome.CONFLICT:
                conflicts.append(report)
            elif result.outcome is LumaRegistrationOutcome.FAILED:
                failures.append(report)
            elif result.outcome is LumaRegistrationOutcome.UPDATED:
                updates.append(report)

    print("\nApply summary:")
    for k in sorted(outcome_counts):
        print(f"  {k:<30}{outcome_counts[k]}")

    if updates:
        print("\nUpdated (backfilled zoho_item_id):")
        for r in updates:
            print(f"  #{r.item_id:<6}{r.material_code:<14}  {r.name[:60]}")

    if conflicts:
        print("\nCONFLICTS — review required (PackTrack zoho_item_id ≠ Luma's existing):")
        for r in conflicts:
            existing = r.result.existing_zoho_item_id if r.result else "?"
            print(
                f"  #{r.item_id:<6}{r.material_code:<14}  "
                f"PT={r.zoho_item_id}  Luma={existing}  {r.name[:50]}"
            )

    if failures:
        print("\nFailures:")
        for r in failures:
            print(
                f"  #{r.item_id:<6}{r.material_code:<14}  "
                f"{r.result.message if r.result else 'no result'}"
            )

    return 1 if (conflicts or failures) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--apply", action="store_true",
                        help="Actually call Luma. Default is dry-run.")
    parser.add_argument("--item-id", type=int, default=None,
                        help="Only consider this PackTrack item id.")
    parser.add_argument("--material-code", type=str, default=None,
                        help="Only consider this material_code.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after this many items.")
    parser.add_argument("--verbose", action="store_true",
                        help="DEBUG-level logs.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
