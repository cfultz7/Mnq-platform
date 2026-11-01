"""Seeded synthetic data generator.

Produces a realistic-shaped dataset to exercise the whole platform: one lead
account whose orders are copied to N-1 followers, multi-execution fills,
scale-outs, copy latency, small copy slippage, and a few injected copy
failures (missing trades, partial copies, mispriced copies).

Every plan goes through the real sizing module and the lead's day state, so
the lead account follows the rules by construction. The generator returns the
ground truth of every injected failure so tests can check that reconciliation
finds exactly those and nothing else.

The outcome distribution (win probability, R ranges) is generic and NOT
calibrated to any real track record. Results printed from this data are
illustrative only.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .config import DEFAULT_RULES, MNQ, Instrument, RiskRules
from .sizing import DAY_BLOCKS, OK, DayState, size_position


@dataclass
class SynthConfig:
    n_accounts: int = 20
    n_plans: int = 300
    start_date: date = date(2026, 1, 5)
    seed: int = 7
    start_price: float = 21_000.0
    p_win: float = 0.5
    setups_per_day: tuple[int, int] = (3, 7)
    commission_per_side: float = 0.50         # per contract; 2x = RiskRules.round_turn_fee
    p_split_fill: float = 0.35                # an order fills in two executions
    p_follower_slip: float = 0.25             # follower fill 1 tick worse than lead (within tolerance)
    p_missing: float = 0.002                  # per follower per plan
    p_partial: float = 0.002
    p_mispriced: float = 0.002


@dataclass
class SynthResult:
    n_days: int
    n_plans: int
    n_executions: int
    ground_truth: set[tuple[str, str, str]] = field(default_factory=set)   # (account, order_id, flag_type)


def _business_days(start: date):
    d = start
    while True:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def _fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


@dataclass
class _Order:
    order_id: str
    side: str          # BUY / SELL
    qty: int
    price: float
    ts: datetime


class _Writer:
    def __init__(self, cfg: SynthConfig, rng: random.Random):
        self.cfg, self.rng = cfg, rng
        self.rows: list[tuple] = []
        self.counter = 0

    def fill_order(self, account: str, order: _Order, plan_id: str) -> None:
        """Write one order as one or two executions."""
        parts = [order.qty]
        if order.qty >= 2 and self.rng.random() < self.cfg.p_split_fill:
            first = self.rng.randint(1, order.qty - 1)
            parts = [first, order.qty - first]
        ts = order.ts
        for q in parts:
            self.counter += 1
            self.rows.append((f"X{self.counter:07d}", account, _fmt(ts), "MNQ", order.side, q, order.price,
                              round(q * self.cfg.commission_per_side, 2), order.order_id, plan_id))
            ts += timedelta(milliseconds=self.rng.randint(20, 400))


def _plan_orders(plan_id: str, direction: int, contracts: int, entry_fill: float, stop: float,
                 open_ts: datetime, win: bool, rng: random.Random, inst: Instrument) -> tuple[list[_Order], datetime]:
    """Lead orders for one trade: entry, then one or two exits."""
    tick = inst.tick_size
    entry_side, exit_side = ("BUY", "SELL") if direction > 0 else ("SELL", "BUY")
    stop_dist = abs(entry_fill - stop)
    orders = [_Order(f"{plan_id}-ENT", entry_side, contracts, entry_fill, open_ts)]
    t = open_ts + timedelta(minutes=rng.uniform(2, 45))

    if win:
        r_final = rng.uniform(0.4, 2.8)
        if contracts >= 2 and rng.random() < 0.6:
            q1 = contracts // 2
            r1 = rng.uniform(0.4, max(0.5, min(1.0, r_final)))
            orders.append(_Order(f"{plan_id}-TP1", exit_side, q1,
                                 inst.round_to_tick(entry_fill + direction * r1 * stop_dist), t))
            t += timedelta(minutes=rng.uniform(1, 30))
            orders.append(_Order(f"{plan_id}-TP2", exit_side, contracts - q1,
                                 inst.round_to_tick(entry_fill + direction * r_final * stop_dist), t))
        else:
            orders.append(_Order(f"{plan_id}-TP", exit_side, contracts,
                                 inst.round_to_tick(entry_fill + direction * r_final * stop_dist), t))
    else:
        if rng.random() < 0.7:   # full stop, 0-1 tick of stop slippage
            px = stop - direction * tick * rng.randint(0, 1)
            orders.append(_Order(f"{plan_id}-STP", exit_side, contracts, px, t))
        else:                    # cut early for a partial loss
            r = rng.uniform(0.2, 0.9)
            px = inst.round_to_tick(entry_fill - direction * r * stop_dist)
            if px == entry_fill:
                px = entry_fill - direction * tick
            orders.append(_Order(f"{plan_id}-EXIT", exit_side, contracts, px, t))
    return orders, t


def generate(conn: sqlite3.Connection, cfg: SynthConfig | None = None, rules: RiskRules = DEFAULT_RULES,
             inst: Instrument = MNQ) -> SynthResult:
    """Fill an initialised (empty) database with synthetic accounts, plans and executions."""
    cfg = cfg or SynthConfig()
    rng = random.Random(cfg.seed)
    accounts = [f"PA-{i:02d}" for i in range(1, cfg.n_accounts + 1)]
    lead, followers = accounts[0], accounts[1:]
    conn.executemany("INSERT INTO accounts VALUES (?, ?, ?)",
                     [(a, int(a == lead), rules.account_size) for a in accounts])

    writer = _Writer(cfg, rng)
    plans: list[tuple] = []
    truth: set[tuple[str, str, str]] = set()
    price = cfg.start_price
    n_days = 0
    tick = inst.tick_size

    for day in _business_days(cfg.start_date):
        if len(plans) >= cfg.n_plans:
            break
        n_days += 1
        state = DayState()
        t = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=35)
        t += timedelta(minutes=rng.uniform(0, 20))
        setups = rng.randint(*cfg.setups_per_day)

        for _ in range(setups):
            if len(plans) >= cfg.n_plans or t.hour >= 15 and t.minute >= 30 or t.hour >= 16:
                break
            price = inst.round_to_tick(price + rng.gauss(0, 18))
            side = rng.choice(["LONG", "SHORT"])
            direction = 1 if side == "LONG" else -1
            decision = None
            for _try in range(10):
                stop_pts = inst.round_to_tick(rng.triangular(8, 70, 22))
                stop = price - direction * stop_pts
                decision = size_position(price, stop, side, state, rules, inst)
                if decision.code == OK or decision.code in DAY_BLOCKS:
                    break
            if decision is None or decision.code in DAY_BLOCKS:
                break           # day is over for the lead
            if decision.code != OK:
                continue        # never found a stop that fits; skip this setup

            plan_id = f"P{len(plans) + 1:04d}"
            target = inst.round_to_tick(price + direction * 2 * abs(price - stop))
            plans.append((plan_id, _fmt(t), side, price, stop, target, decision.contracts, decision.total_risk))

            entry_fill = price + direction * tick * rng.randint(0, 1)    # 0-1 tick entry slippage
            win = rng.random() < cfg.p_win
            orders, t_close = _plan_orders(plan_id, direction, decision.contracts, entry_fill, stop,
                                           t, win, rng, inst)

            # ---- lead fills + day state --------------------------------------------
            for o in orders:
                writer.fill_order(lead, o, plan_id)
            entry, exits = orders[0], orders[1:]
            gross = direction * sum(o.qty * o.price for o in exits) - direction * entry.qty * entry.price
            net = gross * inst.point_value - entry.qty * 2 * cfg.commission_per_side
            state.record(net)

            # ---- follower copies ----------------------------------------------------
            for acct in followers:
                roll = rng.random()
                copies = [_Order(o.order_id, o.side, o.qty, o.price, o.ts) for o in orders]
                if roll < cfg.p_missing:
                    truth.update((acct, o.order_id, "MISSING") for o in orders)
                    continue
                roll -= cfg.p_missing
                if roll < cfg.p_partial:
                    copies[0].qty -= 1
                    truth.add((acct, copies[0].order_id, "PARTIAL"))
                    last = copies[-1]
                    last.qty -= 1
                    truth.add((acct, last.order_id, "PARTIAL" if last.qty > 0 else "MISSING"))
                    copies = [c for c in copies if c.qty > 0]
                else:
                    roll -= cfg.p_partial
                    if roll < cfg.p_mispriced:
                        copies[0].price += direction * tick * rng.randint(3, 8)
                        truth.add((acct, copies[0].order_id, "MISPRICED"))
                for c in copies:
                    c.ts += timedelta(milliseconds=rng.randint(30, 250))
                    if rng.random() < cfg.p_follower_slip:
                        c.price += (tick if c.side == "BUY" else -tick)
                    writer.fill_order(acct, c, plan_id)

            t = t_close + timedelta(minutes=rng.uniform(3, 40))

    conn.executemany("INSERT INTO trade_plans VALUES (?, ?, ?, ?, ?, ?, ?, ?)", plans)
    conn.executemany("INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", writer.rows)
    conn.commit()
    return SynthResult(n_days=n_days, n_plans=len(plans), n_executions=len(writer.rows), ground_truth=truth)
