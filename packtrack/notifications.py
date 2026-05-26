"""Notification dispatcher.

Single function call from any route: ``notify(event, po, **ctx)``. Handlers
register themselves at import time. Adding a new channel (email, SMS, Slack)
means writing a handler and registering it — no route changes.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypedDict

from sqlmodel import Session, select

from packtrack.models import PurchaseOrder, Role, User

logger = logging.getLogger("packtrack.notify")

Handler = Callable[[str, PurchaseOrder, list[User], dict], None]
_HANDLERS: list[Handler] = []


class EventCtx(TypedDict, total=False):
    actor: User
    reason: str
    summary: str
    notes: str


def register(handler: Handler) -> Handler:
    _HANDLERS.append(handler)
    return handler


# Map event kind -> roles that should be notified.
_ROUTING: dict[str, tuple[Role, ...]] = {
    "po.created": (Role.AGENT, Role.DESIGN),
    "po.design_approved": (Role.AGENT,),
    "po.design_rejected": (Role.OWNER,),
    "po.art_changes_flagged": (Role.OWNER,),
    "po.pi_uploaded": (Role.OWNER,),
    "po.pi_approved": (Role.AGENT, Role.DESIGN),
    "po.pi_rejected": (Role.AGENT,),
    "po.production_started": (Role.OWNER, Role.AGENT),
    "po.shipped": (Role.OWNER, Role.RECEIVING),
    "po.received": (Role.OWNER,),
    "po.discrepancy": (Role.OWNER,),
    "po.returned_to_design": (Role.DESIGN,),
    "po.artwork_uploaded": (Role.AGENT,),
}


def notify(session: Session, event: str, po: PurchaseOrder, **ctx) -> None:
    roles = _ROUTING.get(event, ())
    if not roles:
        logger.debug("No routing for event %s — dropping", event)
        return
    recipients = list(
        session.exec(
            select(User).where(User.role.in_(roles), User.is_active == True)  # noqa: E712
        )
    )
    for handler in _HANDLERS:
        try:
            handler(event, po, recipients, ctx)
        except Exception:  # never let a notify call break a request
            logger.exception("Notification handler %s failed for %s", handler.__name__, event)


def notify_stock_alert(session: Session, item: "Item", alert_type: str) -> None:
    """Send Telegram stock alert to all active owners when a threshold is crossed.

    alert_type: 'reorder' or 'critical'. Never raises.
    """
    from packtrack.models import Item as _Item  # avoid circular at module level
    from packtrack.telegram import send

    owners = list(
        session.exec(
            select(User).where(
                User.role == Role.OWNER,
                User.is_active == True,  # noqa: E712
                User.telegram_chat_id.isnot(None),
            )
        )
    )
    if not owners:
        return

    icon = "🔴" if alert_type == "critical" else "🟡"
    days_str = ""
    if item.daily_usage_rate and item.daily_usage_rate > 0:
        days_str = f" (~{int(item.current_stock / item.daily_usage_rate)}d left)"

    threshold = item.critical_point if alert_type == "critical" else item.reorder_point
    msg = (
        f"{icon} Stock alert — {item.name}\n"
        f"On-hand: {item.current_stock:.0f} {item.unit}{days_str}\n"
        f"Below {'critical' if alert_type == 'critical' else 'reorder'} "
        f"point ({threshold:.0f})\n"
        f"→ /po/new?item_id={item.id}"
    )

    for user in owners:
        try:
            send(str(user.telegram_chat_id), msg)
        except Exception:
            logger.exception(
                "notify_stock_alert: failed for user %s item %s", user.id, item.id
            )
