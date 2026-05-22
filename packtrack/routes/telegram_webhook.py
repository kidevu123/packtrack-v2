"""Telegram webhook handler.

Inline buttons + slash commands map back to the same actions the web UI
would take, going through the same workflow gate.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from packtrack.config import settings
from packtrack.db import get_session
from packtrack.models import (
    Attachment,
    AttachmentKind,
    POEvent,
    POStatus,
    PurchaseOrder,
    Role,
    User,
)
from packtrack.notifications import notify
from packtrack.services.workflow import allowed_move
from packtrack.telegram import send

logger = logging.getLogger("packtrack.telegram.webhook")
router = APIRouter()

PENDING_TTL = timedelta(minutes=30)
PENDING_UPLOAD_PI = "upload_pi"
PENDING_UPLOAD_ART = "upload_artwork"
PENDING_REJECT_REASON = "reject_pi_reason"


# ---- helpers --------------------------------------------------------------


def _help_text() -> str:
    return (
        "PackTrack commands\n"
        "/inventory  — show stock\n"
        "/pos        — active POs\n"
        "/upload-pi <PO>     — queue next PDF/photo as PI\n"
        "/upload-artwork <PO>— queue next file as artwork\n"
        "/approve <PO>       — approve art (design) or PI (owner)\n"
        "/reject <PO> <reason>\n"
        "/cancel             — clear pending state\n"
    )


def _po_link(po_id: int) -> str:
    return f"{settings.APP_BASE_URL.rstrip('/')}/po/{po_id}"


def _find_po(session: Session, ident: str) -> PurchaseOrder | None:
    s = (ident or "").strip()
    if not s:
        return None
    if s.isdigit():
        po = session.get(PurchaseOrder, int(s))
        if po:
            return po
    return session.exec(select(PurchaseOrder).where(PurchaseOrder.po_number == s)).first()


def _set_pending(session: Session, user: User, action: str, po_id: int) -> None:
    user.tg_pending_action = action
    user.tg_pending_po_id = po_id
    user.tg_pending_set_at = datetime.utcnow()
    session.add(user)
    session.commit()


def _clear_pending(session: Session, user: User) -> None:
    user.tg_pending_action = None
    user.tg_pending_po_id = None
    user.tg_pending_set_at = None
    session.add(user)
    session.commit()


def _pending_fresh(user: User) -> bool:
    if not user.tg_pending_action or not user.tg_pending_set_at:
        return False
    return datetime.utcnow() - user.tg_pending_set_at <= PENDING_TTL


# ---- action executors (shared with command + button paths) ---------------


def _do_move(session: Session, user: User, po: PurchaseOrder, target: POStatus, reason: str = "") -> tuple[bool, str]:
    ok, err = allowed_move(user, po.status, target)
    if not ok:
        return False, err
    old = po.status
    po.status = target
    po.updated_at = datetime.utcnow()
    session.add(POEvent(
        po_id=po.id, kind="status_change",
        message=f"{old.value} → {target.value} via Telegram by {user.name}"
        + (f" — {reason}" if reason else ""),
        actor_id=user.id,
    ))
    session.commit()

    if old == POStatus.DESIGN_REVIEW and target == POStatus.DESIGN_APPROVED:
        notify(session, "po.design_approved", po)
        return True, f"Art approved for {po.po_number}."
    if old == POStatus.DESIGN_REVIEW and target == POStatus.DESIGN_REJECTED:
        notify(session, "po.design_rejected", po, reason=reason)
        return True, f"Art rejected for {po.po_number}."
    if old == POStatus.PI_RECEIVED and target == POStatus.PI_APPROVED:
        notify(session, "po.pi_approved", po)
        return True, f"PI approved for {po.po_number}. Production."
    if old == POStatus.PI_RECEIVED and target == POStatus.DESIGN_APPROVED:
        notify(session, "po.pi_rejected", po, reason=reason)
        return True, f"PI rejected for {po.po_number}."
    return True, f"{po.po_number}: {old.value} → {target.value}."


def _save_telegram_file(session: Session, user: User, po: PurchaseOrder, blob: bytes,
                       original_name: str, kind: AttachmentKind, caption: str) -> tuple[bool, str]:
    ext = ""
    if "." in (original_name or ""):
        ext = "." + original_name.rsplit(".", 1)[1].lower()
    target_dir = settings.UPLOAD_DIR / kind.value
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = f"tg-{uuid.uuid4().hex}{ext or '.bin'}"
    path = target_dir / safe
    path.write_bytes(blob)
    rel = str(path.relative_to(settings.UPLOAD_DIR))
    rows = session.exec(
        select(Attachment).where(Attachment.po_id == po.id, Attachment.kind == kind)
    ).all()
    version = (max((a.version for a in rows), default=0)) + 1
    session.add(Attachment(
        po_id=po.id, kind=kind, version=version,
        filename=original_name or safe, file_path=rel, source="telegram",
        uploaded_by_id=user.id, notes=caption or None,
    ))
    if kind == AttachmentKind.PI:
        po.status = POStatus.PI_RECEIVED
        po.updated_at = datetime.utcnow()
    session.add(POEvent(
        po_id=po.id, kind="attachment",
        message=f"{kind.value} v{version} via Telegram", actor_id=user.id,
    ))
    session.commit()
    if kind == AttachmentKind.PI:
        notify(session, "po.pi_uploaded", po)
        return True, f"PI saved on {po.po_number}. Owner notified."
    notify(session, "po.artwork_uploaded", po, notes=caption)
    return True, f"Artwork v{version} saved on {po.po_number}."


def _download_file(file_id: str) -> tuple[bytes, str] | None:
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            r.raise_for_status()
            file_path = ((r.json() or {}).get("result") or {}).get("file_path")
            if not file_path:
                return None
            r2 = client.get(
                f"https://api.telegram.org/file/bot{settings.TELEGRAM_BOT_TOKEN}/{file_path}",
            )
            r2.raise_for_status()
            name = file_path.rsplit("/", 1)[-1] if file_path else "telegram-upload"
            return r2.content, name
    except httpx.HTTPError as e:
        logger.warning("Telegram file download: %s", e)
        return None


# ---- inventory / PO list reports -----------------------------------------


def _inventory_report(session: Session) -> str:
    from packtrack.models import Item

    rows = session.exec(select(Item).order_by(Item.name).limit(60)).all()
    if not rows:
        return "No inventory yet — sync Zoho from Admin → Sync."
    lines = ["PackTrack inventory\n"]
    for it in rows:
        days = (
            f"{it.current_stock / it.daily_usage_rate:.0f}d"
            if it.daily_usage_rate and it.daily_usage_rate > 0
            else "—"
        )
        lines.append(f"• {it.name}\n  {it.current_stock:g} {it.unit} | {days} left")
    return "\n".join(lines)


def _pos_report(session: Session) -> str:
    rows = session.exec(
        select(PurchaseOrder)
        .where(PurchaseOrder.status.notin_([POStatus.RECEIVED, POStatus.CANCELLED]))
        .order_by(PurchaseOrder.created_at.desc())
        .limit(20)
    ).all()
    if not rows:
        return "No active POs."
    return "Active POs:\n" + "\n".join(
        f"• {p.po_number} [{p.urgency.value}] {p.status.value}" for p in rows
    )


# ---- webhook entry --------------------------------------------------------


@router.post("/telegram/webhook")
async def webhook(request: Request, session: Session = Depends(get_session)):
    secret = settings.TELEGRAM_WEBHOOK_SECRET.strip()
    if secret and request.headers.get("x-telegram-bot-api-secret-token") != secret:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if not settings.TELEGRAM_BOT_TOKEN:
        return {}

    data = await request.json()
    cbq = data.get("callback_query")
    if cbq:
        _handle_callback(session, cbq)
        return {}
    msg = data.get("message") or data.get("edited_message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        return {}
    _handle_message(session, chat_id, msg)
    return {}


def _user_for_chat(session: Session, chat_id: str | int) -> User | None:
    return session.exec(
        select(User).where(User.telegram_chat_id == str(chat_id), User.is_active == True)  # noqa: E712
    ).first()


def _answer_callback(cb_id: str, text: str = "", alert: bool = False) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={
                    "callback_query_id": cb_id,
                    "text": text[:190],
                    "show_alert": alert,
                },
            )
    except httpx.HTTPError as e:
        logger.warning("answerCallbackQuery: %s", e)


def _handle_callback(session: Session, cbq: dict) -> None:
    cb_id = cbq.get("id")
    from_user = (cbq.get("from") or {}).get("id")
    chat_id = ((cbq.get("message") or {}).get("chat") or {}).get("id")
    raw = (cbq.get("data") or "").split(":")
    user = _user_for_chat(session, str(from_user))
    if user is None:
        _answer_callback(cb_id, "Telegram not linked.", alert=True)
        return
    if len(raw) < 3 or raw[0] != "act":
        _answer_callback(cb_id, "Unknown action.")
        return
    action, po_ident = raw[1], raw[2]
    po = _find_po(session, po_ident)
    if po is None:
        _answer_callback(cb_id, "PO not found.", alert=True)
        return

    if action == "appa":
        ok, m = _do_move(session, user, po, POStatus.DESIGN_APPROVED)
        _answer_callback(cb_id, m, alert=not ok)
        send(str(chat_id), m)
        return
    if action == "flga":
        ok, m = _do_move(session, user, po, POStatus.DESIGN_REJECTED, "Flagged via Telegram.")
        _answer_callback(cb_id, m, alert=not ok)
        send(str(chat_id), m)
        return
    if action == "appi":
        ok, m = _do_move(session, user, po, POStatus.PI_APPROVED)
        _answer_callback(cb_id, m, alert=not ok)
        send(str(chat_id), m)
        return
    if action == "rjpi":
        if user.role != Role.OWNER:
            _answer_callback(cb_id, "Owner only.", alert=True)
            return
        if po.status != POStatus.PI_RECEIVED:
            _answer_callback(cb_id, "PO not awaiting PI approval.", alert=True)
            return
        _set_pending(session, user, PENDING_REJECT_REASON, po.id)
        _answer_callback(cb_id, "Reply with the rejection reason.")
        send(str(chat_id), f"Reply with the reason for rejecting {po.po_number}'s PI.")
        return
    if action == "upi":
        if user.role not in (Role.AGENT, Role.OWNER):
            _answer_callback(cb_id, "Agents only.", alert=True)
            return
        if po.status != POStatus.DESIGN_APPROVED:
            _answer_callback(cb_id, "PO not ready for a PI.", alert=True)
            return
        _set_pending(session, user, PENDING_UPLOAD_PI, po.id)
        _answer_callback(cb_id, "Send the PI now.")
        send(str(chat_id), f"Send the PI for {po.po_number} as a PDF or photo.")
        return
    if action == "uar":
        if user.role not in (Role.DESIGN, Role.OWNER):
            _answer_callback(cb_id, "Design only.", alert=True)
            return
        _set_pending(session, user, PENDING_UPLOAD_ART, po.id)
        _answer_callback(cb_id, "Send the artwork now.")
        send(str(chat_id), f"Send the artwork for {po.po_number}.")
        return
    _answer_callback(cb_id, "Unknown action.")


def _handle_message(session: Session, chat_id: int, msg: dict) -> None:
    from_user = (msg.get("from") or {}).get("id")
    user = _user_for_chat(session, str(from_user)) or _user_for_chat(session, str(chat_id))

    photo = msg.get("photo")
    document = msg.get("document")

    if (photo or document) and user is not None and _pending_fresh(user):
        po = session.get(PurchaseOrder, user.tg_pending_po_id) if user.tg_pending_po_id else None
        if po is None:
            send(str(chat_id), "Pending PO no longer exists. /cancel.")
            _clear_pending(session, user)
            return
        if photo:
            file_id = photo[-1].get("file_id")
            original_name = f"photo-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}.jpeg"
        else:
            file_id = (document or {}).get("file_id")
            original_name = (document or {}).get("file_name") or "telegram-upload"
        if not file_id:
            send(str(chat_id), "Could not read file.")
            return
        result = _download_file(file_id)
        if result is None:
            send(str(chat_id), "Download failed. Try again.")
            return
        blob, _name = result
        caption = (msg.get("caption") or "").strip()
        if user.tg_pending_action == PENDING_UPLOAD_PI:
            ok, m = _save_telegram_file(session, user, po, blob, original_name,
                                        AttachmentKind.PI, caption)
        elif user.tg_pending_action == PENDING_UPLOAD_ART:
            ok, m = _save_telegram_file(session, user, po, blob, original_name,
                                        AttachmentKind.ARTWORK, caption)
        else:
            ok, m = False, "No upload queued. /help."
        send(str(chat_id), m)
        if ok:
            _clear_pending(session, user)
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if user is None:
        send(str(chat_id),
             f"Your Telegram chat ID is {chat_id}.\n"
             "Paste this into Admin → Users to link your account.")
        return

    if user.tg_pending_action == PENDING_REJECT_REASON and _pending_fresh(user) and not text.startswith("/"):
        po = session.get(PurchaseOrder, user.tg_pending_po_id) if user.tg_pending_po_id else None
        if po is None:
            send(str(chat_id), "Pending PO no longer exists.")
        else:
            ok, m = _do_move(session, user, po, POStatus.DESIGN_APPROVED, text)
            send(str(chat_id), m)
        _clear_pending(session, user)
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@", 1)[0]
    rest = parts[1] if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        send(str(chat_id), f"Hi {user.name}.\n\n{_help_text()}")
        return
    if cmd == "/cancel":
        _clear_pending(session, user)
        send(str(chat_id), "Cleared pending state.")
        return
    if cmd in ("/inventory", "/stock"):
        send(str(chat_id), _inventory_report(session))
        return
    if cmd in ("/pos", "/po"):
        send(str(chat_id), _pos_report(session))
        return
    if cmd == "/upload-pi":
        po = _find_po(session, rest)
        if not po:
            send(str(chat_id), "Usage: /upload-pi <PO>")
            return
        if user.role not in (Role.AGENT, Role.OWNER):
            send(str(chat_id), "Agents only.")
            return
        if po.status != POStatus.DESIGN_APPROVED:
            send(str(chat_id), f"{po.po_number} is not awaiting PI ({po.status.value}).")
            return
        _set_pending(session, user, PENDING_UPLOAD_PI, po.id)
        send(str(chat_id), f"Send the PI for {po.po_number} now.")
        return
    if cmd == "/upload-artwork":
        po = _find_po(session, rest)
        if not po:
            send(str(chat_id), "Usage: /upload-artwork <PO>")
            return
        if user.role not in (Role.DESIGN, Role.OWNER):
            send(str(chat_id), "Design only.")
            return
        _set_pending(session, user, PENDING_UPLOAD_ART, po.id)
        send(str(chat_id), f"Send the artwork for {po.po_number} now.")
        return
    if cmd == "/approve":
        po = _find_po(session, rest)
        if not po:
            send(str(chat_id), "Usage: /approve <PO>")
            return
        if user.role == Role.DESIGN and po.status == POStatus.DESIGN_REVIEW:
            _, m = _do_move(session, user, po, POStatus.DESIGN_APPROVED)
        elif user.role == Role.OWNER and po.status == POStatus.PI_RECEIVED:
            _, m = _do_move(session, user, po, POStatus.PI_APPROVED)
        else:
            m = f"Nothing to approve on {po.po_number} ({po.status.value})."
        send(str(chat_id), m)
        return
    if cmd == "/reject":
        sub = rest.split(maxsplit=1)
        po = _find_po(session, sub[0] if sub else "")
        if not po:
            send(str(chat_id), "Usage: /reject <PO> <reason>")
            return
        reason = sub[1] if len(sub) > 1 else "Rejected."
        target = POStatus.DESIGN_APPROVED if po.status == POStatus.PI_RECEIVED else POStatus.DESIGN_REJECTED
        _, m = _do_move(session, user, po, target, reason)
        send(str(chat_id), m)
        return

    send(str(chat_id), _help_text())
