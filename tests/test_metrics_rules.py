import math

import pandas as pd
import pytest

from mnq_platform import metrics, rules
from mnq_platform.config import RiskRules


def _rt(rows):
    """rows: (account, open 'HH:MM', close 'HH:MM', net_pnl, r) on one day."""
    df = pd.DataFrame([{
        "rt_id": i, "account_id": a, "trade_date": "2026-01-05",
        "open_ts": pd.Timestamp(f"2026-01-05 {o}"), "close_ts": pd.Timestamp(f"2026-01-05 {c}"),
        "net_pnl": p, "r_multiple": r, "entry_qty": 2, "commission": 0.0, "entry_slippage_ticks": 0.0,
        "is_lead": 1, "starting_balance": 50_000.0,
    } for i, (a, o, c, p, r) in enumerate(rows)])
    return df


def test_max_drawdown_known_series():
    assert metrics.max_drawdown([100, -50, -80, 60, -200, 300]) == 270.0
    assert metrics.max_drawdown([-100, 50], start_equity=1000) == 100.0
    assert metrics.max_drawdown([10, 20, 30]) == 0.0


def test_summary_stats():
    df = _rt([("A", "10:00", "10:10", 100.0, 1.0), ("A", "10:20", "10:30", -50.0, -0.5),
              ("A", "10:40", "10:50", 0.0, 0.0), ("A", "11:00", "11:10", 200.0, 2.0)])
    s = metrics.summarize(df)
    assert s["trades"] == 4 and s["wins"] == 2 and s["losses"] == 1
    assert s["win_rate"] == 0.5                      # scratch counts in the denominator
    assert s["profit_factor"] == pytest.approx(300 / 50)
    assert s["avg_win_r"] == pytest.approx(1.5)
    assert s["avg_loss_r"] == pytest.approx(0.5)     # magnitude
    assert s["expectancy_r"] == pytest.approx(2.5 / 4)


def test_profit_factor_infinite_without_losses():
    assert math.isinf(metrics.summarize(_rt([("A", "10:00", "10:10", 5.0, 0.1)]))["profit_factor"])


def test_time_of_day_buckets():
    df = _rt([("A", "09:31", "09:40", 10.0, 0.1), ("A", "09:59", "10:05", -10.0, -0.1),
              ("A", "10:00", "10:10", 20.0, 0.2)])
    tod = metrics.time_of_day(df).set_index("bucket")
    assert tod.loc["09:30", "trades"] == 2 and tod.loc["10:00", "trades"] == 1


def test_audit_trade_cap_and_daily_loss():
    # 7 trades in a day; the first 3 lose $260 each (-$780 < -$750), and trade 4 opens after 3 straight losses.
    df = _rt([("A", f"{10 + i}:00", f"{10 + i}:30", -260.0 if i < 3 else 10.0, -1.0) for i in range(7)])
    v = rules.audit_days(df)
    assert set(v.rule) == {"TRADE_CAP", "DAILY_LOSS", "LOSS_STREAK"}


def test_loss_streak_only_counts_closed_trades():
    # Third loss is still open when the fourth trade starts: not a violation.
    ok = _rt([("A", "10:00", "10:05", -50.0, -1), ("A", "10:10", "10:15", -50.0, -1),
              ("A", "10:20", "10:40", -50.0, -1), ("A", "10:30", "10:35", 20.0, 0.4)])
    assert rules.audit_days(ok).empty
    bad = _rt([("A", "10:00", "10:05", -50.0, -1), ("A", "10:10", "10:15", -50.0, -1),
               ("A", "10:20", "10:25", -50.0, -1), ("A", "10:30", "10:35", 20.0, 0.4)])
    assert list(rules.audit_days(bad).rule) == ["LOSS_STREAK"]


def test_account_status_trailing_breach():
    df = _rt([("A", "10:00", "10:05", 1500.0, 1), ("A", "10:10", "10:15", -1000.0, -1),
              ("A", "10:20", "10:25", -1100.0, -1)])
    st = rules.account_status(df, RiskRules(account_drawdown_limit=2000.0)).iloc[0]
    assert st.status == "BREACHED" and st.max_trailing_dd == 2100.0 and st.high_water_mark == 51500.0


def test_synthetic_lead_follows_every_rule(synth_db):
    conn, _ = synth_db
    rt = metrics.load_round_trips(conn)
    lead = rt[rt.is_lead == 1]
    assert rules.audit_days(lead).empty
    plans = pd.read_sql_query("SELECT * FROM trade_plans", conn)
    assert rules.audit_plans(plans).empty
    assert len(lead) == len(plans) == 300
    assert lead.plan_id.notna().all()


def test_every_follower_round_trip_maps_to_a_plan(synth_db):
    conn, _ = synth_db
    rt = metrics.load_round_trips(conn)
    assert rt.plan_id.notna().all()
    assert rt.groupby("account_id").size().max() == 300
