"""Shared helpers for JSON API blueprints."""

from contextlib import contextmanager

from flask import current_app

from ..database import NianpuDatabase


@contextmanager
def open_db():
    db = NianpuDatabase(current_app.config["DATABASE_PATH"])
    db.connect()
    try:
        yield db
    finally:
        db.close()
