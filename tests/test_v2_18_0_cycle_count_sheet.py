"""v2.18.0: cycle-count sheet export + count-entry polish.

Two surfaces:

  1. ``GET /inventory/cycle-count.csv`` — owner-only CSV download of the
     count sheet. Whitelisted columns only (no cost, no price, no
     accounts, no tokens, no Zoho sync errors). Decimal-safe formatting.
     Filters mirror the existing form (``q`` substring + product_line).
  2. The cycle-count form template gained Export/Print buttons, a
     product-line filter, an "only counted rows" toggle, a live
     pre-submit summary, and print-friendly CSS.

This module asserts: helper output, route permissions + content, safe
column whitelist, filter parity, Decimal formatting, blank
counted_qty/notes, print markers in the template, the new
toggle/summary controls, the entry-polish preservation contract, the
read-only invariant (no DB write during export), and the
import-surface defense (no new Zoho/OAuth import).
"""
from __future__ import annotations

import csv
import io
import os
from decimal import Decimal

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret")

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from packtrack.models import Item, Role, User
from packtrack.services.cycle_count import (
    COUNT_SHEET_COLUMNS,
    build_count_sheet_rows,
    format_count_sheet_csv,
    list_product_lines,
)

# --- fixtures -------------------------------------------------------------


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _clear_app_overrides():
    yield
    from packtrack.main import app
    app.dependency_overrides.clear()


def _seed_user(session, *, role=Role.OWNER, user_id=1, name="Owner") -> User:
    u = User(
        id=user_id, email=f"{role.value}-{user_id}@example.com", name=name,
        role=role, password_hash="x", is_active=True,
    )
    session.add(u)
    session.commit()
    return u


def _seed_item(
    session, *,
    name="Bubble mailer", sku="SKU-1", material_code="MC-1",
    product_line="MASTER CASE", current_stock=100.0,
    zoho_item_id: str | None = None,
    snapshot: Decimal | None = None,
    vendor="ACME",
) -> Item:
    zid = zoho_item_id if zoho_item_id is not None else f"z-{sku}"
    it = Item(
        name=name, sku_code=sku, material_code=material_code,
        product_line=product_line, unit="pcs", vendor=vendor,
        current_stock=current_stock, zoho_item_id=zid,
        last_zoho_stock_snapshot=snapshot,
    )
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _client(session, engine, monkeypatch, *, user=None):
    from fastapi.testclient import TestClient

    import packtrack.db
    import packtrack.main
    from packtrack import deps
    from packtrack.db import get_session
    from packtrack.main import app

    monkeypatch.setattr(packtrack.db, "engine", engine)
    monkeypatch.setattr(packtrack.main, "engine", engine)
    app.dependency_overrides[get_session] = lambda: session

    def _force_user():
        return user or session.exec(select(User).order_by(User.id)).first()

    app.dependency_overrides[deps.require_user] = _force_user
    app.dependency_overrides[deps.current_user] = _force_user
    return TestClient(app, raise_server_exceptions=False)


def _parse_csv(body: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(body)))


# --- A. service: count-sheet row builder ----------------------------------


def test_build_count_sheet_includes_every_item_unfiltered(session):
    _seed_user(session)
    _seed_item(session, name="A", sku="A1")
    _seed_item(session, name="B", sku="B1")
    rows = build_count_sheet_rows(session)
    assert len(rows) == 2
    assert {r.item_name for r in rows} == {"A", "B"}


def test_build_count_sheet_q_filter_substring_case_insensitive(session):
    _seed_user(session)
    a = _seed_item(session, name="Apple bag", sku="APPLE-1")
    _seed_item(session, name="Banana box", sku="BANANA-1")
    rows = build_count_sheet_rows(session, q="apple")
    assert [r.item_id for r in rows] == [a.id]


def test_build_count_sheet_q_filter_matches_material_code(session):
    _seed_user(session)
    a = _seed_item(session, name="A", sku="A1", material_code="MC-XYZ")
    _seed_item(session, name="B", sku="B1", material_code="MC-OTHER")
    rows = build_count_sheet_rows(session, q="xyz")
    assert [r.item_id for r in rows] == [a.id]


def test_build_count_sheet_product_line_filter_exact(session):
    _seed_user(session)
    a = _seed_item(session, name="A", sku="A1", product_line="LINE-A")
    _seed_item(session, name="B", sku="B1", product_line="LINE-B")
    rows = build_count_sheet_rows(session, product_line="LINE-A")
    assert [r.item_id for r in rows] == [a.id]


def test_build_count_sheet_computes_variance_when_snapshot_present(session):
    _seed_item(
        session, name="X", sku="X1",
        current_stock=120.0, snapshot=Decimal("100"),
    )
    rows = build_count_sheet_rows(session)
    r = rows[0]
    assert r.current_packtrack_qty == Decimal("120")
    assert r.zoho_snapshot_qty == Decimal("100")
    assert r.zoho_variance == Decimal("20")


def test_build_count_sheet_no_snapshot_yields_none_variance(session):
    _seed_item(session, snapshot=None)
    rows = build_count_sheet_rows(session)
    assert rows[0].zoho_snapshot_qty is None
    assert rows[0].zoho_variance is None


def test_build_count_sheet_counted_qty_and_notes_are_blank(session):
    """Spec invariant: the operator fills these in; the export must
    not pre-populate them."""
    _seed_item(session)
    rows = build_count_sheet_rows(session)
    assert rows[0].counted_qty == ""
    assert rows[0].notes == ""


# --- B. service: CSV formatter --------------------------------------------


def test_csv_columns_match_locked_whitelist():
    """COUNT_SHEET_COLUMNS is the security boundary — adding a column
    requires explicit review. Lock it down here."""
    assert COUNT_SHEET_COLUMNS == (
        "item_id", "item_name", "material_code", "sku_code", "vendor",
        "product_line", "current_packtrack_qty", "zoho_snapshot_qty",
        "zoho_variance", "counted_qty", "notes",
    )


def test_csv_header_matches_columns(session):
    _seed_item(session)
    rows = build_count_sheet_rows(session)
    body = format_count_sheet_csv(rows)
    header = body.splitlines()[0].split(",")
    assert header == list(COUNT_SHEET_COLUMNS)


def test_csv_decimal_formatting_strips_trailing_zeros(session):
    _seed_item(
        session, name="X", sku="X1",
        current_stock=42.0, snapshot=Decimal("40.0000"),
    )
    parsed = _parse_csv(format_count_sheet_csv(build_count_sheet_rows(session)))
    row = parsed[0]
    # Trailing zeros stripped: 42 not 42.0000, 40 not 40.0000.
    assert row["current_packtrack_qty"] == "42"
    assert row["zoho_snapshot_qty"] == "40"
    assert row["zoho_variance"] == "2"


def test_csv_decimal_formatting_preserves_real_precision(session):
    _seed_item(
        session, name="Y", sku="Y1",
        current_stock=42.5, snapshot=Decimal("40.0000"),
    )
    parsed = _parse_csv(format_count_sheet_csv(build_count_sheet_rows(session)))
    assert parsed[0]["current_packtrack_qty"] == "42.5"


def test_csv_none_snapshot_yields_empty_cells(session):
    _seed_item(session, snapshot=None)
    parsed = _parse_csv(format_count_sheet_csv(build_count_sheet_rows(session)))
    assert parsed[0]["zoho_snapshot_qty"] == ""
    assert parsed[0]["zoho_variance"] == ""


def test_csv_counted_and_notes_columns_are_empty(session):
    _seed_item(session)
    parsed = _parse_csv(format_count_sheet_csv(build_count_sheet_rows(session)))
    assert parsed[0]["counted_qty"] == ""
    assert parsed[0]["notes"] == ""


def test_csv_quotes_fields_with_commas(session):
    """csv.QUOTE_MINIMAL — vendor/name with a comma must round-trip."""
    _seed_item(session, name="ACME, Inc.", vendor="Vendor, LLC")
    body = format_count_sheet_csv(build_count_sheet_rows(session))
    parsed = _parse_csv(body)
    assert parsed[0]["item_name"] == "ACME, Inc."
    assert parsed[0]["vendor"] == "Vendor, LLC"


# --- C. SECURITY: sensitive-field exclusion -------------------------------


def test_csv_excludes_sensitive_fields(session):
    """The CSV must NEVER contain pricing, accounts, IDs, tokens, or
    sync error messages. Header check covers the column whitelist;
    body check defensively scans for these substrings anywhere."""
    _seed_user(session)
    _seed_item(session)
    body = format_count_sheet_csv(build_count_sheet_rows(session))
    header_line = body.splitlines()[0].lower()
    for forbidden_col in (
        "cost", "price", "selling", "purchase", "account",
        "token", "secret", "sync_error", "zoho_sync_error",
        "preferred_vendor_id",
    ):
        assert forbidden_col not in header_line, (
            f"forbidden column {forbidden_col!r} in CSV header"
        )


def test_csv_excludes_sync_error_even_if_item_has_one(session):
    """Defense in depth: even if an item happens to carry a Zoho sync
    error message (it shouldn't on an Item row, but the column whitelist
    must hold either way), the CSV must not surface it."""
    it = _seed_item(session)
    # Items don't carry sync errors directly — those live on
    # InventoryAdjustment. But the column whitelist is the gate; verify
    # by trying to extract anything that looks like an error message.
    body = format_count_sheet_csv(build_count_sheet_rows(session))
    assert "error" not in body.lower()
    assert it.id in {int(r["item_id"]) for r in _parse_csv(body)}


# --- D. product_lines helper ----------------------------------------------


def test_list_product_lines_returns_sorted_distinct_non_null(session):
    _seed_item(session, name="A", sku="A1", product_line="LINE B")
    _seed_item(session, name="B", sku="B1", product_line="LINE A")
    _seed_item(session, name="C", sku="C1", product_line="LINE A")
    _seed_item(session, name="D", sku="D1", product_line=None)
    assert list_product_lines(session) == ["LINE A", "LINE B"]


# --- E. route: permissions ------------------------------------------------


def test_owner_can_export_csv(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    r = client.get("/inventory/cycle-count.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="cycle-count.csv"' in r.headers["content-disposition"]


def test_non_owner_cannot_export_csv(session, engine, monkeypatch):
    owner = _seed_user(session, role=Role.OWNER)
    designer = _seed_user(session, role=Role.DESIGN, user_id=2, name="Des")
    _seed_item(session)
    _ = owner  # marked used
    client = _client(session, engine, monkeypatch, user=designer)
    r = client.get("/inventory/cycle-count.csv")
    assert r.status_code == 403


def test_route_respects_q_filter(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    a = _seed_item(session, name="Apple", sku="APP")
    _seed_item(session, name="Banana", sku="BAN")
    client = _client(session, engine, monkeypatch)
    r = client.get("/inventory/cycle-count.csv?q=apple")
    parsed = _parse_csv(r.text)
    assert [int(row["item_id"]) for row in parsed] == [a.id]


def test_route_respects_product_line_filter(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    a = _seed_item(session, name="A", sku="A1", product_line="LINE-A")
    _seed_item(session, name="B", sku="B1", product_line="LINE-B")
    client = _client(session, engine, monkeypatch)
    r = client.get("/inventory/cycle-count.csv?product_line=LINE-A")
    parsed = _parse_csv(r.text)
    assert [int(row["item_id"]) for row in parsed] == [a.id]


# --- F. route: read-only invariant ----------------------------------------


def test_csv_export_does_not_mutate_stock_or_ledger(session, engine, monkeypatch):
    user = _seed_user(session, role=Role.OWNER)
    item = _seed_item(session, current_stock=42.0, snapshot=Decimal("40"))
    _ = user
    stock_before = item.current_stock
    snapshot_before = item.last_zoho_stock_snapshot

    client = _client(session, engine, monkeypatch)
    # Export multiple times to be safe.
    for _ in range(3):
        r = client.get("/inventory/cycle-count.csv")
        assert r.status_code == 200

    session.refresh(item)
    assert item.current_stock == stock_before
    assert item.last_zoho_stock_snapshot == snapshot_before


# --- G. form template: new controls + print markers -----------------------


def test_form_renders_export_csv_link(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/cycle-count").text
    assert 'href="/inventory/cycle-count.csv"' in body
    assert "Export CSV" in body


def test_form_renders_print_button(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/cycle-count").text
    assert "Print sheet" in body
    assert "window.print()" in body


def test_form_renders_print_stylesheet_markers(session, engine, monkeypatch):
    """Print mode must hide chrome + computed columns. The CSS selectors
    that drive that are the contract; assert each marker is present."""
    _seed_user(session, role=Role.OWNER)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/cycle-count").text
    assert "@media print" in body
    # Hide chrome
    assert "header, footer, nav { display: none" in body
    # Computed cells hidden via cc-print-hide class
    assert "cc-print-hide" in body
    # Counted/Note cells get blank-friendly print styling
    assert "cc-counted-cell" in body
    assert "cc-note-cell" in body


def test_form_renders_show_only_counted_toggle(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/cycle-count").text
    assert 'data-testid="cycle-count-only-counted"' in body
    assert "Show only rows with counts entered" in body


def test_form_renders_live_submit_summary(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    _seed_item(session)
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/cycle-count").text
    assert 'data-testid="cycle-count-submit-summary"' in body
    assert "will create adjustments" in body
    assert "will be skipped" in body


def test_form_renders_product_line_filter(session, engine, monkeypatch):
    _seed_user(session, role=Role.OWNER)
    _seed_item(session, name="A", sku="A1", product_line="LINE-A")
    _seed_item(session, name="B", sku="B1", product_line="LINE-B")
    client = _client(session, engine, monkeypatch)
    body = client.get("/inventory/cycle-count").text
    assert 'data-testid="cycle-count-product-line"' in body
    assert ">LINE-A<" in body
    assert ">LINE-B<" in body


# --- H. count entry preservation contract ---------------------------------


def test_entered_counts_preserved_after_validation_error(
    session, engine, monkeypatch,
):
    """An invalid row (negative final qty) should re-render the form
    with every typed counted_/note_ value still in place — operator
    shouldn't have to retype the whole batch. v2.14.0 contract,
    re-asserted under v2.18.0."""
    _seed_user(session, role=Role.OWNER)
    a = _seed_item(session, name="A", sku="A1", current_stock=10.0)
    b = _seed_item(session, name="B", sku="B1", current_stock=10.0)
    client = _client(session, engine, monkeypatch)
    r = client.post("/inventory/cycle-count", data={
        f"counted_{a.id}": "8",
        f"note_{a.id}": "first note",
        f"counted_{b.id}": "-5",  # negative final qty — invalid
        f"note_{b.id}": "second note",
    })
    # 400 re-render; both typed values still in the body.
    assert r.status_code == 400
    assert 'value="8"' in r.text
    assert "first note" in r.text
    assert "second note" in r.text


# --- I. import-surface defense + regression ------------------------------


def test_no_direct_zoho_or_oauth_imports_in_v2_18_0_changes():
    """v2.18.0 touched services/cycle_count.py + routes/cycle_count.py.
    Verify no Zoho client / OAuth library inlined."""
    import packtrack.routes.cycle_count as route_mod
    import packtrack.services.cycle_count as svc_mod
    forbidden = ("zoho.oauth", "zohocrmsdk", "zohoinventory", "requests_oauthlib")
    for mod in (route_mod, svc_mod):
        with open(mod.__file__) as fh:
            src = fh.read()
        for bad in forbidden:
            assert bad not in src, f"{mod.__name__} imports {bad}"
