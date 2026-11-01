"""Rebuild account-level round trips from raw executions.

A round trip runs from the fill that takes an account off flat to the fill
that returns it to flat. The rebuild handles:

* orders filled in several executions (partial fills)
* scaling in (adds in the same direction) and scaling out (partial exits)
* reversals: a fill that crosses zero closes the current trade and opens a
  new one with the remainder; its commission is split pro rata by quantity

P&L uses average prices, which equals FIFO lot matching once a trade is flat:
    gross = direction * (exit notional - entry notional) * point value
R is net P&L divided by the planned price risk |plan entry - plan stop| *
point value * contracts entered. Entry slippage is measured in ticks against
the planned entry, positive = worse than planned.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .config import MNQ, Instrument


@dataclass(frozen=True)
class Fill:
    exec_id: str
    account_id: str
    ts: str
    side: str          # BUY / SELL
    qty: int
    price: float
    commission: float = 0.0
    order_id: str = ""
    plan_id: str | None = None


@dataclass(frozen=True)
class Plan:
    plan_id: str
    side: str
    entry: float
    stop: float


@dataclass
class RoundTrip:
    account_id: str
    plan_id: str | None
    side: str
    open_ts: str
    close_ts: str
    entry_qty: int
    max_position: int
    avg_entry: float
    avg_exit: float
    gross_pnl: float
    commission: float
    net_pnl: float
    n_executions: int
    initial_risk_usd: float | None = None
    r_multiple: float | None = None
    entry_slippage_ticks: float | None = None

    @property
    def trade_date(self) -> str:
        return self.open_ts[:10]


@dataclass
class OpenPosition:
    account_id: str
    side: str
    qty: int
    open_ts: str
    plan_id: str | None


@dataclass
class _Working:
    direction: int
    open_ts: str
    plan_id: str | None
    entry_qty: int = 0
    entry_notional: float = 0.0
    exit_qty: int = 0
    exit_notional: float = 0.0
    commission: float = 0.0
    max_position: int = 0
    exec_ids: set = field(default_factory=set)


class AccountBuilder:
    """Feeds one account's fills (in time order) and collects closed round trips."""

    def __init__(self, account_id: str, inst: Instrument = MNQ):
        self.account_id = account_id
        self.inst = inst
        self.position = 0                    # signed contracts
        self.current: _Working | None = None
        self.closed: list[RoundTrip] = []

    def apply(self, fill: Fill) -> None:
        if fill.account_id != self.account_id:
            raise ValueError(f"fill {fill.exec_id} belongs to {fill.account_id}, not {self.account_id}")
        if fill.qty <= 0:
            raise ValueError(f"fill {fill.exec_id} has non-positive qty")
        signed = fill.qty if fill.side == "BUY" else -fill.qty

        if self.position == 0:
            self._open(fill, fill.qty, fill.commission)
            return

        if (signed > 0) == (self.position > 0):          # adding to the position
            self._add_entry(fill, fill.qty, fill.commission)
            return

        closing = min(fill.qty, abs(self.position))       # reducing / closing / reversing
        remainder = fill.qty - closing
        close_commission = fill.commission * closing / fill.qty
        self._add_exit(fill, closing, close_commission)
        if self.position == 0:
            self._close(fill.ts)
        if remainder:
            self._open(fill, remainder, fill.commission - close_commission)

    # -- internals ------------------------------------------------------
    def _open(self, fill: Fill, qty: int, commission: float) -> None:
        direction = 1 if fill.side == "BUY" else -1
        self.current = _Working(direction=direction, open_ts=fill.ts, plan_id=fill.plan_id)
        self._add_entry(fill, qty, commission)

    def _add_entry(self, fill: Fill, qty: int, commission: float) -> None:
        w = self.current
        w.entry_qty += qty
        w.entry_notional += qty * fill.price
        w.commission += commission
        w.exec_ids.add(fill.exec_id)
        if w.plan_id is None:
            w.plan_id = fill.plan_id
        self.position += w.direction * qty
        w.max_position = max(w.max_position, abs(self.position))

    def _add_exit(self, fill: Fill, qty: int, commission: float) -> None:
        w = self.current
        w.exit_qty += qty
        w.exit_notional += qty * fill.price
        w.commission += commission
        w.exec_ids.add(fill.exec_id)
        self.position -= w.direction * qty

    def _close(self, close_ts: str) -> None:
        w = self.current
        assert w.entry_qty == w.exit_qty, "round trip closed with unequal entry/exit quantity"
        gross = w.direction * (w.exit_notional - w.entry_notional) * self.inst.point_value
        self.closed.append(RoundTrip(
            account_id=self.account_id,
            plan_id=w.plan_id,
            side="LONG" if w.direction > 0 else "SHORT",
            open_ts=w.open_ts,
            close_ts=close_ts,
            entry_qty=w.entry_qty,
            max_position=w.max_position,
            avg_entry=w.entry_notional / w.entry_qty,
            avg_exit=w.exit_notional / w.exit_qty,
            gross_pnl=round(gross, 2),
            commission=round(w.commission, 2),
            net_pnl=round(gross - w.commission, 2),
            n_executions=len(w.exec_ids),
        ))
        self.current = None

    def open_position(self) -> OpenPosition | None:
        if self.position == 0:
            return None
        w = self.current
        return OpenPosition(self.account_id, "LONG" if w.direction > 0 else "SHORT",
                            abs(self.position), w.open_ts, w.plan_id)


def attach_plan_metrics(rt: RoundTrip, plan: Plan | None, inst: Instrument = MNQ) -> RoundTrip:
    """Fill in planned risk, R multiple and entry slippage when the trade has a plan."""
    if plan is None:
        return rt
    direction = 1 if rt.side == "LONG" else -1
    price_risk = abs(plan.entry - plan.stop) * inst.point_value * rt.entry_qty
    rt.initial_risk_usd = round(price_risk, 2)
    rt.r_multiple = rt.net_pnl / price_risk if price_risk else None
    rt.entry_slippage_ticks = round(direction * (rt.avg_entry - plan.entry) / inst.tick_size, 4)
    return rt


def build_round_trips(fills: list[Fill], plans: dict[str, Plan] | None = None,
                      inst: Instrument = MNQ) -> tuple[list[RoundTrip], list[OpenPosition]]:
    """Pure function: fills (any order, any accounts) -> closed round trips + still-open positions.

    Fills are processed per account in (ts, input order). Pass them in broker
    order so same-timestamp fills keep their sequence.
    """
    plans = plans or {}
    builders: dict[str, AccountBuilder] = {}
    indexed = sorted(enumerate(fills), key=lambda p: (p[1].account_id, p[1].ts, p[0]))
    for _, fill in indexed:
        builder = builders.setdefault(fill.account_id, AccountBuilder(fill.account_id, inst))
        builder.apply(fill)

    trips: list[RoundTrip] = []
    still_open: list[OpenPosition] = []
    for account_id in sorted(builders):
        b = builders[account_id]
        for rt in b.closed:
            trips.append(attach_plan_metrics(rt, plans.get(rt.plan_id), inst))
        pos = b.open_position()
        if pos:
            still_open.append(pos)
    return trips, still_open


@dataclass
class RebuildResult:
    n_executions: int
    n_round_trips: int
    open_positions: list[OpenPosition]


def rebuild_round_trips(conn: sqlite3.Connection, inst: Instrument = MNQ) -> RebuildResult:
    """Read executions + plans from SQL, rebuild, and replace the round_trips table."""
    fills = [Fill(r["exec_id"], r["account_id"], r["ts"], r["side"], r["qty"], r["price"],
                  r["commission"], r["order_id"], r["plan_id"])
             for r in conn.execute("SELECT * FROM executions ORDER BY account_id, ts, rowid")]
    plans = {r["plan_id"]: Plan(r["plan_id"], r["side"], r["entry"], r["stop"])
             for r in conn.execute("SELECT plan_id, side, entry, stop FROM trade_plans")}
    trips, still_open = build_round_trips(fills, plans, inst)

    with conn:
        conn.execute("DELETE FROM round_trips")
        conn.executemany(
            """INSERT INTO round_trips (account_id, plan_id, side, open_ts, close_ts, trade_date,
                   entry_qty, max_position, avg_entry, avg_exit, gross_pnl, commission, net_pnl,
                   initial_risk_usd, r_multiple, entry_slippage_ticks, n_executions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(t.account_id, t.plan_id, t.side, t.open_ts, t.close_ts, t.trade_date, t.entry_qty,
              t.max_position, t.avg_entry, t.avg_exit, t.gross_pnl, t.commission, t.net_pnl,
              t.initial_risk_usd, t.r_multiple, t.entry_slippage_ticks, t.n_executions) for t in trips],
        )
    return RebuildResult(len(fills), len(trips), still_open)
