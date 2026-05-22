from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from packtrack.db import get_session
from packtrack.deps import require_user
from packtrack.models import User
from packtrack.services.dashboard import build_dashboard

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    dash = build_dashboard(session, user)
    now = datetime.utcnow()
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "home.html",
        {
            "user": user, "dash": dash,
            "now_ym": now.strftime("%b 1"),
        },
    )
