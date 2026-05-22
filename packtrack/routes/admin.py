from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from packtrack.auth import hash_password
from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import Role, SyncRun, User
from packtrack.scheduler import trigger_sync_now
from packtrack.services.scope import distinct_vendors, get_scope, set_scope

router = APIRouter(prefix="/admin")


def _owner_only(user: User) -> None:
    if user.role != Role.OWNER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


@router.get("/users", response_class=HTMLResponse)
def users(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _owner_only(user)
    rows = session.exec(select(User).order_by(User.role, User.name)).all()
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "admin/users.html", {"user": user, "users": rows, "Role": Role}
    )


@router.post("/users")
def create_user(
    name: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    telegram_chat_id: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _owner_only(user)
    email = email.strip().lower()
    if session.exec(select(User).where(User.email == email)).first():
        raise HTTPException(status_code=400, detail="Email already in use.")
    new = User(
        name=name.strip(),
        email=email,
        role=Role(role),
        password_hash=hash_password(password),
        telegram_chat_id=telegram_chat_id.strip() or None,
    )
    session.add(new)
    session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def toggle_user(
    user_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _owner_only(user)
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot disable yourself.")
    target = session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404)
    target.is_active = not target.is_active
    session.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/sync", response_class=HTMLResponse)
def sync_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _owner_only(user)
    runs = session.exec(
        select(SyncRun).order_by(SyncRun.started_at.desc()).limit(10)
    ).all()
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "admin/sync.html",
        {
            "user": user,
            "runs": runs,
            "zoho_configured": settings.zoho_configured,
            "gateway_configured": settings.gateway_configured,
        },
    )


@router.post("/sync/run")
def sync_run(user: User = Depends(require_user)):
    _owner_only(user)
    trigger_sync_now()
    return RedirectResponse(url="/admin/sync", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _owner_only(user)
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "admin/settings.html",
        {
            "user": user,
            "scope": get_scope(session) or "",
            "vendors": distinct_vendors(session),
        },
    )


@router.post("/settings/scope")
def update_scope(
    vendor_scope: str = Form(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    _owner_only(user)
    set_scope(session, vendor_scope, actor_id=user.id)
    return RedirectResponse(url="/admin/settings", status_code=303)


@router.post("/wipe-data")
def wipe_data(
    confirm: str = Form(""),
    user: User = Depends(require_user),
):
    """Owner-only. Drops every PO/line/event/attachment/shipment/item/sync
    log/Zoho mirror. Keeps users + AppSettings (so vendor scope survives).
    Requires typing WIPE in the form to fire."""
    _owner_only(user)
    if confirm.strip() != "WIPE":
        return RedirectResponse(url="/admin/settings?wiped=cancel", status_code=303)
    from packtrack.wipe import wipe as run_wipe
    run_wipe(delete_files=True)
    return RedirectResponse(url="/admin/settings?wiped=ok", status_code=303)
