"""Post-trade rules audit and account status.

Checks every account-day against the strategy rules, and every trade plan
against the sizing limits. Works on rebuilt round trips, so it audits what
actually happened on each account (copies included), not just the plan.

Account status uses a trailing drawdown on closed-trade equity: an account is
BREACHED once equity falls ``account_drawdown_limit`` below its high-water
mark. Real prop-firm rules often trail intraday unrealized equity and/or lock
the threshold at the starting balance; this closed-trade version is a stated
simplification.
"""
from __future__ import annotations

import pandas as pd

from .config import DEFAULT_RULES, RiskRules


def audit_days(rt: pd.DataFrame, rules: RiskRules = DEFAULT_RULES) -> pd.DataFrame:
    """One row per rule violation found in the round trips."""
    violations = []
    for (acct, day), g in rt.groupby(["account_id", "trade_date"], sort=True):
        g = g.sort_values(["open_ts", "rt_id"])
        if len(g) > rules.max_trades_per_day:
            violations.append((acct, day, "TRADE_CAP", None,
                               f"{len(g)} trades > {rules.max_trades_per_day}"))

        # Daily loss: walk trades in close order; realized P&L may not go below the limit.
        cum = 0.0
        for row in g.sort_values(["close_ts", "rt_id"]).itertuples():
            cum += row.net_pnl
            if cum < -rules.daily_loss_limit - 1e-9:
                violations.append((acct, day, "DAILY_LOSS", row.rt_id,
                                   f"realized ${cum:,.2f} < -${rules.daily_loss_limit:,.0f}"))
                break

        # Loss streak: no trade may be opened after N straight losses (closed before it opened).
        streak = 0
        closed = g.sort_values(["close_ts", "rt_id"]).reset_index(drop=True)
        for row in g.itertuples():
            prior = closed[closed.close_ts <= row.open_ts]
            streak = 0
            for pnl in prior.net_pnl:
                streak = streak + 1 if pnl < 0 else 0
            if streak >= rules.max_consecutive_losses:
                violations.append((acct, day, "LOSS_STREAK", row.rt_id,
                                   f"opened after {streak} straight losses"))
                break

        oversize = g[g.entry_qty > rules.max_contracts]
        for row in oversize.itertuples():
            violations.append((acct, day, "SIZE", row.rt_id,
                               f"{row.entry_qty} contracts > {rules.max_contracts}"))

    return pd.DataFrame(violations, columns=["account_id", "trade_date", "rule", "rt_id", "detail"])


def audit_plans(plans: pd.DataFrame, rules: RiskRules = DEFAULT_RULES) -> pd.DataFrame:
    """Plans that break the sizing limits (should be empty if every plan went through sizing)."""
    bad = []
    for p in plans.itertuples():
        if p.risk_usd > rules.risk_per_trade + 1e-9:
            bad.append((p.plan_id, "RISK", f"${p.risk_usd:,.2f} > ${rules.risk_per_trade:,.0f}"))
        if not rules.min_contracts <= p.contracts <= rules.max_contracts:
            bad.append((p.plan_id, "SIZE", f"{p.contracts} outside {rules.min_contracts}-{rules.max_contracts}"))
    return pd.DataFrame(bad, columns=["plan_id", "rule", "detail"])


def account_status(rt: pd.DataFrame, rules: RiskRules = DEFAULT_RULES) -> pd.DataFrame:
    rows = []
    for acct, g in rt.groupby("account_id", sort=True):
        g = g.sort_values(["close_ts", "rt_id"])
        start = g.starting_balance.iloc[0]
        equity = start + g.net_pnl.cumsum()
        hwm = equity.cummax().clip(lower=start)
        dd = hwm - equity
        breach = dd >= rules.account_drawdown_limit
        rows.append({
            "account_id": acct,
            "trades": len(g),
            "ending_balance": round(equity.iloc[-1], 2),
            "high_water_mark": round(hwm.iloc[-1], 2),
            "max_trailing_dd": round(dd.max(), 2),
            "dd_limit": rules.account_drawdown_limit,
            "status": "BREACHED" if breach.any() else "ACTIVE",
            "breached_at": g.close_ts[breach].iloc[0] if breach.any() else None,
        })
    return pd.DataFrame(rows)
