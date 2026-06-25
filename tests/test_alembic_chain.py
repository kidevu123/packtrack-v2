"""Lock the alembic migration chain to a single head + linear ancestry.

If a future change accidentally creates a divergent branch (two heads),
this test fails before deploy.
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:5432/x")
os.environ.setdefault("PACKTRACK_SECRET_KEY", "x")

from alembic.config import Config
from alembic.script import ScriptDirectory


def _script_dir() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_alembic_has_a_single_head():
    heads = _script_dir().get_heads()
    assert len(heads) == 1, f"expected one head, got {heads!r}"


def test_alembic_chain_includes_the_v2_4_1_receiving_idempotency_migration():
    sd = _script_dir()
    chain_revisions = {r.revision for r in sd.walk_revisions()}
    assert "3c8a2b1e9d40" in chain_revisions, (
        "Expected the v2.4.1 receiving-idempotency migration in the chain. "
        "If you renamed/removed it, update this test."
    )


def test_alembic_chain_is_linear():
    """No revision should have a branching parent (down_revision must be a
    single revision id or None)."""
    sd = _script_dir()
    for r in sd.walk_revisions():
        assert not isinstance(r.down_revision, tuple) or len(r.down_revision) <= 1, (
            f"revision {r.revision} has multiple parents {r.down_revision!r}"
        )
