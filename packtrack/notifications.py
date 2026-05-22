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
