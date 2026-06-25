"""Derive a human "product line" / brand group from a packaging item name.

PackTrack inventory has no explicit brand column coming from Zoho, but item
names are consistently brand-prefixed (e.g. ``FIX 15mg 12ct Hybrid Focus
(Green) - Bottle Label``). This module turns a raw name into a stable display
group so /inventory can be browsed by product line.

The rule, reverse-engineered from real names:

* Strip any ``[Packaging]`` tag.
* Read the leading *alphabetic* tokens — these are the brand. Stop at the
  first token that contains a digit (a spec like ``15mg`` / ``12ct``), a
  separator (``-``), or an opening parenthesis.
* Cap the brand at two tokens so sub-brands like ``FIX Beyond`` are kept but
  long descriptive names don't become their own group.
* If there is no leading brand token, or the name leads with a generic
  packaging noun (``Master Case Box``), fall back to a single catch-all group.

Examples:
    "FIX 15mg 12ct Hybrid Focus (Green) - Bottle Label" -> "FIX"
    "FIX Beyond - Citrus Drift - Blister Card + Blister" -> "FIX Beyond"
    "25ct Master Case Box"                               -> "Unassigned / Generic"

The function is pure (no DB), so it is shared by the Zoho sync, the Alembic
backfill, and the tests.
"""
from __future__ import annotations

import re

GENERIC_GROUP = "Unassigned / Generic"

_TAG_RE = re.compile(r"\[/?packaging\]", re.IGNORECASE)
_SEPARATORS = {"-", "\u2013", "\u2014", "+", "|", "/", "·"}
_BRAND_TOKEN_RE = re.compile(r"^[A-Za-z&'.]+$")
_MAX_BRAND_TOKENS = 2

# Leading packaging nouns that signal "this is a generic/unbranded item",
# not a brand. Only checked for the FIRST token.
_GENERIC_LEADING = {
    "box", "boxes", "case", "cases", "master", "carton", "cartons",
    "roll", "rolls", "label", "labels", "bag", "bags", "pouch", "pouches",
    "sticker", "stickers", "generic", "misc", "sample", "samples", "tape",
    "insert", "inserts", "divider", "dividers", "tray", "trays", "lid",
    "lids", "cap", "caps", "sleeve", "sleeves", "shrink", "film", "wrap",
    "pallet", "bottle", "bottles", "jar", "jars", "tube", "tubes",
    "blister", "card", "cards", "shipper", "mailer",
}


def derive_product_line(name: str | None) -> str:
    """Return the display product line / brand group for an item name."""
    if not name:
        return GENERIC_GROUP

    cleaned = _TAG_RE.sub(" ", name).strip()
    if not cleaned:
        return GENERIC_GROUP

    brand: list[str] = []
    for token in cleaned.split():
        if token in _SEPARATORS or token.startswith("("):
            break
        if any(ch.isdigit() for ch in token):
            break
        word = token.strip(".,")
        if not word or not _BRAND_TOKEN_RE.match(word):
            break
        if not brand and word.lower() in _GENERIC_LEADING:
            return GENERIC_GROUP
        brand.append(word)
        if len(brand) >= _MAX_BRAND_TOKENS:
            break

    if not brand:
        return GENERIC_GROUP
    return " ".join(brand)


def group_sort_key(product_line: str) -> tuple[int, str]:
    """Sort key that keeps the generic catch-all group last, brands A→Z."""
    is_generic = 1 if product_line == GENERIC_GROUP else 0
    return (is_generic, product_line.lower())
