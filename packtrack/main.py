"""FastAPI app factory.

Mount order matters: static files, then routes. Templates is module-level so
route handlers import it (avoids circular imports with main).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from packtrack import __version__, scheduler
# Side-effect: registers the telegram handler with notifications.
from packtrack import telegram  # noqa: F401
from packtrack.config import settings
from packtrack.db import engine
from packtrack.routes import admin, auth, inbox, internal, inventory, purchase_orders, receiving, search, telegram_webhook, webhooks

logger = logging.getLogger("packtrack")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["app_version"] = __version__


def _qty(value) -> str:
    """Render a numeric quantity cleanly: thousands-sep, no trailing .0."""
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "¥", "CAD": "$", "AUD": "$"}


def _money(value, currency: str = "USD") -> str:
    """Render an amount with currency symbol when known, else ISO code prefix."""
    if value is None or value == "":
        return ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    sym = _CURRENCY_SYMBOLS.get((currency or "USD").upper(), "")
    formatted = f"{f:,.2f}"
    return f"{sym}{formatted}" if sym else f"{(currency or '').upper()} {formatted}"


def _initials(name: str | None) -> str:
    """First letter of each whitespace-separated chunk, max 2 chars. Fallback '?'."""
    if not name:
        return "?"
    parts = [p for p in str(name).strip().split() if p]
    if not parts:
        return "?"
    return (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")).upper()


templates.env.filters["qty"] = _qty
templates.env.filters["money"] = _money
templates.env.filters["initials"] = _initials


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(
    title="PackTrack",
    version=__version__,
    docs_url=None,  # operator UI is the docs
    redoc_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def inject_vendor_scope(request: Request, call_next):
    """Stash ``vendor_scope`` on the request so base.html can show the
    nav-bar pill on every page without each route having to pass it."""
    from sqlmodel import Session
    from packtrack.services.scope import get_scope as _get_scope

    try:
        with Session(engine) as s:
            request.state.vendor_scope = _get_scope(s) or ""
    except Exception:
        request.state.vendor_scope = ""
    return await call_next(request)


app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR.parent / "static")),
    name="static",
)
# User-uploaded content (PIs, artwork, item images) lives outside the source
# tree so deploys don't wipe it. Mounted separately so Caddy / nginx could
# serve it directly later if needed.
app.mount(
    "/uploads",
    StaticFiles(directory=str(settings.UPLOAD_DIR)),
    name="uploads",
)


app.include_router(auth.router)
app.include_router(inbox.router)
app.include_router(purchase_orders.router)
app.include_router(inventory.router)
app.include_router(receiving.router)
app.include_router(admin.router)
app.include_router(search.router)
app.include_router(telegram_webhook.router)
app.include_router(internal.router)
app.include_router(webhooks.router)


@app.get("/healthz")
def healthz() -> dict:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db = "ok"
    except Exception as e:
        db = f"error: {e}"
    return {
        "ok": True,
        "version": __version__,
        "db": db,
        "gateway_configured": settings.gateway_configured,
        "zoho_configured": settings.zoho_configured,
        "telegram_configured": settings.telegram_configured,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # 303 from a deps redirect → propagate as a redirect
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    if "text/html" in (request.headers.get("accept") or ""):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "detail": exc.detail or ""},
            status_code=exc.status_code,
        )
    return JSONResponse(
        {"error": exc.detail or "Error"}, status_code=exc.status_code
    )
