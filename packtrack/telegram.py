"""Telegram sender + message templates.

Inline keyboards on actionable notifications let the user act from the lock
screen. Server-side role checks in routes/telegram_webhook.py are the actual
authority — buttons can't escalate privileges.
"""
from __future__ import annotations

import logging

import httpx

from packtrack.config import settings
from packtrack.models import PurchaseOrder, Role, User
from packtrack.notifications import register

logger = logging.getLogger("packtrack.telegram")

API = "https://api.telegram.org"


def _po_link(po_id: int) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/po/{po_id}"


def send(chat_id: str, text: str, reply_markup: dict | None = None) -> None:
    if not settings.telegram_configured or not chat_id:
        return
    payload: dict = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.post(
                f"{API}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload,
            )
        if r.status_code >= 400:
            logger.warning("Telegram %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def _kbd(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": cb} for label, cb in row]
            for row in rows
        ]
    }


def _send_to(users: list[User], text: str, reply_markup: dict | None = None) -> None:
    for u in users:
        if u.telegram_chat_id:
            send(u.telegram_chat_id, text, reply_markup)


def _items_summary(po: PurchaseOrder) -> str:
    """Pick a summary line for notification text. Long POs collapse to a
    money + count line so messages stay readable on a phone."""
    summary = po.item_summary
    if not summary:
        return "(no lines)"
    if len(summary) <= 3:
        return ", ".join(s["name"] for s in summary)
    head = ", ".join(s["name"] for s in summary[:2])
    extra = len(summary) - 2
    return f"{head} + {extra} more · {len(po.lines)} lines"


@register
def telegram_handler(event: str, po: PurchaseOrder, recipients: list[User], ctx: dict) -> None:
    if not settings.telegram_configured:
        return
    link = _po_link(po.id)
    items = _items_summary(po)

    if event == "po.created":
        urgency = f"[{po.urgency.value.upper()}] " if po.urgency.value != "normal" else ""
        for u in recipients:
            if not u.telegram_chat_id:
                continue
            if u.role == Role.DESIGN:
                kbd = _kbd([
                    [("✅ Approve art", f"act:appa:{po.id}")],
                    [("🎨 Upload artwork", f"act:uar:{po.id}"),
                     ("⚠️ Flag changes", f"act:flga:{po.id}")],
                ])
                send(u.telegram_chat_id,
                     f"PackTrack {urgency}New PO {po.po_number}\n{items}\n{link}",
                     kbd)
            else:
                send(u.telegram_chat_id,
                     f"PackTrack {urgency}New PO {po.po_number}\n{items}\n{link}")
        return

    if event == "po.design_approved":
        kbd = _kbd([[("📄 Upload PI", f"act:upi:{po.id}")]])
        _send_to(recipients,
                 f"PackTrack {po.po_number} — art approved. Upload PI.\n{link}", kbd)
        return

    if event == "po.pi_uploaded":
        kbd = _kbd([
            [("✅ Approve PI", f"act:appi:{po.id}"),
             ("❌ Reject", f"act:rjpi:{po.id}")],
        ])
        _send_to(recipients, f"PackTrack {po.po_number} — PI uploaded.\n{link}", kbd)
        return

    if event == "po.pi_approved":
        _send_to(recipients,
                 f"PackTrack {po.po_number} — PI approved. Production.\n{link}")
        return

    if event == "po.pi_rejected":
        reason = ctx.get("reason", "No reason given.")
        _send_to(recipients,
                 f"PackTrack {po.po_number} — PI rejected.\nReason: {reason}\n{link}")
        return

    if event == "po.design_rejected":
        reason = ctx.get("reason", "No reason given.")
        _send_to(recipients,
                 f"PackTrack {po.po_number} — design rejected.\nReason: {reason}\n{link}")
        return

    if event == "po.art_changes_flagged":
        notes = ctx.get("notes", "")
        _send_to(recipients,
                 f"PackTrack {po.po_number} — art changes flagged.\n{notes}\n{link}")
        return

    if event == "po.production_started":
        _send_to(recipients,
                 f"PackTrack {po.po_number} — production started.\n{link}")
        return

    if event == "po.shipped":
        summary = ctx.get("summary", "")
        _send_to(recipients,
                 f"PackTrack {po.po_number} — shipped.\n{summary}\n{link}")
        return

    if event == "po.received":
        summary = ctx.get("summary", "")
        _send_to(recipients,
                 f"PackTrack {po.po_number} — received.\n{summary}\n{link}")
        return

    if event == "po.discrepancy":
        notes = ctx.get("notes", "")
        _send_to(recipients,
                 f"PackTrack [DISCREPANCY] {po.po_number}\n{notes}\n{link}")
        return

    if event == "po.returned_to_design":
        _send_to(recipients,
                 f"PackTrack {po.po_number} — back in design queue.\n{link}")
        return

    if event == "po.artwork_uploaded":
        notes = ctx.get("notes", "")
        _send_to(recipients,
                 f"PackTrack [ARTWORK] {po.po_number}\n{notes}\n{link}")
        return
