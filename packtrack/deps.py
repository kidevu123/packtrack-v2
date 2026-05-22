"""FastAPI dependencies."""
from collections.abc import Callable

from fastapi import Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from packtrack.auth import decode_session
from packtrack.config import settings
from packtrack.db import get_session
from packtrack.models import Role, User


def current_user(
    request: Request,
    session: Session = Depends(get_session),
    packtrack_session: str | None = Cookie(default=None, alias=None),
) -> User | None:
    cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
    user_id = decode_session(cookie)
    if not user_id:
        return None
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_user(
    request: Request,
    user: User | None = Depends(current_user),
):
    if user is None:
        # API/htmx callers expect 401; HTML callers should redirect.
        accept = request.headers.get("accept", "")
        hx = request.headers.get("hx-request") == "true"
        if "text/html" in accept and not hx:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": f"/login?next={request.url.path}"},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_role(*roles: Role) -> Callable:
    allowed = {Role(r) for r in roles}

    def _dep(user: User = Depends(require_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return user

    return _dep


def login_redirect_to(path: str) -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={path}", status_code=status.HTTP_303_SEE_OTHER)
