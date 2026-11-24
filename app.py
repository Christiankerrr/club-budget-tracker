"""Club Budget Tracker - a Flask web app for student clubs to track their money.

Features: user accounts, create/join clubs, add/edit/delete income and expense
transactions, categories, live balance, monthly budgets, search/filter, and
reports with charts and CSV export.

How a request flows through this file:
    browser  ->  route function (@bp.route)  ->  SQL via get_db()  ->  render_template()
Every route that touches a club first calls get_membership() so users can only
see clubs they belong to.

Run:  flask --app app run --debug
"""
# ---- Standard-library imports ----
import csv                      # writes the CSV report export
import io                       # in-memory text buffer for building the CSV
import os                       # file paths and environment variables
import secrets                  # cryptographically secure random tokens (join codes, CSRF)
import sqlite3                  # the database driver (used here for IntegrityError)
from datetime import date       # today's date and date validation
from decimal import Decimal, InvalidOperation  # exact decimal math for money input
from functools import wraps     # keeps a view's name when we wrap it in a decorator

# ---- Third-party imports (Flask and its helper library Werkzeug) ----
from flask import (Blueprint, Flask, Response, abort, flash, g, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

# ---- Our own database helpers (db.py) ----
from db import close_db, get_db, init_db

# Categories every new club starts with: (name, "income" or "expense").
DEFAULT_CATEGORIES = [
    ("School Funding", "income"), ("Fundraising", "income"),
    ("Donations", "income"), ("Membership Dues", "income"),
    ("Food", "expense"), ("Supplies", "expense"),
    ("Events", "expense"), ("Travel", "expense"),
]
MAX_AMOUNT = Decimal("1000000")  # $1,000,000 upper bound per transaction

# A Blueprint is a group of routes. All routes below are registered on "bp",
# so in templates/redirects they are referred to as "main.<function_name>".
bp = Blueprint("main", __name__)


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------
def create_app(test_config=None):
    """Build and configure the Flask app. Tests pass their own config."""
    # instance_relative_config puts the database in the "instance/" folder,
    # which is ignored by git so real data never gets committed.
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        # SECRET_KEY signs the session cookie. Set a real one in production.
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-change-me"),
        DATABASE=os.path.join(app.instance_path, "budget.sqlite3"),
    )
    if test_config:
        # Tests override settings, e.g. point DATABASE at a temporary file.
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)

    app.teardown_appcontext(close_db)                     # close the DB after every request
    app.jinja_env.filters["money"] = format_cents         # lets templates write {{ cents|money }}
    app.jinja_env.globals["csrf_token"] = csrf_token      # lets templates call {{ csrf_token() }}
    app.register_blueprint(bp)                            # attach all the routes below

    # Create the tables on startup if they don't exist yet.
    with app.app_context():
        init_db()
    return app


# --------------------------------------------------------------------------
# Helpers: money, dates, security
# --------------------------------------------------------------------------
def format_cents(cents):
    """12345 -> '$123.45', -500 -> '-$5.00'. Used as the Jinja 'money' filter."""
    cents = int(cents or 0)                # None (no rows) counts as 0
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    # cents // 100 = dollars (with thousands commas), cents % 100 = leftover cents (2 digits)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def parse_amount(text):
    """Turn user input like '12.50' or '$1,200' into integer cents.

    Raises ValueError with a user-friendly message on bad input.
    Money is stored as whole cents because floats can't hold 0.10 exactly.
    """
    # Strip spaces, thousands commas and a leading dollar sign.
    cleaned = str(text or "").strip().replace(",", "").lstrip("$")
    try:
        value = Decimal(cleaned)           # Decimal is exact, unlike float
    except InvalidOperation:
        raise ValueError("Amount must be a number, e.g. 12.50.")
    # Decimal accepts "NaN" and "Infinity"; reject them BEFORE comparing,
    # because comparing NaN raises an error instead of returning False.
    if not value.is_finite():
        raise ValueError("Amount must be a number, e.g. 12.50.")
    if value <= 0:
        raise ValueError("Amount must be greater than zero.")
    # exponent -2 means two decimal places; -3 or lower means too many (e.g. 12.345).
    if value.as_tuple().exponent < -2:
        raise ValueError("Amount can have at most two decimal places.")
    if value > MAX_AMOUNT:
        raise ValueError("Amount is too large.")
    return int(value * 100)                # dollars -> cents


def parse_date(text):
    """Validate an ISO date string 'YYYY-MM-DD'; return it normalized.

    Dates are stored as 'YYYY-MM-DD' text so they sort correctly and
    substr(date, 1, 7) gives the month ('YYYY-MM') in SQL.
    """
    try:
        return date.fromisoformat(str(text).strip()).isoformat()
    except ValueError:
        raise ValueError("Date must be in YYYY-MM-DD format.")


def csrf_token():
    """Per-session random token embedded in every form to block CSRF attacks.

    A forged form on another website can't know this token, so its POST is rejected.
    """
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@bp.before_app_request
def load_user_and_check_csrf():
    """Runs before EVERY request: load the logged-in user and reject forged POSTs."""
    # session is a signed cookie; after login it holds the user's id.
    user_id = session.get("user_id")
    g.user = None                          # g = storage that lasts for one request
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT user_id, username, email FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if request.method == "POST":
        # Every form includes a hidden csrf_token field; it must match the session.
        sent = request.form.get("csrf_token", "")
        # compare_digest compares in constant time so the token can't be guessed by timing.
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, description="Invalid or missing CSRF token.")


def login_required(view):
    """Decorator: put @login_required on a route to send logged-out users to the login page."""
    @wraps(view)                           # keep the original function name for url_for()
    def wrapped(*args, **kwargs):
        if g.user is None:
            # Remember where they were going so login can send them back.
            return redirect(url_for("main.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def get_membership(club_id):
    """Return the current user's membership in a club, or 404.

    404 (not 403) so non-members cannot even learn a club ID exists.
    The returned row has both the user's role and the club's details.
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
    """Stop the request with 403 Forbidden unless the user is the club's treasurer."""
    if member["role"] != "treasurer":
        abort(403, description="Only the club treasurer can do that.")


def club_categories(club_id):
    """All categories of a club, income first (type DESC), then alphabetical."""
    return get_db().execute(
        "SELECT category_id, name, type FROM categories WHERE club_id = ? ORDER BY type DESC, name",
        (club_id,),
    ).fetchall()


def club_balance(club_id):
    """Current balance in cents, read from the club_balances VIEW (income - expenses)."""
    row = get_db().execute(
        "SELECT balance_cents FROM club_balances WHERE club_id = ?", (club_id,)
    ).fetchone()
    return row["balance_cents"] if row else 0


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
@bp.route("/")
def index():
    """Home page: logged-in users go to their clubs, everyone else to login."""
    return redirect(url_for("main.clubs" if g.user else "main.login"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    """GET shows the sign-up form; POST validates it and creates the account."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        # Validate the fields one at a time; stop at the first problem.
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
                # Never store the real password - only a salted hash of it.
                db.execute(
                    "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                    (username, email, generate_password_hash(password)),
                )
                db.commit()
            except sqlite3.IntegrityError:
                # The UNIQUE constraint on username/email was violated.
                error = "That username or email is already registered."
            else:
                flash("Account created. Please log in.", "success")
                return redirect(url_for("main.login"))
        flash(error, "error")              # show the message on the re-rendered form
    return render_template("register.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    """GET shows the login form; POST checks the password and starts a session."""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        # Same message for "no such user" and "wrong password" so attackers
        # can't find out which usernames exist.
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect username or password.", "error")
        else:
            token = session.get("csrf")
            session.clear()                 # prevent session fixation
            session["user_id"] = user["user_id"]   # this is what "logged in" means
            if token:
                session["csrf"] = token     # keep the CSRF token so open forms still work
            nxt = request.args.get("next", "")
            # Only follow local paths to avoid an open-redirect.
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("main.clubs"))
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    """Log out (POST, so it's protected by the CSRF check)."""
    session.clear()
    return redirect(url_for("main.login"))


# --------------------------------------------------------------------------
# Clubs: list, create, join
# --------------------------------------------------------------------------
@bp.route("/clubs")
@login_required
def clubs():
    """'My Clubs' page: every club the user belongs to, with its balance."""
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
    """Create a club: the creator becomes treasurer and default categories are added."""
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not 2 <= len(name) <= 80:
        flash("Club name must be 2-80 characters.", "error")
        return redirect(url_for("main.clubs"))
    db = get_db()
    # Join code = 6 random hex characters, e.g. "3FA9C1". It must be unique,
    # so if we randomly pick one that's taken, try again (up to 5 times).
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
        # for/else: this runs only if the loop never hit "break".
        abort(500)
    club_id = cur.lastrowid                # the id SQLite gave the new club
    db.execute(
        "INSERT INTO memberships (user_id, club_id, role) VALUES (?, ?, 'treasurer')",
        (g.user["user_id"], club_id),
    )
    # executemany runs the same INSERT once per default category.
    db.executemany(
        "INSERT INTO categories (club_id, name, type) VALUES (?, ?, ?)",
        [(club_id, n, t) for n, t in DEFAULT_CATEGORIES],
    )
    db.commit()                            # save club + membership + categories together
    flash(f"Club created. Share join code {code} with your members.", "success")
    return redirect(url_for("main.club_dashboard", club_id=club_id))


@bp.route("/clubs/join", methods=["POST"])
@login_required
def join_club():
    """Join an existing club by its join code (as a regular member)."""
    code = request.form.get("join_code", "").strip().upper()   # codes are case-insensitive
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
        # The (user_id, club_id) primary key already exists.
        flash("You are already a member of that club.", "info")
    return redirect(url_for("main.club_dashboard", club_id=club["club_id"]))


# --------------------------------------------------------------------------
# Club dashboard: balance + searchable / filterable transaction list
# --------------------------------------------------------------------------
def build_transaction_filters(club_id, args):
    """Translate query-string filters into a parameterized WHERE clause.

    Invalid filter values are ignored instead of crashing the page.
    Returns (where_sql, params). User values ONLY go into params (bound to "?"),
    never into the SQL text, which prevents SQL injection.
    """
    clauses, params = ["t.club_id = ?"], [club_id]
    # Text search over the description and the category name.
    q = args.get("q", "").strip()
    if q:
        clauses.append("(t.description LIKE ? OR c.name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]     # % = "anything" in LIKE
    # Income / expense filter.
    if args.get("type") in ("income", "expense"):
        clauses.append("c.type = ?")
        params.append(args["type"])
    # Single category filter.
    if args.get("category", "").isdigit():
        clauses.append("t.category_id = ?")
        params.append(int(args["category"]))
    # Date range (inclusive). If the value is missing or invalid, undo the
    # clause we just added with pop() so the filter is simply skipped.
    for key, op in (("start", ">="), ("end", "<=")):
        try:
            clauses.append(f"t.txn_date {op} ?")
            params.append(parse_date(args[key]))
        except (KeyError, ValueError):
            clauses.pop()
    # Amount range (inclusive), same pattern as the dates.
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
    """Main club page: balance, totals, filtered transaction list, and members."""
    member = get_membership(club_id)       # 404 if the user isn't in this club
    where, params = build_transaction_filters(club_id, request.args)
    db = get_db()
    # The transaction list. {where} only contains our own fixed SQL pieces;
    # the user's values are passed separately in params.
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
    # All-time totals for the stat cards (not affected by the filters).
    # SUM(CASE ...) adds up only the rows of one type; COALESCE turns "no rows" (NULL) into 0.
    totals = db.execute(
        """SELECT COALESCE(SUM(CASE WHEN c.type='income'  THEN t.amount_cents END), 0) AS income,
                  COALESCE(SUM(CASE WHEN c.type='expense' THEN t.amount_cents END), 0) AS expense
           FROM transactions t JOIN categories c ON c.category_id = t.category_id
           WHERE t.club_id = ?""",
        (club_id,),
    ).fetchone()
    # Member list; "treasurer" sorts before "member" with DESC.
    members = db.execute(
        """SELECT u.username, m.role FROM memberships m JOIN users u ON u.user_id = m.user_id
           WHERE m.club_id = ? ORDER BY m.role DESC, u.username""",
        (club_id,),
    ).fetchall()
    return render_template(
        "club.html", club=member, txns=txns, totals=totals,
        balance=club_balance(club_id), categories=club_categories(club_id),
        members=members, filters=request.args,   # filters: refill the filter form
    )


# --------------------------------------------------------------------------
# Transactions: create / edit / delete
# --------------------------------------------------------------------------
def read_transaction_form(club_id):
    """Validate the transaction form. Returns (values_dict, error_message).

    Exactly one of the two is None: values on success, error on failure.
    """
    form = request.form
    try:
        amount = parse_amount(form.get("amount"))
        txn_date = parse_date(form.get("txn_date"))
    except ValueError as exc:
        return None, str(exc)
    # The category must exist AND belong to this club (stops using another club's category).
    category = get_db().execute(
        "SELECT category_id FROM categories WHERE category_id = ? AND club_id = ?",
        (form.get("category_id", type=int), club_id),
    ).fetchone()
    if category is None:
        return None, "Choose a valid category."
    description = form.get("description", "").strip()[:200]   # cap length at 200 chars
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
    # Members can only change their own entries; the treasurer can change any.
    if member["role"] != "treasurer" and txn["created_by"] != g.user["user_id"]:
        abort(403, description="You can only change transactions you created.")
    return txn


@bp.route("/clubs/<int:club_id>/transactions/new", methods=["GET", "POST"])
@login_required
def new_transaction(club_id):
    """GET shows an empty form; POST validates it and saves a new transaction."""
    member = get_membership(club_id)
    if request.method == "POST":
        values, error = read_transaction_form(club_id)
        if error:
            flash(error, "error")          # fall through and show the form again
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
            # Redirect after POST so refreshing the page doesn't submit twice.
            return redirect(url_for("main.club_dashboard", club_id=club_id))
    return render_template("transaction_form.html", club=member, txn=None,
                           categories=club_categories(club_id), today=date.today().isoformat())


@bp.route("/clubs/<int:club_id>/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(club_id, txn_id):
    """GET shows the form filled with the transaction; POST saves the changes."""
    member = get_membership(club_id)
    txn = get_editable_transaction(club_id, txn_id, member)   # 404/403 checks
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
    # Same template as "new"; txn is passed so the fields are pre-filled.
    return render_template("transaction_form.html", club=member, txn=txn,
                           categories=club_categories(club_id), today=date.today().isoformat())


@bp.route("/clubs/<int:club_id>/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(club_id, txn_id):
    """Delete a transaction (owner or treasurer only). The balance view updates automatically."""
    member = get_membership(club_id)
    get_editable_transaction(club_id, txn_id, member)   # only for its 404/403 checks
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
    """Treasurer adds a custom income or expense category."""
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
            # UNIQUE (club_id, name, type) already has this category.
            flash("That category already exists.", "error")
    return redirect(url_for("main.budgets", club_id=club_id))


@bp.route("/clubs/<int:club_id>/budgets", methods=["GET", "POST"])
@login_required
def budgets(club_id):
    """GET: budget vs. actual for a month. POST (treasurer): set a category's monthly budget."""
    member = get_membership(club_id)
    # Month to show, as 'YYYY-MM'. request.values reads the URL query OR the form.
    period = request.values.get("period") or date.today().strftime("%Y-%m")
    try:
        date.fromisoformat(period + "-01")    # validate by turning it into a real date
    except ValueError:
        period = date.today().strftime("%Y-%m")   # bad input -> current month
    db = get_db()
    if request.method == "POST":
        require_treasurer(member)
        category_id = request.form.get("category_id", type=int)
        # Budgets only make sense for this club's EXPENSE categories.
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
            # Insert a new row, or if one already exists for this month, update its amount.
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
    """For each expense category: budget for the month vs. actual spending.

    LEFT JOIN keeps categories that have no budget (budget is then NULL).
    The subquery adds up that category's transactions whose date starts
    with the month, e.g. substr('2026-11-05', 1, 7) = '2026-11'.
    """
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
    """Compute all report numbers for an optional date range.

    Returns (totals, by_category, by_month). Empty start/end = no limit.
    """
    # Build the shared WHERE clause once; all three queries use it.
    where, params = "t.club_id = ?", [club_id]
    if start:
        where += " AND t.txn_date >= ?"
        params.append(start)
    if end:
        where += " AND t.txn_date <= ?"
        params.append(end)
    db = get_db()
    # 1) Total income and total expenses in the range.
    totals = db.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN c.type='income'  THEN t.amount_cents END), 0) AS income,
                   COALESCE(SUM(CASE WHEN c.type='expense' THEN t.amount_cents END), 0) AS expense
            FROM transactions t JOIN categories c ON c.category_id = t.category_id WHERE {where}""",
        params,
    ).fetchone()
    # 2) Total and number of transactions per category (GROUP BY = one row per category).
    by_category = db.execute(
        f"""SELECT c.name, c.type, SUM(t.amount_cents) AS total, COUNT(*) AS n
            FROM transactions t JOIN categories c ON c.category_id = t.category_id
            WHERE {where} GROUP BY c.category_id ORDER BY c.type, total DESC""",
        params,
    ).fetchall()
    # 3) Income and expenses per month ('YYYY-MM'), oldest first.
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
    """Read optional ?start=&end= dates from the URL. Bad dates show an error and are ignored."""
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
    """Reports page: totals, tables, and the data for the two Chart.js charts."""
    member = get_membership(club_id)
    start, end = read_report_range()
    totals, by_category, by_month = report_data(club_id, start, end)
    expense_rows = [r for r in by_category if r["type"] == "expense"]
    # Chart data in dollars (cents / 100). The template turns this dict into
    # JavaScript with {{ chart|tojson }}.
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
    """Download every transaction in the date range as a CSV file (opens in Excel/Sheets)."""
    get_membership(club_id)                # members only
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
    # Build the CSV in memory, then send it as the response body.
    out = io.StringIO()
    writer = csv.writer(out)               # handles quoting of commas/quotes for us
    writer.writerow(["date", "type", "category", "amount", "description", "entered_by"])
    for r in rows:
        # csv_safe() on every user-typed field (see BUG-1 in the report).
        writer.writerow([r["txn_date"], r["type"], csv_safe(r["name"]), f"{r['amount_cents'] / 100:.2f}",
                         csv_safe(r["description"]), csv_safe(r["username"])])
    # Content-Disposition: attachment makes the browser download it as a file.
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=club-{club_id}-report.csv"})


# Lets you also start the app with:  python app.py
if __name__ == "__main__":
    create_app().run(debug=True)
