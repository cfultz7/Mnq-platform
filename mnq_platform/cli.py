"""Command-line entry point.

    python -m mnq_platform.cli run-all  --db mnq.db           # synthetic data -> everything
    python -m mnq_platform.cli size     --entry 21000 --stop 20978.5
    python -m mnq_platform.cli import   --db mnq.db --accounts a.csv --executions e.csv [--plans p.csv]
    python -m mnq_platform.cli rebuild  --db mnq.db
    python -m mnq_platform.cli reconcile --db mnq.db [--date 2026-01-07] [--out reports]
    python -m mnq_platform.cli audit    --db mnq.db
    python -m mnq_platform.cli report   --db mnq.db
    python -m mnq_platform.cli dashboard --db mnq.db --out dashboard.html [--offline]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

from . import db, metrics, reconcile, rules
from .config import DEFAULT_RULES
from .dashboard import build_dashboard
from .rebuild import rebuild_round_trips
from .sizing import DayState, size_position
from .synth import SynthConfig, generate


def cmd_generate(a) -> None:
    conn = db.connect(a.db)
    db.init_db(conn, reset=True)
    res = generate(conn, SynthConfig(n_accounts=a.accounts, n_plans=a.plans, seed=a.seed))
    print(f"synthetic data: {res.n_plans} plans over {res.n_days} days, {a.accounts} accounts, "
          f"{res.n_executions:,} executions, {len(res.ground_truth)} injected copy faults")


def cmd_import(a) -> None:
    conn = db.connect(a.db)
    db.init_db(conn, reset=a.reset)
    counts = db.import_csv(conn, a.accounts, a.executions, a.plans)
    print(f"imported {counts}")


def cmd_rebuild(a) -> None:
    conn = db.connect(a.db)
    res = rebuild_round_trips(conn)
    print(f"rebuilt {res.n_round_trips:,} round trips from {res.n_executions:,} executions")
    for p in res.open_positions:
        print(f"  WARNING open position: {p.account_id} {p.side} {p.qty} since {p.open_ts}")


def cmd_reconcile(a) -> None:
    conn = db.connect(a.db)
    t0 = time.perf_counter()
    flags = reconcile.reconcile(conn, a.date)      # one pass over every date when --date is omitted
    dates = [a.date] if a.date else [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ts, 1, 10) FROM executions ORDER BY 1")]
    by_date: dict[str, list] = {}
    for f in flags:
        by_date.setdefault(f.trade_date, []).append(f)
    for d in dates:
        day_flags = by_date.get(d, [])
        if a.out:
            reconcile.write_daily_report(conn, d, a.out)
        if a.date or day_flags:
            kinds = pd.Series([f.flag_type for f in day_flags]).value_counts().to_dict() if day_flags else {}
            print(f"{d}: {len(day_flags)} flags {kinds}")
    print(f"reconciled {len(dates)} day(s): {len(flags)} flags in {time.perf_counter() - t0:.2f}s"
          + (f"; reports in {a.out}/" if a.out else ""))


def cmd_audit(a) -> None:
    conn = db.connect(a.db)
    rt = metrics.load_round_trips(conn)
    plans = pd.read_sql_query("SELECT * FROM trade_plans", conn)
    days = rules.audit_days(rt)
    bad_plans = rules.audit_plans(plans)
    status = rules.account_status(rt)
    print(f"rule violations: {len(days)} account-day, {len(bad_plans)} plan")
    if len(days):
        print(days.to_string(index=False))
    if len(bad_plans):
        print(bad_plans.to_string(index=False))
    print()
    print(status.to_string(index=False))


def cmd_report(a) -> None:
    conn = db.connect(a.db)
    rt = metrics.load_round_trips(conn)
    lead = rt[rt.is_lead == 1]
    print("STRATEGY (lead account):", metrics.format_summary(metrics.summarize(lead)))
    print("ALL ACCOUNTS:           ", metrics.format_summary(metrics.summarize(rt)))
    print(f"max account drawdown:    ${metrics.by_account(rt).max_drawdown.max():,.2f}")
    print()
    pd.set_option("display.width", 200)
    print(metrics.by_account(rt).round(3).to_string(index=False))
    print()
    print(metrics.time_of_day(lead).round(3).to_string(index=False))


def cmd_dashboard(a) -> None:
    conn = db.connect(a.db)
    out = build_dashboard(conn, a.out, offline=a.offline)
    print(f"dashboard written to {out}")


def cmd_size(a) -> None:
    day = DayState(trades_taken=a.trades_today, realized_pnl=a.day_pnl, consecutive_losses=a.loss_streak)
    print(size_position(a.entry, a.stop, a.side, day, DEFAULT_RULES))


def cmd_run_all(a) -> None:
    cmd_generate(a)
    cmd_rebuild(a)
    a.date = None
    cmd_reconcile(a)
    cmd_audit(a)
    print()
    cmd_report(a)
    a.out = a.dashboard
    cmd_dashboard(a)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mnq_platform", description="Multi-account MNQ risk & analytics platform")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_db(sp):
        sp.add_argument("--db", default="mnq.db", help="SQLite database path (default mnq.db)")
        return sp

    g = with_db(sub.add_parser("generate", help="create a fresh DB filled with synthetic data"))
    g.add_argument("--accounts", type=int, default=20)
    g.add_argument("--plans", type=int, default=300)
    g.add_argument("--seed", type=int, default=7)
    g.set_defaults(func=cmd_generate)

    im = with_db(sub.add_parser("import", help="load accounts/plans/executions from CSV"))
    im.add_argument("--accounts", required=True)
    im.add_argument("--executions", required=True)
    im.add_argument("--plans")
    im.add_argument("--reset", action="store_true", help="drop existing tables first")
    im.set_defaults(func=cmd_import)

    with_db(sub.add_parser("rebuild", help="rebuild round trips from executions")).set_defaults(func=cmd_rebuild)

    r = with_db(sub.add_parser("reconcile", help="reconcile follower copies against the lead"))
    r.add_argument("--date", help="YYYY-MM-DD (default: every date)")
    r.add_argument("--out", help="folder for daily CSV reports")
    r.set_defaults(func=cmd_reconcile)

    with_db(sub.add_parser("audit", help="rule-compliance audit and account status")).set_defaults(func=cmd_audit)
    with_db(sub.add_parser("report", help="print performance metrics")).set_defaults(func=cmd_report)

    d = with_db(sub.add_parser("dashboard", help="write the Plotly HTML dashboard"))
    d.add_argument("--out", default="dashboard.html")
    d.add_argument("--offline", action="store_true", help="inline plotly.js (no network needed)")
    d.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("size", help="pre-trade sizing for one trade")
    s.add_argument("--entry", type=float, required=True)
    s.add_argument("--stop", type=float, required=True)
    s.add_argument("--side", choices=["LONG", "SHORT", "long", "short"])
    s.add_argument("--trades-today", type=int, default=0)
    s.add_argument("--day-pnl", type=float, default=0.0)
    s.add_argument("--loss-streak", type=int, default=0)
    s.set_defaults(func=cmd_size)

    ra = with_db(sub.add_parser("run-all", help="generate synthetic data and run the full pipeline"))
    ra.add_argument("--accounts", type=int, default=20)
    ra.add_argument("--plans", type=int, default=300)
    ra.add_argument("--seed", type=int, default=7)
    ra.add_argument("--reports", dest="out", default="reports", help="folder for daily recon CSVs")
    ra.add_argument("--dashboard", default="dashboard.html")
    ra.add_argument("--offline", action="store_true")
    ra.set_defaults(func=cmd_run_all)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main(sys.argv[1:])
