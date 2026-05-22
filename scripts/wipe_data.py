"""Wipe transactional data — POs, lines, events, attachments, shipments,
items, Zoho mirrors, sync logs. Keeps users + AppSettings.

Use before pulling real data in:

    sudo -u packtrack bash -lc 'cd /opt/packtrack/app && . .venv/bin/activate && \
        set -a && source /etc/packtrack/packtrack.env && set +a && python scripts/wipe_data.py --yes'
"""
from __future__ import annotations

import argparse

from packtrack.wipe import wipe


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true",
                    help="Skip the WIPE confirmation prompt.")
    args = ap.parse_args()
    if not args.yes:
        confirm = input("Type 'WIPE' to confirm: ").strip()
        if confirm != "WIPE":
            raise SystemExit("Aborted.")
    counts = wipe()
    print("Wiped:")
    for tbl, n in counts.items():
        print(f"  {tbl}: {n} rows")


if __name__ == "__main__":
    main()
