"""Test fixtures.

These tests target pure helpers in ``packtrack/services/material_audit.py``;
they do not need the database, FastAPI app, or environment configuration.
We pre-populate ``DATABASE_URL`` to a placeholder so that any incidental
import of ``packtrack.config`` doesn't error during collection — tests
themselves never open a connection.
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://packtrack:dummy@127.0.0.1:5432/packtrack_test_unused",
)
os.environ.setdefault("PACKTRACK_SECRET_KEY", "test-secret-not-real")
