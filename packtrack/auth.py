"""Password hashing + signed-cookie sessions.

argon2id is the modern default for password hashing. We use itsdangerous for
the session cookie because Starlette's SessionMiddleware does the same thing
under the hood and we don't need server-side session storage for 4 users.
"""
from datetime import datetime, timedelta

from itsdangerous import BadSignature, TimestampSigner
from passlib.context import CryptContext

from packtrack.config import settings

pwd_ctx = CryptContext(schemes=["argon2"], deprecated="auto")
_signer = TimestampSigner(settings.PACKTRACK_SECRET_KEY)


def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return pwd_ctx.verify(password, hashed)
    except Exception:
        return False


def encode_session(user_id: int) -> str:
    return _signer.sign(str(user_id)).decode()


def decode_session(cookie: str | None) -> int | None:
    if not cookie:
        return None
    try:
        raw = _signer.unsign(cookie, max_age=settings.SESSION_MAX_AGE_SECONDS)
        return int(raw)
    except (BadSignature, ValueError):
        return None


def session_expiry() -> datetime:
    return datetime.utcnow() + timedelta(seconds=settings.SESSION_MAX_AGE_SECONDS)
