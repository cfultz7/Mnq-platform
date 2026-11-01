"""Pre-trade position sizing and rule gating.

Given an entry and a stop price, decide how many MNQ contracts (2–6) can be
traded without breaking any of the per-account rules:

* planned risk per trade  <= $250
* realized day P&L never below -$750 (the next trade's risk must fit what is left)
* at most 6 trades per day
* no new trade after 3 straight losses that day

Risk per contract = (stop distance in ticks + slippage buffer ticks) * tick value
                    + round-turn fee
so that a trade stopped out with normal slippage still lands inside the budget.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import DEFAULT_RULES, MNQ, Instrument, RiskRules

# Decision codes (stable strings, so callers can branch on them).
OK = "OK"
BAD_INPUT = "BAD_INPUT"
TRADE_CAP = "TRADE_CAP"
LOSS_STREAK = "LOSS_STREAK"
DAILY_LOSS = "DAILY_LOSS"
STOP_TOO_WIDE = "STOP_TOO_WIDE"
DAY_BLOCKS = frozenset({TRADE_CAP, LOSS_STREAK, DAILY_LOSS})


@dataclass
class DayState:
    """Running state of one account's trading day."""

    trades_taken: int = 0
    realized_pnl: float = 0.0
    consecutive_losses: int = 0

    def record(self, net_pnl: float) -> None:
        """Update the day after a round trip closes. A loss is net P&L < 0."""
        self.trades_taken += 1
        self.realized_pnl += net_pnl
        self.consecutive_losses = self.consecutive_losses + 1 if net_pnl < 0 else 0


@dataclass(frozen=True)
class SizingDecision:
    approved: bool
    code: str
    side: str | None
    entry: float
    stop: float
    contracts: int = 0
    stop_ticks: int = 0
    risk_per_contract: float = 0.0
    total_risk: float = 0.0
    risk_budget: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __str__(self) -> str:  # pragma: no cover - formatting only
        head = f"{'APPROVED' if self.approved else 'REJECTED'} [{self.code}]"
        if self.approved:
            body = (f"{self.side} {self.contracts} MNQ @ {self.entry:.2f}, stop {self.stop:.2f} "
                    f"({self.stop_ticks} ticks) | risk ${self.total_risk:,.2f} of ${self.risk_budget:,.2f} "
                    f"budget (${self.risk_per_contract:,.2f}/contract)")
        else:
            body = ""
        notes = "".join(f"\n  - {n}" for n in self.notes)
        return f"{head} {body}{notes}".rstrip()


def risk_per_contract(stop_ticks: int, rules: RiskRules = DEFAULT_RULES, inst: Instrument = MNQ) -> float:
    return (stop_ticks + rules.slippage_buffer_ticks) * inst.tick_value + rules.round_turn_fee


def size_position(entry: float, stop: float, side: str | None = None, day: DayState | None = None,
                  rules: RiskRules = DEFAULT_RULES, inst: Instrument = MNQ) -> SizingDecision:
    """Return the approved contract count for a trade, or a rejection with the reason.

    ``side`` is 'LONG' or 'SHORT'; when omitted it is inferred from where the stop sits.
    ``day`` is the account's state so far today (a fresh day if omitted).
    """
    day = day or DayState()
    base = dict(entry=entry, stop=stop)

    # --- input checks -----------------------------------------------------
    if not (inst.on_tick_grid(entry) and inst.on_tick_grid(stop)):
        return SizingDecision(False, BAD_INPUT, side, notes=(f"prices must be on the {inst.tick_size} tick grid",), **base)
    if entry == stop:
        return SizingDecision(False, BAD_INPUT, side, notes=("stop equals entry",), **base)
    inferred = "LONG" if stop < entry else "SHORT"
    if side is None:
        side = inferred
    side = side.upper()
    if side not in ("LONG", "SHORT"):
        return SizingDecision(False, BAD_INPUT, side, notes=("side must be LONG or SHORT",), **base)
    if side != inferred:
        where = "below" if side == "LONG" else "above"
        return SizingDecision(False, BAD_INPUT, side, notes=(f"a {side} stop must be {where} the entry",), **base)

    # --- day-level gates --------------------------------------------------
    if day.trades_taken >= rules.max_trades_per_day:
        return SizingDecision(False, TRADE_CAP, side,
                              notes=(f"{day.trades_taken} trades taken; cap is {rules.max_trades_per_day}",), **base)
    if day.consecutive_losses >= rules.max_consecutive_losses:
        return SizingDecision(False, LOSS_STREAK, side,
                              notes=(f"{day.consecutive_losses} straight losses; done for the day",), **base)
    loss_capacity = rules.daily_loss_limit + day.realized_pnl  # $ that can still be lost today
    if loss_capacity <= 0:
        return SizingDecision(False, DAILY_LOSS, side,
                              notes=(f"day P&L ${day.realized_pnl:,.2f} is at the -${rules.daily_loss_limit:,.0f} limit",),
                              **base)

    # --- sizing -----------------------------------------------------------
    stop_ticks = round(abs(entry - stop) / inst.tick_size)
    rpc = risk_per_contract(stop_ticks, rules, inst)
    budget = min(rules.risk_per_trade, loss_capacity)
    notes: list[str] = []
    if budget < rules.risk_per_trade:
        notes.append(f"budget cut to ${budget:,.2f} by remaining daily loss room")

    fit = math.floor(budget / rpc + 1e-9)
    contracts = min(fit, rules.max_contracts)
    if fit > rules.max_contracts:
        notes.append(f"{fit} contracts would fit; capped at {rules.max_contracts}")
    if contracts < rules.min_contracts:
        notes.append(f"{rules.min_contracts} contracts x ${rpc:,.2f} = ${rules.min_contracts * rpc:,.2f} "
                     f"exceeds ${budget:,.2f} budget")
        return SizingDecision(False, STOP_TOO_WIDE, side, stop_ticks=stop_ticks, risk_per_contract=rpc,
                              risk_budget=budget, notes=tuple(notes), **base)

    return SizingDecision(True, OK, side, contracts=contracts, stop_ticks=stop_ticks, risk_per_contract=rpc,
                          total_risk=round(contracts * rpc, 2), risk_budget=budget, notes=tuple(notes), **base)


def max_stop_ticks(contracts: int, rules: RiskRules = DEFAULT_RULES, inst: Instrument = MNQ) -> int:
    """Widest stop (in ticks) that still allows ``contracts`` on a fresh day."""
    per_contract_budget = (rules.risk_per_trade / contracts) - rules.round_turn_fee
    return math.floor(per_contract_budget / inst.tick_value + 1e-9) - rules.slippage_buffer_ticks
