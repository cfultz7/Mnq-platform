-- SQLite schema for the multi-account MNQ risk & analytics platform.
-- Timestamps are ISO-8601 text in exchange-local time ('YYYY-MM-DD HH:MM:SS.fff'),
-- which sorts correctly as text.

CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,
    is_lead           INTEGER NOT NULL DEFAULT 0 CHECK (is_lead IN (0, 1)),
    starting_balance  REAL    NOT NULL
);

-- One row per trade idea, produced by the pre-trade sizing module on the lead account.
CREATE TABLE IF NOT EXISTS trade_plans (
    plan_id    TEXT PRIMARY KEY,
    ts         TEXT    NOT NULL,
    side       TEXT    NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    entry      REAL    NOT NULL,
    stop       REAL    NOT NULL,
    target     REAL,
    contracts  INTEGER NOT NULL CHECK (contracts > 0),
    risk_usd   REAL    NOT NULL
);

-- Raw broker fills. A single order can fill in several executions.
-- order_id is the copier's order tag: the same value on the lead and on every copy.
CREATE TABLE IF NOT EXISTS executions (
    exec_id     TEXT PRIMARY KEY,
    account_id  TEXT    NOT NULL REFERENCES accounts(account_id),
    ts          TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    side        TEXT    NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    price       REAL    NOT NULL,
    commission  REAL    NOT NULL DEFAULT 0,
    order_id    TEXT    NOT NULL,
    plan_id     TEXT    REFERENCES trade_plans(plan_id)
);
CREATE INDEX IF NOT EXISTS ix_exec_account_ts ON executions(account_id, ts);
CREATE INDEX IF NOT EXISTS ix_exec_order      ON executions(order_id);

-- Flat-to-flat trades rebuilt from executions (derived; safe to delete and rebuild).
CREATE TABLE IF NOT EXISTS round_trips (
    rt_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            TEXT    NOT NULL REFERENCES accounts(account_id),
    plan_id               TEXT,
    side                  TEXT    NOT NULL CHECK (side IN ('LONG', 'SHORT')),
    open_ts               TEXT    NOT NULL,
    close_ts              TEXT    NOT NULL,
    trade_date            TEXT    NOT NULL,
    entry_qty             INTEGER NOT NULL,
    max_position          INTEGER NOT NULL,
    avg_entry             REAL    NOT NULL,
    avg_exit              REAL    NOT NULL,
    gross_pnl             REAL    NOT NULL,
    commission            REAL    NOT NULL,
    net_pnl               REAL    NOT NULL,
    initial_risk_usd      REAL,
    r_multiple            REAL,
    entry_slippage_ticks  REAL,
    n_executions          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rt_account_date ON round_trips(account_id, trade_date);

-- Output of the daily copy reconciliation (derived; rebuilt per date).
CREATE TABLE IF NOT EXISTS recon_flags (
    flag_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date        TEXT NOT NULL,
    account_id        TEXT NOT NULL,
    order_id          TEXT NOT NULL,
    plan_id           TEXT,
    flag_type         TEXT NOT NULL CHECK (flag_type IN
                          ('MISSING', 'PARTIAL', 'OVERFILL', 'MISPRICED', 'WRONG_SIDE', 'UNEXPECTED')),
    expected_qty      INTEGER,
    filled_qty        INTEGER,
    expected_price    REAL,
    avg_price         REAL,
    price_diff_ticks  REAL,
    detail            TEXT
);
CREATE INDEX IF NOT EXISTS ix_flags_date ON recon_flags(trade_date);
