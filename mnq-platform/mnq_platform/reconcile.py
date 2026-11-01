"""Daily copy reconciliation: lead account vs. every follower account.

The trade copier stamps each copied order with the lead's order ID, so every
lead order should appear once on every follower with the same side, the same
quantity and (within tolerance) the same price. Fills are aggregated to the
order level (summed qty, volume-weighted price) before comparing.

Flags written to ``recon_flags``:
    MISSING     lead order has no fills on the follower
    PARTIAL     follower filled fewer contracts than the lead
    OVERFILL    follower filled more contracts than the lead
    MISPRICED   follower VWAP differs from lead VWAP by more than the tolerance
    WRONG_SIDE  follower order is on the opposite side
    UNEXPECTED  follower order with no matching lead order (manual / rogue trade)

A single order can carry more than one flag (e.g. PARTIAL and MISPRICED).
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import DEFAULT_RECON, MNQ, Instrument, ReconConfig
from .db import lead_account


@dataclass(frozen=True)
class OrderAgg:
    account_id: str
    order_id: str
    plan_id: str | None
    trade_date: str
    side: str
    qty: int
    vwap: float


@dataclass(frozen=True)
class Flag:
    trade_date: str
    account_id: str
    order_id: str
    plan_id: str | None
    flag_type: str
    expected_qty: int | None = None
    filled_qty: int | None = None
    expected_price: float | None = None
    avg_price: float | None = None
    price_diff_ticks: float | None = None
    detail: str = ""


_ORDER_SQL = """
SELECT account_id, order_id, MIN(plan_id) AS plan_id, substr(MIN(ts), 1, 10) AS trade_date,
       MIN(side) AS side, COUNT(DISTINCT side) AS n_sides,
       SUM(qty) AS qty, SUM(qty * price) / SUM(qty) AS vwap
FROM executions
GROUP BY account_id, order_id
"""


def load_orders(conn: sqlite3.Connection) -> dict[tuple[str, str], OrderAgg]:
    orders = {}
    for r in conn.execute(_ORDER_SQL):
        if r["n_sides"] != 1:
            raise ValueError(f"order {r['order_id']} on {r['account_id']} has fills on both sides")
        orders[(r["account_id"], r["order_id"])] = OrderAgg(
            r["account_id"], r["order_id"], r["plan_id"], r["trade_date"], r["side"], r["qty"], r["vwap"])
    return orders


def compare_orders(lead: OrderAgg, follower: OrderAgg | None, account_id: str,
                   cfg: ReconConfig = DEFAULT_RECON, inst: Instrument = MNQ) -> list[Flag]:
    """All flags for one lead order on one follower account."""
    base = dict(trade_date=lead.trade_date, account_id=account_id, order_id=lead.order_id,
                plan_id=lead.plan_id, expected_qty=lead.qty, expected_price=lead.vwap)
    if follower is None:
        return [Flag(flag_type="MISSING", filled_qty=0, detail=f"lead {lead.side} {lead.qty} not copied", **base)]

    flags: list[Flag] = []
    base.update(filled_qty=follower.qty, avg_price=follower.vwap)
    if follower.side != lead.side:
        flags.append(Flag(flag_type="WRONG_SIDE", detail=f"lead {lead.side}, follower {follower.side}", **base))
    if follower.qty < lead.qty:
        flags.append(Flag(flag_type="PARTIAL", detail=f"{follower.qty} of {lead.qty} filled", **base))
    elif follower.qty > lead.qty:
        flags.append(Flag(flag_type="OVERFILL", detail=f"{follower.qty} filled vs {lead.qty} on lead", **base))

    # Signed so that positive = follower got a worse price than the lead.
    direction = 1 if lead.side == "BUY" else -1
    diff_ticks = direction * (follower.vwap - lead.vwap) / inst.tick_size
    if abs(diff_ticks) > cfg.price_tolerance_ticks + 1e-9:
        word = "worse" if diff_ticks > 0 else "better"
        flags.append(Flag(flag_type="MISPRICED", price_diff_ticks=round(diff_ticks, 2),
                          detail=f"{abs(diff_ticks):.2f} ticks {word} than lead", **base))
    return flags


def reconcile(conn: sqlite3.Connection, trade_date: str | None = None, cfg: ReconConfig = DEFAULT_RECON,
              inst: Instrument = MNQ, write: bool = True) -> list[Flag]:
    """Reconcile one trade date (or every date when ``trade_date`` is None)."""
    lead_id = lead_account(conn)
    followers = [r["account_id"] for r in
                 conn.execute("SELECT account_id FROM accounts WHERE is_lead = 0 ORDER BY account_id")]
    orders = load_orders(conn)
    lead_orders = sorted((o for (acct, _), o in orders.items()
                          if acct == lead_id and (trade_date is None or o.trade_date == trade_date)),
                         key=lambda o: o.order_id)
    all_lead_ids = {oid for (acct, oid) in orders if acct == lead_id}

    flags: list[Flag] = []
    for acct in followers:
        for lo in lead_orders:
            flags.extend(compare_orders(lo, orders.get((acct, lo.order_id)), acct, cfg, inst))
    for (acct, oid), fo in sorted(orders.items()):
        if acct == lead_id or oid in all_lead_ids:
            continue
        if trade_date is None or fo.trade_date == trade_date:
            flags.append(Flag(fo.trade_date, acct, oid, fo.plan_id, "UNEXPECTED", filled_qty=fo.qty,
                              avg_price=fo.vwap, detail=f"{fo.side} {fo.qty} with no lead order"))

    if write:
        dates = {trade_date} if trade_date else {o.trade_date for o in orders.values()}
        with conn:
            conn.executemany("DELETE FROM recon_flags WHERE trade_date = ?", [(d,) for d in dates])
            conn.executemany(
                """INSERT INTO recon_flags (trade_date, account_id, order_id, plan_id, flag_type, expected_qty,
                       filled_qty, expected_price, avg_price, price_diff_ticks, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(f.trade_date, f.account_id, f.order_id, f.plan_id, f.flag_type, f.expected_qty, f.filled_qty,
                  f.expected_price, f.avg_price, f.price_diff_ticks, f.detail) for f in flags],
            )
    return flags


def daily_summary(conn: sqlite3.Connection, trade_date: str) -> list[dict]:
    """Per-account sign-off sheet for one day: orders, flags and P&L vs. the lead."""
    lead_id = lead_account(conn)
    n_lead_orders = conn.execute(
        "SELECT COUNT(DISTINCT order_id) FROM executions WHERE account_id = ? AND substr(ts, 1, 10) = ?",
        (lead_id, trade_date)).fetchone()[0]
    pnl = {r["account_id"]: (r["n"], r["pnl"]) for r in conn.execute(
        "SELECT account_id, COUNT(*) AS n, ROUND(SUM(net_pnl), 2) AS pnl FROM round_trips "
        "WHERE trade_date = ? GROUP BY account_id", (trade_date,))}
    flag_counts = {r["account_id"]: r["n"] for r in conn.execute(
        "SELECT account_id, COUNT(*) AS n FROM recon_flags WHERE trade_date = ? GROUP BY account_id", (trade_date,))}
    lead_pnl = pnl.get(lead_id, (0, 0.0))[1] or 0.0

    rows = []
    for r in conn.execute("SELECT account_id, is_lead FROM accounts ORDER BY account_id"):
        acct = r["account_id"]
        n_trips, day_pnl = pnl.get(acct, (0, 0.0))
        day_pnl = day_pnl or 0.0
        n_flags = flag_counts.get(acct, 0)
        rows.append({
            "trade_date": trade_date,
            "account_id": acct,
            "role": "LEAD" if r["is_lead"] else "FOLLOWER",
            "lead_orders": n_lead_orders,
            "round_trips": n_trips,
            "flags": n_flags,
            "net_pnl": round(day_pnl, 2),
            "pnl_vs_lead": round(day_pnl - lead_pnl, 2),
            "status": "OK" if n_flags == 0 else "REVIEW",
        })
    return rows


def write_daily_report(conn: sqlite3.Connection, trade_date: str, out_dir: str | Path) -> tuple[Path, Path]:
    """Write <date>_summary.csv and <date>_flags.csv for the day's sign-off."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / f"recon_{trade_date}_summary.csv"
    flags_path = out / f"recon_{trade_date}_flags.csv"

    summary = daily_summary(conn, trade_date)
    with open(summary_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)

    flag_rows = [dict(r) for r in conn.execute(
        "SELECT trade_date, account_id, order_id, plan_id, flag_type, expected_qty, filled_qty, expected_price, "
        "avg_price, price_diff_ticks, detail FROM recon_flags WHERE trade_date = ? ORDER BY account_id, order_id",
        (trade_date,))]
    fields = list(asdict(Flag("", "", "", None, "")).keys())
    with open(flags_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(flag_rows)
    return summary_path, flags_path
