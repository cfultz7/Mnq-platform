"""Contract specification and risk rules.

Every number the platform enforces lives here so that the sizing module, the
rules audit and the synthetic data generator all read from one source.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    """Micro E-mini Nasdaq-100 (MNQ) contract spec."""

    symbol: str = "MNQ"
    tick_size: float = 0.25    # index points per tick
    point_value: float = 2.0   # USD per index point per contract

    @property
    def tick_value(self) -> float:
        """USD per tick per contract ($0.50 for MNQ)."""
        return self.tick_size * self.point_value

    def round_to_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 2)

    def on_tick_grid(self, price: float) -> bool:
        return abs(price - self.round_to_tick(price)) < 1e-9


@dataclass(frozen=True)
class RiskRules:
    """Per-account trading rules from the strategy spec."""

    account_size: float = 50_000.0
    risk_per_trade: float = 250.0          # max planned $ risk per trade, per account
    daily_loss_limit: float = 750.0        # realized day P&L may not go below -this
    max_trades_per_day: int = 6
    max_consecutive_losses: int = 3        # stop trading for the day after this many straight losses
    min_contracts: int = 2
    max_contracts: int = 6
    # Extra ticks of adverse fill (entry slippage + stop slippage) priced into the
    # risk of every contract, so a stopped-out trade still fits inside the budget.
    slippage_buffer_ticks: int = 2
    # Placeholder fee (entry + exit, per contract). Set to the broker/firm's real rate.
    round_turn_fee: float = 1.00
    # Placeholder trailing drawdown that would close a funded account. Set to the firm's rule.
    account_drawdown_limit: float = 2_000.0


@dataclass(frozen=True)
class ReconConfig:
    """Tolerance used when comparing a copied (follower) fill to the lead fill."""

    price_tolerance_ticks: float = 2.0


MNQ = Instrument()
DEFAULT_RULES = RiskRules()
DEFAULT_RECON = ReconConfig()
