"""Performance metrics over rebuilt round trips.

Conventions
* A win is net P&L > 0, a loss is net P&L < 0; a scratch (exactly 0) is neither
  but still counts as a trade in the win-rate denominator.
* Profit factor = gross dollars won / gross dollars lost (inf if no losses).
* Average loser R is reported as a positive magnitude (e.g. 0.95R).
* Drawdown is measured on closed-trade equity, starting from the account's
  starting balance, and reported as a positive dollar amount.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Iterable

import pandas as pd


def load_round_trips(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT rt.*, a.is_lead, a.starting_balance FROM round_trips rt "
        "JOIN accounts a USING (account_id) ORDER BY account_id, close_ts, rt_id", conn)
    for col in ("open_ts", "close_ts"):
        df[col] = pd.to_datetime(df[col])
    return df


def max_drawdown(pnl: Iterable[float], start_equity: float = 0.0) -> float:
    """Largest peak-to-trough drop of cumulative P&L, as a positive number."""
    peak = equity = start_equity
    worst = 0.0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return round(worst, 2)


def drawdown_series(pnl: pd.Series) -> pd.Series:
    """Underwater curve (<= 0) of cumulative P&L, starting from a zero peak."""
    equity = pnl.cumsum()
    peak = equity.cummax().clip(lower=0.0)
    return equity - peak


def summarize(rt: pd.DataFrame) -> dict:
    """Headline stats for any set of round trips."""
    n = len(rt)
    if n == 0:
        return {"trades": 0}
    wins = rt[rt.net_pnl > 0]
    losses = rt[rt.net_pnl < 0]
    gross_won = wins.net_pnl.sum()
    gross_lost = -losses.net_pnl.sum()
    with_r = rt.dropna(subset=["r_multiple"])
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "profit_factor": (gross_won / gross_lost) if gross_lost > 0 else math.inf,
        "net_pnl": round(rt.net_pnl.sum(), 2),
        "avg_win_usd": round(wins.net_pnl.mean(), 2) if len(wins) else 0.0,
        "avg_loss_usd": round(-losses.net_pnl.mean(), 2) if len(losses) else 0.0,
        "avg_win_r": with_r[with_r.net_pnl > 0].r_multiple.mean() if len(wins) else float("nan"),
        "avg_loss_r": -with_r[with_r.net_pnl < 0].r_multiple.mean() if len(losses) else float("nan"),
        "expectancy_r": with_r.r_multiple.mean() if len(with_r) else float("nan"),
        "avg_entry_slippage_ticks": rt.entry_slippage_ticks.mean(),
        "commission": round(rt.commission.sum(), 2),
    }


def by_account(rt: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for acct, g in rt.groupby("account_id", sort=True):
        g = g.sort_values(["close_ts", "rt_id"])
        s = summarize(g)
        s["account_id"] = acct
        s["role"] = "LEAD" if g.is_lead.iloc[0] else "FOLLOWER"
        s["max_drawdown"] = max_drawdown(g.net_pnl, g.starting_balance.iloc[0])
        s["ending_balance"] = round(g.starting_balance.iloc[0] + g.net_pnl.sum(), 2)
        rows.append(s)
    cols = ["account_id", "role", "trades", "win_rate", "profit_factor", "avg_win_r", "avg_loss_r",
            "expectancy_r", "net_pnl", "max_drawdown", "ending_balance", "avg_entry_slippage_ticks"]
    return pd.DataFrame(rows)[cols]


def time_of_day(rt: pd.DataFrame, bucket_minutes: int = 30) -> pd.DataFrame:
    """Results grouped by the time the trade was opened."""
    df = rt.copy()
    df["bucket"] = df.open_ts.dt.floor(f"{bucket_minutes}min").dt.strftime("%H:%M")
    out = df.groupby("bucket").agg(
        trades=("net_pnl", "size"),
        win_rate=("net_pnl", lambda s: (s > 0).mean()),
        avg_r=("r_multiple", "mean"),
        net_pnl=("net_pnl", "sum"),
    ).reset_index()
    return out


def rolling_expectancy(rt: pd.DataFrame, window: int = 30) -> pd.Series:
    """Rolling mean R over the last ``window`` trades (in close order)."""
    s = rt.sort_values(["close_ts", "rt_id"]).r_multiple.reset_index(drop=True)
    return s.rolling(window, min_periods=window).mean()


def format_summary(s: dict) -> str:
    if not s.get("trades"):
        return "no trades"
    pf = "inf" if math.isinf(s["profit_factor"]) else f"{s['profit_factor']:.2f}"
    return (f"trades {s['trades']} | win rate {s['win_rate']:.1%} | profit factor {pf} | "
            f"avg win {s['avg_win_r']:.2f}R | avg loss {s['avg_loss_r']:.2f}R | "
            f"expectancy {s['expectancy_r']:+.3f}R | net ${s['net_pnl']:,.2f}")
