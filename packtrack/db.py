"""Sync SQLAlchemy session via SQLModel.

Sync (not async) keeps the codebase simple — this is a 4-user app and the
hot path is database, not I/O concurrency. FastAPI handles sync routes on
its threadpool. Async would force every dependency into the async ecosystem
for no performance gain at this scale.
"""
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlmodel import Session

from packtrack.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False,
)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
