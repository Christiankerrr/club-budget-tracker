-- Club Budget Tracker database schema (SQLite)
-- Money is stored as INTEGER cents to avoid floating-point rounding errors.
-- A club's balance is NOT stored; it is derived from transactions (see club_balances view)
-- so it can never drift out of sync with the transaction history.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,  -- 'Alice' == 'alice'
    email         TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS clubs (
    club_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    join_code   TEXT    NOT NULL UNIQUE,
    created_by  INTEGER NOT NULL REFERENCES users(user_id),
    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Many-to-many: a user can belong to many clubs, a club has many users.
CREATE TABLE IF NOT EXISTS memberships (
    user_id   INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    club_id   INTEGER NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    role      TEXT    NOT NULL CHECK (role IN ('treasurer', 'member')),
    joined_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, club_id)
);

-- Each club owns its categories. The category decides whether a
-- transaction is income or expense, so the type is not repeated in transactions.
CREATE TABLE IF NOT EXISTS categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id     INTEGER NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL CHECK (type IN ('income', 'expense')),
    UNIQUE (club_id, name, type),
    UNIQUE (category_id, club_id)   -- target for the composite FK below
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id        INTEGER NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    category_id    INTEGER NOT NULL,
    created_by     INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    amount_cents   INTEGER NOT NULL CHECK (amount_cents > 0),
    txn_date       TEXT    NOT NULL,          -- ISO 8601 'YYYY-MM-DD'
    description    TEXT,
    created_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Composite FK: the category must belong to the SAME club as the transaction.
    FOREIGN KEY (category_id, club_id) REFERENCES categories(category_id, club_id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_club_date ON transactions(club_id, txn_date);

-- Monthly spending budget per expense category.
CREATE TABLE IF NOT EXISTS budgets (
    budget_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    club_id      INTEGER NOT NULL REFERENCES clubs(club_id) ON DELETE CASCADE,
    category_id  INTEGER NOT NULL,
    period       TEXT    NOT NULL,             -- 'YYYY-MM'
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    UNIQUE (club_id, category_id, period),
    FOREIGN KEY (category_id, club_id) REFERENCES categories(category_id, club_id) ON DELETE CASCADE
);

-- Current balance of every club = income - expenses.
CREATE VIEW IF NOT EXISTS club_balances AS
SELECT cl.club_id,
       COALESCE(SUM(CASE WHEN c.type = 'income' THEN t.amount_cents
                         ELSE -t.amount_cents END), 0) AS balance_cents
FROM clubs cl
LEFT JOIN transactions t ON t.club_id = cl.club_id
LEFT JOIN categories  c ON c.category_id = t.category_id
GROUP BY cl.club_id;
