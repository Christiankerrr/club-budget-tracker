"""Database helpers: one SQLite connection per request, stored on flask.g."""
import sqlite3

from flask import current_app, g


def get_db():
    """Return the request's SQLite connection, opening it on first use."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row          # rows behave like dicts: row["name"]
        g.db.execute("PRAGMA foreign_keys = ON")  # SQLite leaves FKs off by default
    return g.db


def close_db(_exc=None):
    """Close the connection at the end of the request (registered as teardown)."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create all tables/views. Safe to run repeatedly (IF NOT EXISTS)."""
    db = get_db()
    with current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf-8"))
    db.commit()
