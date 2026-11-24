import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app  # noqa: E402

TOKEN = "test-csrf-token"


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.sqlite3"),
                       "SECRET_KEY": "test"})


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["csrf"] = TOKEN
    return c


def post(client, url, data=None, **kw):
    """POST with the CSRF token attached."""
    data = dict(data or {})
    data.setdefault("csrf_token", TOKEN)
    return client.post(url, data=data, **kw)


def register_and_login(client, username="alice", password="password123"):
    post(client, "/register", {"username": username, "email": f"{username}@csu.edu", "password": password})
    post(client, "/login", {"username": username, "password": password})


def create_club(client, name="Chess Club"):
    resp = post(client, "/clubs/create", {"name": name, "description": "test"})
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def category_id(app, club_id, name):
    from db import get_db
    with app.app_context():
        return get_db().execute(
            "SELECT category_id FROM categories WHERE club_id=? AND name=?", (club_id, name)
        ).fetchone()[0]


def add_txn(client, club_id, cat, amount, day="2026-10-05", desc=""):
    return post(client, f"/clubs/{club_id}/transactions/new",
                {"category_id": cat, "amount": amount, "txn_date": day, "description": desc})
