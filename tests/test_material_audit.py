"""Tests for the material-code identity audit helpers.

Pure-function tests — no database, no fixtures beyond plain ``ItemView``
dataclasses. The CLI in ``scripts/audit_material_codes.py`` is the only
caller that touches Postgres, so by testing the pure helpers we cover the
decision logic without standing up a test DB.
"""
from __future__ import annotations

from packtrack.services.material_audit import (
    ItemView,
    audit,
    compute_safe_defaults,
    find_blank_sku_codes,
    find_duplicate_material_codes,
    find_duplicate_sku_codes,
    find_missing_material_codes,
    find_missing_zoho_ids,
    find_vendor_material_conflicts,
)


def _item(
    id: int,
    *,
    sku: str | None = None,
    zoho: str | None = None,
    name: str = "",
    vendor: str | None = None,
    material: str | None = None,
) -> ItemView:
    return ItemView(
        id=id,
        zoho_item_id=zoho,
        name=name or f"Item {id}",
        sku_code=sku,
        vendor=vendor,
        material_code=material,
    )


# ---------- detection ----------------------------------------------------


def test_audit_detects_blank_sku_codes():
    items = [
        _item(1, sku="A"),
        _item(2, sku=None),
        _item(3, sku=""),
        _item(4, sku="   "),  # whitespace-only counts as blank
    ]
    blanks = find_blank_sku_codes(items)
    assert sorted(it.id for it in blanks) == [2, 3, 4]


def test_audit_detects_duplicate_sku_codes():
    items = [
        _item(1, sku="A", vendor="Helen"),
        _item(2, sku="A", vendor="Other"),  # collision across vendors
        _item(3, sku="B"),
        _item(4, sku=None),
    ]
    dups = find_duplicate_sku_codes(items)
    assert "A" in dups
    assert {it.id for it in dups["A"]} == {1, 2}
    assert "B" not in dups


def test_audit_duplicate_sku_is_case_sensitive():
    # Zoho is case-sensitive, so we are too.
    items = [_item(1, sku="abc"), _item(2, sku="ABC")]
    assert find_duplicate_sku_codes(items) == {}


def test_audit_detects_missing_zoho_ids():
    items = [_item(1, zoho="z1"), _item(2, zoho=""), _item(3, zoho=None)]
    assert {it.id for it in find_missing_zoho_ids(items)} == {2, 3}


def test_audit_detects_missing_material_codes():
    items = [_item(1, material="MAT-A"), _item(2, material=""), _item(3, material=None)]
    assert {it.id for it in find_missing_material_codes(items)} == {2, 3}


def test_audit_detects_duplicate_material_codes():
    items = [
        _item(1, material="MAT-A"),
        _item(2, material="MAT-A"),
        _item(3, material="MAT-B"),
        _item(4, material=None),
    ]
    dups = find_duplicate_material_codes(items)
    assert "MAT-A" in dups
    assert {it.id for it in dups["MAT-A"]} == {1, 2}


def test_audit_detects_vendor_material_conflicts():
    items = [
        _item(1, vendor="Helen", material="X"),
        _item(2, vendor="Helen", material="X"),  # within-vendor collision
        _item(3, vendor="Other", material="X"),  # cross-vendor — NOT a conflict here
    ]
    conflicts = find_vendor_material_conflicts(items)
    assert ("Helen", "X") in conflicts
    assert ("Other", "X") not in conflicts


# ---------- safe-default proposal ----------------------------------------


def test_safe_default_only_copies_unique_skus():
    items = [
        _item(1, sku="UNIQUE-A"),
        _item(2, sku="UNIQUE-B"),
        _item(3, sku="DUP"),
        _item(4, sku="DUP"),
    ]
    proposals = compute_safe_defaults(items)
    proposed_ids = {p.item_id for p in proposals}
    assert proposed_ids == {1, 2}, (
        "Duplicates must be skipped — safe defaults only fire when the sku is "
        "unique across the audited set."
    )


def test_safe_default_never_overwrites_existing_material_code():
    items = [
        _item(1, sku="UNIQUE-A", material="ALREADY-SET"),
        _item(2, sku="UNIQUE-B", material=None),
    ]
    proposals = compute_safe_defaults(items)
    assert {p.item_id for p in proposals} == {2}


def test_safe_default_skips_blank_sku():
    items = [
        _item(1, sku=None, material=None),
        _item(2, sku="", material=None),
        _item(3, sku="   ", material=None),
    ]
    assert compute_safe_defaults(items) == []


def test_safe_default_uses_stripped_value():
    """Leading/trailing whitespace is stripped when copying — Zoho occasionally
    returns padded strings, and we don't want that to leak into Luma payloads."""
    items = [_item(1, sku="  CLEAN  ", material=None)]
    proposals = compute_safe_defaults(items)
    assert len(proposals) == 1
    assert proposals[0].proposed_material_code == "CLEAN"


# ---------- aggregate report --------------------------------------------


def test_audit_aggregate_report_shape():
    items = [
        _item(1, sku="A", zoho="z1", vendor="Helen", material="MAT-A"),
        _item(2, sku="A", zoho="z2", vendor="Other"),  # dup sku
        _item(3, sku=None, zoho=None),                 # blank sku, no zoho
        _item(4, sku="B", zoho="z4", material="MAT-B"),
    ]
    rep = audit(items)
    assert rep.total == 4
    assert rep.with_sku_code == 3
    assert {it.id for it in rep.blank_sku_code} == {3}
    assert "A" in rep.duplicate_sku_code
    assert {it.id for it in rep.missing_zoho_id} == {3}
    assert {it.id for it in rep.missing_material_code} == {2, 3}
    # No safe defaults: A is duplicate, item 3 has no sku, items 1 & 4
    # already have material_code. So nothing is left to propose.
    assert rep.safe_defaults == []


def test_audit_empty_input_is_safe():
    """An empty database (current state) must produce a clean empty report
    rather than raising or emitting noise."""
    rep = audit([])
    assert rep.total == 0
    assert rep.with_sku_code == 0
    assert rep.blank_sku_code == []
    assert rep.duplicate_sku_code == {}
    assert rep.missing_zoho_id == []
    assert rep.missing_material_code == []
    assert rep.duplicate_material_code == {}
    assert rep.vendor_material_conflicts == {}
    assert rep.safe_defaults == []
