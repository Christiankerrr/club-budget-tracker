"""Club Budget Tracker - a Flask web app for student clubs to track their money.

Features: user accounts, create/join clubs, add/edit/delete income and expense
transactions, categories, live balance, monthly budgets, search/filter, and
reports with charts and CSV export.

Run:  flask --app app run --debug
"""
import csv
import io
import os
import secrets
import sqlite3
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (Blueprint, Flask, Response, abort, flash, g, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from db import close_db, get_db, init_db

DEFAULT_CATEGORIES = [
    ("School Funding", "income"), ("Fundraising", "income"),
    ("Donations", "income"), ("Membership Dues", "income"),
    ("Food", "expense"), ("Supplies", "expense"),
    ("Events", "expense"), ("Travel", "expense"),
]
MAX_AMOUNT = Decimal("1000000")  # $1,000,000 upper bound per transaction

bp = Blueprint("main", __name__)


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------
def create_app(test_config=None):
    """Build and configure the Flask app. Tests pass their own config."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        DATABASE=os.path.join(app.instance_path, "budget.sqlite3"),
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)

    app.teardown_appcontext(close_db)
    app.jinja_env.filters["money"] = format_cents
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.register_blueprint(bp)

    with app.app_context():
        init_db()
    return app


# --------------------------------------------------------------------------
# Helpers: money, dates, security
# --------------------------------------------------------------------------
def format_cents(cents):
    """12345 -> '$123.45', -500 -> '-$5.00'. Used as the Jinja 'money' filter."""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def parse_amount(text):
    """Turn user input like '12.50' or '$1,200' into integer cents.

    Raises ValueError with a user-friendly message on bad input.
    """
    cleaned = str(text or "").strip().replace(",", "").lstrip("$")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Amount must be a number, e.g. 12.50.")
    if not value.is_finite():
        raise ValueError("Amount must be a number, e.g. 12.50.")
    if value <= 0:
        raise ValueError("Amount must be greater than zero.")
    if value.as_tuple().exponent < -2:
        raise ValueError("Amount can have at most two decimal places.")
    if value > MAX_AMOUNT:
        raise ValueError("Amount is too large.")
    return int(value * 100)


def parse_date(text):
    """Validate an ISO date string 'YYYY-MM-DD'; return it normalized."""
    try:
        return date.fromisoformat(str(text).strip()).isoformat()
    except ValueError:
        raise ValueError("Date must be in YYYY-MM-DD format.")


def csrf_token():
    """Per-session random token embedded in every form to block CSRF attacks."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@bp.before_app_request
def load_user_and_check_csrf():
    """Load the logged-in user into g.user and reject forged POST requests."""
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT user_id, username, email FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, description="Invalid or missing CSRF token.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_membership(club_id):
    """Return the current user's membership in a club, or 404.

    404 (not 403) so non-members cannot even learn a club ID exists.
    """
    row = get_db().execute(
        """SELECT m.role, c.club_id, c.name, c.description, c.join_code
           FROM memberships m JOIN clubs c ON c.club_id = m.club_id
           WHERE m.user_id = ? AND m.club_id = ?""",
        (g.user["user_id"], club_id),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def require_treasurer(member):
    if member["role"] != "treasurer":
        abort(403, description="Only the club treasurer can do that.")


def club_categories(club_id):
    return get_db().execute(
        "SELECT category_id, name, type FROM categories WHERE club_id = ? ORDER BY type DESC, name",
        (club_id,),
    ).fetchall()


def club_balance(club_id):
    row = get_db().execute(
        "SELECT balance_cents FROM club_balances WHERE club_id = ?", (club_id,)
    ).fetchone()
    return row["balance_cents"] if row else 0


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
@bp.route("/")
def index():
    return redirect(url_for("main.clubs" if g.user else "main.login"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        error = None
        if not 3 <= len(username) <= 30:
            error = "Username must be 3-30 characters."
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "Enter a valid email address."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        if error is None:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                    (username, email, generate_password_hash(password)),
                )
                db.commit()
            except sqlite3.IntegrityError:
                error = "That username or email is already registered."
            else:
                flash("Account created. Please log in.", "success")
                return redirect(url_for("main.login"))
        flash(error, "error")
    return render_template("register.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username or password.", "error")
        else:
            token = session.get("csrf")
            session.clear()                 # prevent session fixation
            session["user_id"] = user["user_id"]
            if token:
                session["csrf"] = token
            nxt = request.args.get("next", "")
            # Only follow local paths to avoid an open-redirect.
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("main.clubs"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# --------------------------------------------------------------------------
# Clubs: list, create, join
# --------------------------------------------------------------------------
@bp.route("/clubs")
@login_required
def clubs():
    rows = get_db().execute(
        """SELECT c.club_id, c.name, c.description, m.role, b.balance_cents
           FROM memberships m
           JOIN clubs c ON c.club_id = m.club_id
           JOIN club_balances b ON b.club_id = c.club_id
           WHERE m.user_id = ? ORDER BY c.name""",
        (g.user["user_id"],),
    ).fetchall()
    return render_template("clubs.html", clubs=rows)


@bp.route("/clubs/create", methods=["POST"])
@login_required
def create_club():
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not 2 <= len(name) <= 80:
        flash("Club name must be 2-80 characters.", "error")
        return redirect(url_for("main.clubs"))
    db = get_db()
    for _ in range(5):  # retry on the (very unlikely) join-code collision
        code = secrets.token_hex(3).upper()
        try:
            cur = db.execute(
                "INSERT INTO clubs (name, description, join_code, created_by) VALUES (?, ?, ?, ?)",
                (name, description, code, g.user["user_id"]),
            )
            break
        except sqlite3.IntegrityError:
            continue
    else:
        abort(500)
    club_id = cur.lastrowid
    db.execute(
        "INSERT INTO memberships (user_id, club_id, role) VALUES (?, ?, 'treasurer')",
        (g.user["user_id"], club_id),
    )
    db.executemany(
        "INSERT INTO categories (club_id, name, type) VALUES (?, ?, ?)",
        [(club_id, n, t) for n, t in DEFAULT_CATEGORIES],
    )
    db.commit()
    flash(f"Club created. Share join code {code} with your members.", "success")
    return redirect(url_for("main.club_dashboard", club_id=club_id))


@bp.route("/clubs/join", methods=["POST"])
@login_required
def join_club():
    code = request.form.get("join_code", "").strip().upper()
    db = get_db()
    club = db.execute("SELECT club_id, name FROM clubs WHERE join_code = ?", (code,)).fetchone()
    if club is None:
        flash("No club found with that join code.", "error")
        return redirect(url_for("main.clubs"))
    try:
        db.execute(
            "INSERT INTO memberships (user_id, club_id, role) VALUES (?, ?, 'member')",
            (g.user["user_id"], club["club_id"]),
        )
        db.commit()
        flash(f"You joined {club['name']}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already a member of that club.", "info")
    return redirect(url_for("main.club_dashboard", club_id=club["club_id"]))


# --------------------------------------------------------------------------
# Club dashboard: balance + searchable / filterable transaction list
# --------------------------------------------------------------------------
def build_transaction_filters(club_id, args):
    """Translate query-string filters into a parameterized WHERE clause.

    Invalid filter values are ignored instead of crashing the page.
    """
    clauses, params = ["t.club_id = ?"], [club_id]
    q = args.get("q", "").strip()
    if q:
        clauses.append("(t.description LIKE ? OR c.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if args.get("type") in ("income", "expense"):
        clauses.append("c.type = ?")
        params.append(args["type"])
    if args.get("category", "").isdigit():
        clauses.append("t.category_id = ?")
        params.append(int(args["category"]))
    for key, op in (("start", ">="), ("end", "<=")):
        try:
            clauses.append(f"t.txn_date {op} ?")
            params.append(parse_date(args[key]))
        except (KeyError, ValueError):
            clauses.pop()
    for key, op in (("min", ">="), ("max", "<=")):
        try:
            clauses.append(f"t.amount_cents {op} ?")
            params.append(parse_amount(args[key]))
        except (KeyError, ValueError):
            clauses.pop()
    return " AND ".join(clauses), params


@bp.route("/clubs/<int:club_id>")
@login_required
def club_dashboard(club_id):
    member = get_membership(club_id)
    where, params = build_transaction_filters(club_id, request.args)
    db = get_db()
    txns = db.execute(
        f"""SELECT t.transaction_id, t.amount_cents, t.txn_date, t.description,
                   t.created_by, c.name AS category, c.type, u.username
            FROM transactions t
            JOIN categories c ON c.category_id = t.category_id
            LEFT JOIN users u ON u.user_id = t.created_by
            WHERE {where}
            ORDER BY t.txn_date DESC, t.transaction_id DESC""",
        params,
    ).fetchall()
    totals = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN c.type='income'  THEN t.amount_cents END), 0) AS income,
                  COALESCE(SUM(CASE WHEN c.type='expense' THEN t.amount_cents END), 0) AS expense
           FROM transactions t JOIN categories c ON c.category_id = t.category_id
           WHERE t.club_id = ?""",
        (club_id,),
    ).fetchone()
    members = db.execute(
        """SELECT u.username, m.role FROM memberships m JOIN users u ON u.user_id = m.user_id
           WHERE m.club_id = ? ORDER BY m.role DESC, u.username""",
        (club_id,),
    ).fetchall()
    return render_template(
        "club.html", club=member, txns=txns, totals=totals,
        balance=club_balance(club_id), categories=club_categories(club_id),
        members=members, filters=request.args,
    )


# --------------------------------------------------------------------------
# Transactions: create / edit / delete
# --------------------------------------------------------------------------
def read_transaction_form(club_id):
    """Validate the transaction form. Returns (values_dict, error_message)."""
    form = request.form
    try:
        amount = parse_amount(form.get("amount"))
        txn_date = parse_date(form.get("txn_date"))
    except ValueError as exc:
        return None, str(exc)
    category = get_db().execute(
        "SELECT category_id FROM categories WHERE category_id = ? AND club_id = ?",
        (form.get("category_id", type=int), club_id),
    ).fetchone()
    if category is None:
        return None, "Choose a valid category."
    description = form.get("description", "").strip()[:200]
    return {"amount": amount, "txn_date": txn_date,
            "category_id": category["category_id"], "description": description}, None


def get_editable_transaction(club_id, txn_id, member):
    """Fetch a transaction the current user may change (owner or treasurer)."""
    txn = get_db().execute(
        "SELECT * FROM transactions WHERE transaction_id = ? AND club_id = ?",
        (txn_id, club_id),
    ).fetchone()
    if txn is None:
        abort(404)
    if member["role"] != "treasurer" and txn["created_by"] != g.user["user_id"]:
        abort(403, description="You can only change transactions you created.")
    return txn


@bp.route("/clubs/<int:club_id>/transactions/new", methods=["GET", "POST"])
@login_required
def new_transaction(club_id):
    member = get_membership(club_id)
    if request.method == "POST":
        values, error = read_transaction_form(club_id)
        if error:
            flash(error, "error")
        else:
            db = get_db()
            db.execute(
                """INSERT INTO transactions (club_id, category_id, created_by, amount_cents, txn_date, description)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (club_id, values["category_id"], g.user["user_id"],
                 values["amount"], values["txn_date"], values["description"]),
            )
            db.commit()
            flash("Transaction added.", "success")
            return redirect(url_for("main.club_dashboard", club_id=club_id))
    return render_template("transaction_form.html", club=member, txn=None,
                           categories=club_categories(club_id), today=date.today().isoformat())


@bp.route("/clubs/<int:club_id>/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(club_id, txn_id):
    member = get_membership(club_id)
    txn = get_editable_transaction(club_id, txn_id, member)
    if request.method == "POST":
        values, error = read_transaction_form(club_id)
        if error:
            flash(error, "error")
        else:
            db = get_db()
            db.execute(
                """UPDATE transactions SET category_id = ?, amount_cents = ?, txn_date = ?, description = ?
                   WHERE transaction_id = ? AND club_id = ?""",
                (values["category_id"], values["amount"], values["txn_date"],
                 values["description"], txn_id, club_id),
            )
            db.commit()
            flash("Transaction updated.", "success")
            return redirect(url_for("main.club_dashboard", club_id=club_id))
    return render_template("transaction_form.html", club=member, txn=txn,
                           categories=club_categories(club_id), today=date.today().isoformat())


@bp.route("/clubs/<int:club_id>/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(club_id, txn_id):
    member = get_membership(club_id)
    get_editable_transaction(club_id, txn_id, member)
    db = get_db()
    db.execute("DELETE FROM transactions WHERE transaction_id = ? AND club_id = ?", (txn_id, club_id))
    db.commit()
    flash("Transaction deleted.", "success")
    return redirect(url_for("main.club_dashboard", club_id=club_id))


# --------------------------------------------------------------------------
# Categories and budgets (treasurer only for changes)
# --------------------------------------------------------------------------
@bp.route("/clubs/<int:club_id>/categories", methods=["POST"])
@login_required
def add_category(club_id):
    member = get_membership(club_id)
    require_treasurer(member)
    name = request.form.get("name", "").strip()
    ctype = request.form.get("type")
    if not 1 <= len(name) <= 40 or ctype not in ("income", "expense"):
        flash("Enter a category name and choose income or expense.", "error")
    else:
        try:
            db = get_db()
            db.execute("INSERT INTO categories (club_id, name, type) VALUES (?, ?, ?)",
                       (club_id, name, ctype))
            db.commit()
            flash(f"Category '{name}' added.", "success")
        except sqlite3.IntegrityError:
            flash("That category already exists.", "error")
    return redirect(url_for("main.budgets", club_id=club_id))


@bp.route("/clubs/<int:club_id>/budgets", methods=["GET", "POST"])
@login_required
def budgets(club_id):
    member = get_membership(club_id)
    period = request.values.get("period") or date.today().strftime("%Y-%m")
    try:
        date.fromisoformat(period + "-01")
    except ValueError:
        period = date.today().strftime("%Y-%m")
    db = get_db()
    if request.method == "POST":
        require_treasurer(member)
        category_id = request.form.get("category_id", type=int)
        cat = db.execute(
            "SELECT category_id FROM categories WHERE category_id = ? AND club_id = ? AND type = 'expense'",
            (category_id, club_id),
        ).fetchone()
        try:
            if cat is None:
                raise ValueError("Choose an expense category.")
            amount = parse_amount(request.form.get("amount"))
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            # UPSERT: one budget per category per month.
            db.execute(
                """INSERT INTO budgets (club_id, category_id, period, amount_cents) VALUES (?, ?, ?, ?)
                   ON CONFLICT (club_id, category_id, period) DO UPDATE SET amount_cents = excluded.amount_cents""",
                (club_id, category_id, period, amount),
            )
            db.commit()
            flash("Budget saved.", "success")
        return redirect(url_for("main.budgets", club_id=club_id, period=period))
    rows = budget_vs_actual(club_id, period)
    return render_template("budgets.html", club=member, period=period, rows=rows,
                           categories=club_categories(club_id))


def budget_vs_actual(club_id, period):
    """For each expense category: budget for the month vs. actual spending."""
    return get_db().execute(
        """SELECT c.category_id, c.name,
                  b.amount_cents AS budget,
                  COALESCE((SELECT SUM(t.amount_cents) FROM transactions t
                            WHERE t.category_id = c.category_id
                              AND substr(t.txn_date, 1, 7) = ?), 0) AS spent
           FROM categories c
           LEFT JOIN budgets b ON b.category_id = c.category_id AND b.period = ?
           WHERE c.club_id = ? AND c.type = 'expense'
           ORDER BY c.name""",
        (period, period, club_id),
    ).fetchall()


# --------------------------------------------------------------------------
# Reports: totals, per-category, per-month, CSV export
# --------------------------------------------------------------------------
def report_data(club_id, start, end):
    """Compute all report numbers for an optional date range."""
    where, params = "t.club_id = ?", [club_id]
    if start:
        where += " AND t.txn_date >= ?"
        params.append(start)
    if end:
        where += " AND t.txn_date <= ?"
        params.append(end)
    db = get_db()
    totals = db.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN c.type='income'  THEN t.amount_cents END), 0) AS income,
                   COALESCE(SUM(CASE WHEN c.type='expense' THEN t.amount_cents END), 0) AS expense
            FROM transactions t JOIN categories c ON c.category_id = t.category_id WHERE {where}""",
        params,
    ).fetchone()
    by_category = db.execute(
        f"""SELECT c.name, c.type, SUM(t.amount_cents) AS total, COUNT(*) AS n
            FROM transactions t JOIN categories c ON c.category_id = t.category_id
            WHERE {where} GROUP BY c.category_id ORDER BY c.type, total DESC""",
        params,
    ).fetchall()
    by_month = db.execute(
        f"""SELECT substr(t.txn_date, 1, 7) AS month,
                   SUM(CASE WHEN c.type='income'  THEN t.amount_cents ELSE 0 END) AS income,
                   SUM(CASE WHEN c.type='expense' THEN t.amount_cents ELSE 0 END) AS expense
            FROM transactions t JOIN categories c ON c.category_id = t.category_id
            WHERE {where} GROUP BY month ORDER BY month""",
        params,
    ).fetchall()
    return totals, by_category, by_month


def csv_safe(value):
    """Stop CSV/formula injection: Excel runs cells starting with = + - @ as formulas.

    Prefixing a single quote makes the spreadsheet show the text literally.
    """
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def read_report_range():
    start = end = ""
    try:
        if request.args.get("start"):
            start = parse_date(request.args["start"])
        if request.args.get("end"):
            end = parse_date(request.args["end"])
    except ValueError as exc:
        flash(str(exc), "error")
    return start, end


@bp.route("/clubs/<int:club_id>/reports")
@login_required
def reports(club_id):
    member = get_membership(club_id)
    start, end = read_report_range()
    totals, by_category, by_month = report_data(club_id, start, end)
    expense_rows = [r for r in by_category if r["type"] == "expense"]
    chart = {
        "categoryLabels": [r["name"] for r in expense_rows],
        "categoryValues": [r["total"] / 100 for r in expense_rows],
        "months": [r["month"] for r in by_month],
        "monthIncome": [r["income"] / 100 for r in by_month],
        "monthExpense": [r["expense"] / 100 for r in by_month],
    }
    return render_template("reports.html", club=member, start=start, end=end, totals=totals,
                           by_category=by_category, by_month=by_month, chart=chart,
                           balance=club_balance(club_id))


@bp.route("/clubs/<int:club_id>/reports.csv")
@login_required
def reports_csv(club_id):
    get_membership(club_id)
    start, end = read_report_range()
    where, params = "t.club_id = ?", [club_id]
    if start:
        where += " AND t.txn_date >= ?"
        params.append(start)
    if end:
        where += " AND t.txn_date <= ?"
        params.append(end)
    rows = get_db().execute(
        f"""SELECT t.txn_date, c.type, c.name, t.amount_cents, t.description, u.username
            FROM transactions t JOIN categories c ON c.category_id = t.category_id
            LEFT JOIN users u ON u.user_id = t.created_by
            WHERE {where} ORDER BY t.txn_date""",
        params,
    ).fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "type", "category", "amount", "description", "entered_by"])
    for r in rows:
        writer.writerow([r["txn_date"], r["type"], csv_safe(r["name"]), f"{r['amount_cents'] / 100:.2f}",
                         csv_safe(r["description"]), csv_safe(r["username"])])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=club-{club_id}-report.csv"})


if __name__ == "__main__":
    create_app().run(debug=True)
