# Club Budget Tracker

A web-based budget tracker for student clubs and organizations (CPSC 332 project).
Clubs track their income, expenses, budgets, and balance in one place.

**Stack:** Python 3 · Flask 3 · SQLite · Jinja2 templates · Chart.js · pytest

## Features
- Register / log in (passwords hashed with Werkzeug), CSRF protection on every form
- Create a club (you become **treasurer**) or join one with a 6-character join code (**member**)
- Add, edit, delete income and expense transactions; members can change only their own, the treasurer can change any
- Categories (8 defaults per club + custom ones); each category is income or expense
- Live balance = income − expenses (computed by the `club_balances` SQL view)
- Search and filter transactions by text, type, category, date range, and amount range
- Monthly budgets per expense category, with budget vs. actual and progress bars
- Reports: income, expenses, net, funds left, expenses by category, income vs. expenses by month (charts), CSV export

## Run it
```bash
pip install -r requirements.txt
python seed.py                      # optional demo data
flask --app app run --debug         # open http://127.0.0.1:5000
```
Demo logins created by `seed.py` (test data only): `treasurer` / `demo12345` and `member` / `demo12345`.

## Test it
```bash
python -m pytest -v
```

## Project layout
| File | Purpose |
|---|---|
| `app.py` | Flask app factory, all routes, validation, reports |
| `db.py` | SQLite connection per request, schema initialization |
| `schema.sql` | Tables, constraints, indexes, `club_balances` view |
| `templates/` | Jinja2 pages (dashboard, transaction form, budgets, reports) |
| `static/style.css` | Styling |
| `seed.py` | Demo data |
| `tests/` | pytest suite (feature tests + bug regression tests) |

## Database schema
`users` 1─* `memberships` *─1 `clubs` 1─* `categories` 1─* `transactions`, and `clubs` 1─* `budgets` *─1 `categories`.
Money is stored as integer cents. A composite foreign key `(category_id, club_id)` guarantees a transaction's
category belongs to the same club.
