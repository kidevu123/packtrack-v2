"""Notification dispatcher.

Single function call from any route: ``notify(event, po, **ctx)``. Handlers
register themselves at import time. Adding a new channel (email, SMS, Slack)
means writing a handler and registering it — no route changes.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from sqlmodel import Session, select

from packtrack.models import PurchaseOrder, Role, User

if TYPE_CHECKING:
    from packtrack.models import Item
    from packtrack.services.forecast import ForecastRow

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


def notify_stock_alert(session: Session, item: Item, alert_type: str) -> None:
    """Send Telegram stock alert to all active owners when a threshold is crossed.

    alert_type: 'reorder' or 'critical'. Never raises.
    """
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


def notify_forecast_alert(session: Session, row: ForecastRow) -> None:
    """Fire once per restock cycle when reorder_by_sea enters the 7-day window.

    Deduplication: skips if current_stock hasn't risen meaningfully since last alert.
    Never raises.
    """
    from packtrack.telegram import send

    item = row.item

    # Deduplication: if current_stock hasn't risen since last alert, skip.
    # "risen" = current_stock > last_alerted_stock + 10% of reorder_point (hysteresis)
    if item.forecast_alert_sent_stock is not None:
        hysteresis = max(1.0, item.reorder_point * 0.10)
        if item.current_stock <= item.forecast_alert_sent_stock + hysteresis:
            return  # already alerted this restock cycle

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

    days = int(row.days_of_stock) if row.days_of_stock < 9999 else 0
    reorder_str = row.reorder_by_sea.strftime("%b %d") if row.reorder_by_sea else "overdue"
    text = (
        f"🔴 Order Now: {item.name}\n"
        f"On hand: {int(item.current_stock):,} {item.unit}\n"
        f"Days of stock: {days}\n"
        f"Reorder by (sea): {reorder_str}\n"
        f"Suggested: {int(row.suggested_qty):,} {item.unit}\n"
        f"Sea lead: {item.sea_lead_days}d"
    )

    from packtrack.config import settings as _settings
    for user in owners:
        try:
            send(
                str(user.telegram_chat_id),
                text,
                reply_markup={
                    "inline_keyboard": [[
                        {
                            "text": "Create PO",
                            "url": f"{_settings.APP_BASE_URL}/po/new?item_id={item.id}&suggested_qty={int(row.suggested_qty)}",
                        }
                    ]]
                },
            )
        except Exception:
            logger.exception(
                "notify_forecast_alert: failed for user %s item %s", user.id, item.id
            )

    # Mark sent at current stock level so we don't repeat until restocked
    item.forecast_alert_sent_stock = item.current_stock
    session.add(item)
    session.commit()
    logger.info("Forecast alert sent for item %s (stock=%s)", item.material_code, item.current_stock)
