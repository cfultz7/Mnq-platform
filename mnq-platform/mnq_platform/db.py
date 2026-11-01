"""SQLite connection, schema setup and CSV import."""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
TABLES = ("recon_flags", "round_trips", "executions", "trade_plans", "accounts")


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, reset: bool = False) -> None:
    """Create all tables. With reset=True, drop existing tables first."""
    if reset:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()


def lead_account(conn: sqlite3.Connection) -> str:
    rows = conn.execute("SELECT account_id FROM accounts WHERE is_lead = 1").fetchall()
    if len(rows) != 1:
        raise ValueError(f"expected exactly one lead account, found {len(rows)}")
    return rows[0]["account_id"]


# ---------------------------------------------------------------------------
# CSV import (for running the platform on real broker exports)
# ---------------------------------------------------------------------------

ACCOUNT_COLS = ("account_id", "is_lead", "starting_balance")
PLAN_COLS = ("plan_id", "ts", "side", "entry", "stop", "target", "contracts", "risk_usd")
EXEC_COLS = ("exec_id", "account_id", "ts", "symbol", "side", "qty", "price",
             "commission", "order_id", "plan_id")


def _read_csv(path: str | Path, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> list[dict]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        rows = []
        for row in reader:
            out = {c: (row.get(c) or None) for c in required + optional}
            rows.append(out)
        return rows


def import_csv(conn: sqlite3.Connection, accounts: str | Path, executions: str | Path,
               plans: str | Path | None = None) -> dict[str, int]:
    """Load accounts, (optional) trade plans and executions from CSV files."""
    acct_rows = _read_csv(accounts, ACCOUNT_COLS)
    conn.executemany(
        "INSERT INTO accounts VALUES (?, ?, ?)",
        [(r["account_id"], int(r["is_lead"]), float(r["starting_balance"])) for r in acct_rows],
    )
    n_plans = 0
    if plans:
        plan_rows = _read_csv(plans, ("plan_id", "ts", "side", "entry", "stop", "contracts", "risk_usd"),
                              ("target",))
        conn.executemany(
            "INSERT INTO trade_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r["plan_id"], r["ts"], r["side"].upper(), float(r["entry"]), float(r["stop"]),
              float(r["target"]) if r["target"] else None, int(r["contracts"]), float(r["risk_usd"]))
             for r in plan_rows],
        )
        n_plans = len(plan_rows)
    exec_rows = _read_csv(executions,
                          ("exec_id", "account_id", "ts", "symbol", "side", "qty", "price", "order_id"),
                          ("commission", "plan_id"))
    conn.executemany(
        "INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(r["exec_id"], r["account_id"], r["ts"], r["symbol"], r["side"].upper(), int(r["qty"]),
          float(r["price"]), float(r["commission"] or 0), r["order_id"], r["plan_id"])
         for r in exec_rows],
    )
    conn.commit()
    return {"accounts": len(acct_rows), "plans": n_plans, "executions": len(exec_rows)}
