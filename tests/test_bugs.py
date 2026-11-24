"""Regression tests for bugs found while reviewing the AI-generated code.
Each test was written FIRST to reproduce the bug (it failed), then the code was fixed."""
import csv
import io

from conftest import TOKEN, add_txn, category_id, create_club, post, register_and_login


def test_bug1_csv_export_neutralizes_spreadsheet_formulas(client, app):
    """BUG-1: a description like '=HYPERLINK(...)' ran as a formula when the CSV was opened in Excel."""
    register_and_login(client)
    club_id = create_club(client)
    add_txn(client, club_id, category_id(app, club_id, "Food"), "5", "2026-10-05",
            '=HYPERLINK("http://evil.example","Click")')
    rows = list(csv.reader(io.StringIO(client.get(f"/clubs/{club_id}/reports.csv").data.decode())))
    assert rows[1][4].startswith("'=HYPERLINK")   # description cell is now plain text


def test_suspected_open_redirect_is_not_exploitable(client):
    """SUSPECTED (not a bug): next=/\\evil.example passes the startswith('/') check, but
    Werkzeug percent-encodes the backslash in the Location header, so the browser stays on-site."""
    post(client, "/register", {"username": "alice", "email": "a@csu.edu", "password": "password123"})
    resp = post(client, "/login?next=/%5Cevil.example", {"username": "alice", "password": "password123"})
    location = resp.headers["Location"]
    assert location.startswith("/") and not location.startswith(("//", "/\\"))


def test_bug2_usernames_are_case_insensitive(client):
    """BUG-2: 'Alice' could register even though 'alice' existed (impersonation risk)."""
    post(client, "/register", {"username": "alice", "email": "a@csu.edu", "password": "password123"})
    resp = post(client, "/register", {"username": "Alice", "email": "b@csu.edu", "password": "password123"},
                follow_redirects=True)
    assert b"already registered" in resp.data
