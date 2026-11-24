"""Shared pytest setup. pytest loads this file automatically before the tests.

Fixtures (functions marked @pytest.fixture) are created fresh for every test
that lists them as a parameter, e.g. def test_x(client, app).
"""
import os
import sys

import pytest

# Let the tests import app.py and db.py from the project folder.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app  # noqa: E402

# Fixed CSRF token the tests put in the session and send with every POST.
TOKEN = "test-csrf-token"


@pytest.fixture
def app(tmp_path):
    """A fresh app with its own empty database file in a temp folder (tmp_path)."""
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.sqlite3"),
                       "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    """A fake browser for the app, with our CSRF token already in its session."""
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = TOKEN
    return c


def post(client, url, data=None, **kw):
    """POST with the CSRF token attached."""
    data = dict(data or {})
    data.setdefault("csrf_token", TOKEN)
    return client.post(url, data=data, **kw)


# ---- Helpers that perform common steps through the real routes ----
def register_and_login(client, username="alice", password="password123"):
    post(client, "/register", {"username": username, "email": f"{username}@csu.edu", "password": password})
    post(client, "/login", {"username": username, "password": password})


def create_club(client, name="Chess Club"):
    """Create a club and return its id (read from the redirect URL /clubs/<id>)."""
    resp = post(client, "/clubs/create", {"name": name, "description": "test"})
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def category_id(app, club_id, name):
    """Look up a category's id by name, e.g. category_id(app, 1, "Food")."""
    from db import get_db
    with app.app_context():
        return get_db().execute(
            "SELECT category_id FROM categories WHERE club_id=? AND name=?", (club_id, name)
        ).fetchone()[0]


def add_txn(client, club_id, cat, amount, day="2026-10-05", desc=""):
    """Submit the "add transaction" form."""
    return post(client, f"/clubs/{club_id}/transactions/new",
                {"category_id": cat, "amount": amount, "txn_date": day, "description": desc})
