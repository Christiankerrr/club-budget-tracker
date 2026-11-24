"""Load demo data so the app has something to show.  Run:  python seed.py
Demo logins (test data only):  treasurer / demo12345   and   member / demo12345
"""
import random
from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from app import DEFAULT_CATEGORIES, create_app
from db import get_db

app = create_app()
with app.app_context():          # get_db() needs an app context outside a request
    db = get_db()
    # Stop if the demo users already exist, so running this twice is safe.
    if db.execute("SELECT 1 FROM users WHERE username = 'treasurer'").fetchone():
        print("Demo data already loaded.")
        raise SystemExit
    # 1) Two demo users -> ids = {"treasurer": id, "member": id}
    ids = {}
    for name in ("treasurer", "member"):
        ids[name] = db.execute(
            "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
            (name, f"{name}@example.edu", generate_password_hash("demo12345"))).lastrowid
    # 2) One club with a fixed join code, and both users as members.
    club = db.execute(
        "INSERT INTO clubs (name, description, join_code, created_by) VALUES (?, ?, ?, ?)",
        ("Robotics Club", "Building robots for the spring competition", "ROBO26", ids["treasurer"])).lastrowid
    db.execute("INSERT INTO memberships VALUES (?, ?, 'treasurer', CURRENT_TIMESTAMP)", (ids["treasurer"], club))
    db.execute("INSERT INTO memberships VALUES (?, ?, 'member', CURRENT_TIMESTAMP)", (ids["member"], club))
    # 3) The default categories -> cat = {"Food": category_id, ...}
    cat = {}
    for n, t in DEFAULT_CATEGORIES:
        cat[n] = db.execute("INSERT INTO categories (club_id, name, type) VALUES (?, ?, ?)", (club, n, t)).lastrowid

    # 4) Ten transactions spread from late August to mid November.
    #    A fixed random seed means everyone gets the exact same demo data.
    random.seed(332)
    start = date(2026, 8, 25)
    # (category, amount in cents, days after start, description)
    rows = [("School Funding", 150000, 0, "ASI semester allocation"),
            ("Membership Dues", 40000, 10, "Fall dues (20 members)"),
            ("Fundraising", 62550, 40, "Bake sale"),
            ("Donations", 25000, 55, "Alumni donation"),
            ("Supplies", 38999, 12, "Motors and sensors"),
            ("Food", 8540, 20, "Kickoff meeting pizza"),
            ("Events", 15000, 45, "Workshop room rental"),
            ("Travel", 42000, 70, "Bus to regional competition"),
            ("Food", 6275, 72, "Team dinner at regionals"),
            ("Supplies", 21450, 80, "3D printer filament")]
    for name, cents, offset, desc in rows:
        who = ids["treasurer"] if random.random() < .7 else ids["member"]
        db.execute("INSERT INTO transactions (club_id, category_id, created_by, amount_cents, txn_date, description)"
                   " VALUES (?, ?, ?, ?, ?, ?)",
                   (club, cat[name], who, cents, (start + timedelta(days=offset)).isoformat(), desc))
    # 5) November 2026 budgets (Travel is set up to be over budget for the demo).
    for name, cents in (("Food", 10000), ("Supplies", 50000), ("Travel", 40000), ("Events", 20000)):
        db.execute("INSERT INTO budgets (club_id, category_id, period, amount_cents) VALUES (?, ?, '2026-11', ?)",
                   (club, cat[name], cents))
    db.commit()
    print("Demo data loaded. Log in as treasurer / demo12345")
