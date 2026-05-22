"""Build the role-aware inbox.

This is the new home page — instead of a busy dashboard, each role sees only
the items they can act on right now. Empty inbox = nothing to do.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, select

from packtrack.models import (
    Item,
    POStatus,
    PurchaseOrder,
    Role,
    Shipment,
    ShipStatus,
    User,
)


@dataclass
class InboxItem:
    title: str
    subtitle: str
    cta: str  # button label
    href: str
    severity: str = "info"  # info | warning | danger


def _po_card(po: PurchaseOrder, cta: str, severity: str = "info") -> InboxItem:
    summary = po.item_summary
    if not summary:
        items = "(no lines)"
    elif len(summary) == 1:
        items = summary[0]["name"]
    elif len(summary) <= 3:
        items = ", ".join(s["name"] for s in summary)
    else:
        head = f"{summary[0]['name']} + {len(summary) - 1} more"
        items = f"{head} · {po.total:,.0f} {po.currency}" if po.total else head
    return InboxItem(
        title=po.po_number,
        subtitle=items,
        cta=cta,
        href=f"/po/{po.id}",
        severity=severity,
    )


def build_inbox(session: Session, user: User) -> list[InboxItem]:
    items: list[InboxItem] = []

    if user.role == Role.OWNER:
        # PIs awaiting approval
        for po in session.exec(
            select(PurchaseOrder).where(PurchaseOrder.status == POStatus.PI_RECEIVED)
        ):
            items.append(_po_card(po, "Approve PI", severity="warning"))

        # Critical / amber stock
        for it in session.exec(select(Item).where(Item.current_stock <= Item.critical_point)):
            items.append(InboxItem(
                title=it.name,
                subtitle=f"{it.current_stock:g} {it.unit} left — critical",
                cta="Reorder",
                href=f"/po/new?item_id={it.id}",
                severity="danger",
            ))

        # Shipments arrived but not yet received (sat in receiving)
        for sh in session.exec(
            select(Shipment).where(Shipment.status == ShipStatus.IN_TRANSIT)
        ):
            if sh.eta is not None:
                items.append(InboxItem(
                    title=f"{sh.po.po_number} arriving {sh.eta}",
                    subtitle=f"{sh.method.value} — {sh.quantity:g} units",
                    cta="View",
                    href=f"/po/{sh.po.id}",
                ))

    elif user.role == Role.DESIGN:
        for po in session.exec(
            select(PurchaseOrder).where(PurchaseOrder.status == POStatus.DESIGN_REVIEW)
        ):
            items.append(_po_card(po, "Review art", severity="warning"))

    elif user.role == Role.AGENT:
        for po in session.exec(
            select(PurchaseOrder).where(PurchaseOrder.status == POStatus.DESIGN_APPROVED)
        ):
            items.append(_po_card(po, "Upload PI", severity="warning"))
        for po in session.exec(
            select(PurchaseOrder).where(PurchaseOrder.status == POStatus.PRODUCTION)
        ):
            items.append(_po_card(po, "Mark shipped", severity="info"))

    elif user.role == Role.RECEIVING:
        for sh in session.exec(
            select(Shipment).where(Shipment.status == ShipStatus.IN_TRANSIT)
        ):
            items.append(InboxItem(
                title=f"{sh.po.po_number}",
                subtitle=f"{sh.method.value} — {sh.quantity:g} units, ETA {sh.eta or '—'}",
                cta="Receive",
                href=f"/po/{sh.po.id}#shipments",
                severity="warning" if sh.eta else "info",
            ))

    return items
