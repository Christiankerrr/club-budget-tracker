"""Automated tests for Club Budget Tracker. Run with:  python -m pytest -v"""
import pytest

from app import format_cents, parse_amount
from conftest import (TOKEN, add_txn, category_id, create_club, post,
                      register_and_login)
from db import get_db


# Each test sets up data through the real routes, then checks the page or the
# database. Every test gets its own empty database (see conftest.py).

# ---------- unit tests: money helpers ----------
# parametrize runs the same test once for each (input, expected) pair.
@pytest.mark.parametrize("text,cents", [
    ("12.50", 1250), ("$1,200", 120000), ("0.01", 1), ("  7 ", 700), ("1000000", 100000000),
])
def test_parse_amount_valid(text, cents):
    assert parse_amount(text) == cents


@pytest.mark.parametrize("text", [
    "", "abc", "0", "-5", "12.345", "NaN", "Infinity", "1000000.01", "1e400",
])
def test_parse_amount_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_format_cents():
    assert format_cents(123456) == "$1,234.56"
    assert format_cents(-500) == "-$5.00"
    assert format_cents(0) == "$0.00"
    assert format_cents(None) == "$0.00"


# ---------- accounts ----------
def test_register_login_logout(client):
    register_and_login(client)
    assert client.get("/clubs").status_code == 200
    post(client, "/logout")
    assert client.get("/clubs").status_code == 302  # back to login


def test_duplicate_username_rejected(client):
    register_and_login(client)
    post(client, "/logout")
    # Logging out clears the session (and the CSRF token), so put the token back.
    with client.session_transaction() as s:
        s["csrf"] = TOKEN
    resp = post(client, "/register", {"username": "alice", "email": "other@csu.edu",
                                      "password": "password123"}, follow_redirects=True)
    assert b"already registered" in resp.data


def test_short_password_rejected(client):
    resp = post(client, "/register", {"username": "bob", "email": "bob@csu.edu", "password": "short"},
                follow_redirects=True)
    assert b"at least 8 characters" in resp.data


def test_wrong_password(client):
    register_and_login(client)
    post(client, "/logout")
    with client.session_transaction() as s:
        s["csrf"] = TOKEN
    resp = post(client, "/login", {"username": "alice", "password": "wrongpass1"})
    assert b"Incorrect username or password" in resp.data


def test_post_without_csrf_token_is_rejected(client):
    resp = client.post("/register", data={"username": "eve", "email": "e@x.io", "password": "password123"})
    assert resp.status_code == 400


# ---------- clubs ----------
def test_create_club_makes_creator_treasurer_and_seeds_categories(client, app):
    register_and_login(client)
    club_id = create_club(client)
    with app.app_context():
        db = get_db()
        role = db.execute("SELECT role FROM memberships WHERE club_id=?", (club_id,)).fetchone()[0]
        n = db.execute("SELECT COUNT(*) FROM categories WHERE club_id=?", (club_id,)).fetchone()[0]
    assert role == "treasurer"
    assert n == 8


def test_join_club_with_code(client, app):
    register_and_login(client, "alice")
    club_id = create_club(client)
    with app.app_context():
        code = get_db().execute("SELECT join_code FROM clubs WHERE club_id=?", (club_id,)).fetchone()[0]
    post(client, "/logout")
    with client.session_transaction() as s:
        s["csrf"] = TOKEN
    register_and_login(client, "bob")
    resp = post(client, "/clubs/join", {"join_code": code.lower()}, follow_redirects=True)
    assert b"You joined" in resp.data


def test_non_member_cannot_view_club(client):
    register_and_login(client, "alice")
    club_id = create_club(client)
    post(client, "/logout")
    with client.session_transaction() as s:
        s["csrf"] = TOKEN
    register_and_login(client, "mallory")
    assert client.get(f"/clubs/{club_id}").status_code == 404
    assert client.get(f"/clubs/{club_id}/reports.csv").status_code == 404


# ---------- transactions & balance ----------
def test_add_transactions_updates_balance(client, app):
    register_and_login(client)
    club_id = create_club(client)
    add_txn(client, club_id, category_id(app, club_id, "School Funding"), "500.00")
    add_txn(client, club_id, category_id(app, club_id, "Food"), "120.25")
    page = client.get(f"/clubs/{club_id}").data
    assert b"$379.75" in page   # balance = 500.00 - 120.25


def test_invalid_amount_shows_error_and_saves_nothing(client, app):
    register_and_login(client)
    club_id = create_club(client)
    resp = add_txn(client, club_id, category_id(app, club_id, "Food"), "-20")
    assert b"greater than zero" in resp.data
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_cannot_use_category_from_another_club(client, app):
    register_and_login(client)
    club_a = create_club(client, "Club A")
    club_b = create_club(client, "Club B")
    resp = add_txn(client, club_a, category_id(app, club_b, "Food"), "10")
    assert b"valid category" in resp.data


def test_edit_and_delete_transaction(client, app):
    register_and_login(client)
    club_id = create_club(client)
    food = category_id(app, club_id, "Food")
    add_txn(client, club_id, food, "10.00", desc="pizza")
    with app.app_context():
        tid = get_db().execute("SELECT transaction_id FROM transactions").fetchone()[0]
    post(client, f"/clubs/{club_id}/transactions/{tid}/edit",
         {"category_id": food, "amount": "15.00", "txn_date": "2026-10-06", "description": "pizza + soda"})
    with app.app_context():
        row = get_db().execute("SELECT amount_cents, description FROM transactions").fetchone()
    assert (row[0], row[1]) == (1500, "pizza + soda")
    post(client, f"/clubs/{club_id}/transactions/{tid}/delete")
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_member_cannot_edit_someone_elses_transaction(client, app):
    register_and_login(client, "alice")
    club_id = create_club(client)
    add_txn(client, club_id, category_id(app, club_id, "Food"), "10.00")
    with app.app_context():
        db = get_db()
        code = db.execute("SELECT join_code FROM clubs").fetchone()[0]
        tid = db.execute("SELECT transaction_id FROM transactions").fetchone()[0]
    post(client, "/logout")
    with client.session_transaction() as s:
        s["csrf"] = TOKEN
    register_and_login(client, "bob")
    post(client, "/clubs/join", {"join_code": code})
    resp = post(client, f"/clubs/{club_id}/transactions/{tid}/delete")
    assert resp.status_code == 403


# ---------- search & filters ----------
def test_search_and_filters(client, app):
    register_and_login(client)
    club_id = create_club(client)
    add_txn(client, club_id, category_id(app, club_id, "Food"), "30", "2026-09-10", "Pizza night")
    add_txn(client, club_id, category_id(app, club_id, "Travel"), "200", "2026-10-15", "Bus to conference")
    add_txn(client, club_id, category_id(app, club_id, "Donations"), "75", "2026-10-31", "Alumni gift")

    def shown(qs):
        # Load the dashboard with a query string, e.g. shown("q=pizza")
        return client.get(f"/clubs/{club_id}?{qs}").data

    assert b"Pizza night" in shown("q=pizza") and b"Bus to conference" not in shown("q=pizza")
    assert b"Alumni gift" in shown("type=income") and b"Pizza night" not in shown("type=income")
    # date range is inclusive of both end points
    page = shown("start=2026-10-01&end=2026-10-31")
    assert b"Bus to conference" in page and b"Alumni gift" in page and b"Pizza night" not in page
    page = shown("min=50&max=100")
    assert b"Alumni gift" in page and b"Bus to conference" not in page
    # garbage filter values are ignored rather than crashing
    assert client.get(f"/clubs/{club_id}?start=notadate&min=abc").status_code == 200


# ---------- budgets & reports ----------
def test_budget_vs_actual(client, app):
    register_and_login(client)
    club_id = create_club(client)
    food = category_id(app, club_id, "Food")
    post(client, f"/clubs/{club_id}/budgets", {"period": "2026-10", "category_id": food, "amount": "100"})
    add_txn(client, club_id, food, "40", "2026-10-02")
    add_txn(client, club_id, food, "75", "2026-10-20")
    add_txn(client, club_id, food, "999", "2026-11-01")  # different month, must not count
    page = client.get(f"/clubs/{club_id}/budgets?period=2026-10").data
    assert b"$115.00" in page      # spent in October
    assert b"-$15.00" in page      # over budget


def test_budget_must_be_expense_category(client, app):
    register_and_login(client)
    club_id = create_club(client)
    resp = post(client, f"/clubs/{club_id}/budgets",
                {"period": "2026-10", "category_id": category_id(app, club_id, "Donations"), "amount": "10"},
                follow_redirects=True)
    assert b"Choose an expense category" in resp.data


def test_reports_totals_and_date_range(client, app):
    register_and_login(client)
    club_id = create_club(client)
    add_txn(client, club_id, category_id(app, club_id, "Fundraising"), "300", "2026-09-01")
    add_txn(client, club_id, category_id(app, club_id, "Supplies"), "50", "2026-09-15")
    add_txn(client, club_id, category_id(app, club_id, "Events"), "100", "2026-10-01")
    page = client.get(f"/clubs/{club_id}/reports").data
    assert b"$300.00" in page and b"$150.00" in page
    page = client.get(f"/clubs/{club_id}/reports?start=2026-09-01&end=2026-09-30").data
    assert b"$50.00" in page and b"$250.00" in page   # Sept net = 300 - 50


def test_report_with_no_transactions(client):
    register_and_login(client)
    club_id = create_club(client)
    resp = client.get(f"/clubs/{club_id}/reports")
    assert resp.status_code == 200 and b"$0.00" in resp.data


def test_csv_export(client, app):
    register_and_login(client)
    club_id = create_club(client)
    add_txn(client, club_id, category_id(app, club_id, "Food"), "12.5", "2026-10-05", "Snacks")
    resp = client.get(f"/clubs/{club_id}/reports.csv")
    assert resp.mimetype == "text/csv"
    lines = resp.data.decode().strip().splitlines()
    assert lines[0] == "date,type,category,amount,description,entered_by"
    assert lines[1] == "2026-10-05,expense,Food,12.50,Snacks,alice"
