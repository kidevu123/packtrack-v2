import base64
import json
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, select

from packtrack.auth import encode_session, verify_password
from packtrack.config import settings
from packtrack.db import get_session
from packtrack.deps import current_user
from packtrack.models import User

router = APIRouter()


def _oidc_signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.PACKTRACK_SECRET_KEY, salt="oidc-state")


# --------------------------------------------------------------------------
# Local form login
# --------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str | None = None,
    user: User | None = Depends(current_user),
):
    if user is not None:
        return RedirectResponse("/", status_code=303)
    from packtrack.main import templates
    return templates.TemplateResponse(
        request, "login.html",
        {"next": next or "/", "oidc_configured": settings.oidc_configured},
    )


@router.post("/login")
def login_submit(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.email == email.strip().lower())).first()
    from packtrack.main import templates
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": next, "error": "Invalid email or password.", "oidc_configured": settings.oidc_configured},
            status_code=401,
        )
    cookie_value = encode_session(user.id)
    redirect = RedirectResponse(url=next or "/", status_code=303)
    redirect.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=cookie_value,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    return redirect


@router.get("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(settings.SESSION_COOKIE_NAME)
    return redirect


# --------------------------------------------------------------------------
# Authentik OIDC SSO
# --------------------------------------------------------------------------

@router.get("/auth/sso")
def sso_start(next: str = "/"):
    """Kick off the Authentik OIDC authorization code flow."""
    if not settings.oidc_configured:
        return RedirectResponse("/login?error=SSO+not+configured", status_code=303)

    nonce = secrets.token_urlsafe(16)
    signed_state = _oidc_signer().dumps({"n": nonce, "next": next})

    params = urlencode({
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": settings.OIDC_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": signed_state,
    })

    # Authentik's authorize endpoint is global — the client_id picks the provider
    redirect = RedirectResponse(
        url=f"http://192.168.1.164:9000/application/o/authorize/?{params}",
        status_code=303,
    )
    # Store signed nonce in a short-lived cookie for CSRF validation
    redirect.set_cookie("_oidc_nonce", nonce, httponly=True, max_age=300, samesite="lax")
    return redirect


@router.get("/auth/callback")
def oidc_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    session: Session = Depends(get_session),
):
    """Authentik redirects here after authentication."""
    from packtrack.main import templates

    def fail(msg: str, status: int = 400):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": msg, "oidc_configured": settings.oidc_configured},
            status_code=status,
        )

    if error:
        return fail(f"SSO error: {error_description or error}", 401)
    if not code or not state:
        return fail("Invalid SSO callback — missing code or state.")

    # Verify signed state (includes nonce + next URL)
    try:
        state_data = _oidc_signer().loads(state, max_age=300)
    except BadSignature:
        return fail("SSO session expired or tampered. Please try again.")

    # Verify nonce cookie matches to prevent CSRF
    cookie_nonce = request.cookies.get("_oidc_nonce", "")
    if not secrets.compare_digest(cookie_nonce, state_data.get("n", "")):
        return fail("SSO state mismatch — possible CSRF. Please try again.")

    next_url = state_data.get("next", "/") or "/"

    # Exchange authorization code for tokens
    token_url = "http://192.168.1.164:9000/application/o/token/"
    try:
        with httpx.Client(timeout=10.0) as client:
            token_resp = client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.OIDC_REDIRECT_URI,
                    "client_id": settings.OIDC_CLIENT_ID,
                    "client_secret": settings.OIDC_CLIENT_SECRET,
                },
            )
    except httpx.HTTPError as exc:
        return fail(f"Could not reach SSO server: {exc}")

    if token_resp.status_code != 200:
        return fail(f"Token exchange failed ({token_resp.status_code}).")

    tokens = token_resp.json()
    id_token = tokens.get("id_token", "")
    if not id_token:
        return fail("No ID token in SSO response.")

    # Decode JWT payload — we trust our own Authentik instance on the local network
    try:
        padded = id_token.split(".")[1]
        padded += "=" * (4 - len(padded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        email = (claims.get("email") or "").strip().lower()
    except Exception:
        return fail("Could not parse SSO identity token.")

    if not email:
        return fail("SSO did not return an email address.")

    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        return fail(f"No PackTrack account found for {email}. Ask an admin to create one.", 403)
    if not user.is_active:
        return fail(f"Account {email} is inactive.", 403)

    # All good — create session
    cookie_value = encode_session(user.id)
    redirect = RedirectResponse(url=next_url, status_code=303)
    redirect.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=cookie_value,
        max_age=settings.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
    )
    redirect.delete_cookie("_oidc_nonce")
    return redirect
