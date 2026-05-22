"""Transactional data wipe — usable from CLI scripts and the admin route."""
from __future__ import annotations

import os
import shutil

from sqlmodel import Session, text

from packtrack.config import settings
from packtrack.db import engine

TABLES = (
    "po_events",
    "po_lines",
    "attachments",
    "shipments",
    "purchase_orders",
    "items",
    "zoho_mirror",
    "sync_runs",
)


def wipe(*, delete_files: bool = True) -> dict[str, int]:
    counts: dict[str, int] = {}
    with Session(engine) as session:
        for tbl in TABLES:
            row = session.exec(text(f"SELECT COUNT(*) FROM {tbl}")).first()
            counts[tbl] = int(row[0] if row else 0)
        session.exec(text(
            "TRUNCATE TABLE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE"
        ))
        session.commit()

    if delete_files:
        for sub in ("items", "pi", "artwork", "po_design", "proforma_invoices", "telegram"):
            d = os.path.join(str(settings.UPLOAD_DIR), sub)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        os.makedirs(os.path.join(str(settings.UPLOAD_DIR), "items"), exist_ok=True)

    return counts
