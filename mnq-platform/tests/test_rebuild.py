import random

import pytest

from mnq_platform.rebuild import Fill, Plan, build_round_trips
from tests.conftest import make_fill as F


def test_simple_long():
    trips, still_open = build_round_trips([F(1, "A", "10:00:00", "BUY", 2, 100.0, 1.0),
                                           F(2, "A", "10:05:00", "SELL", 2, 110.0, 1.0)])
    assert not still_open and len(trips) == 1
    t = trips[0]
    assert t.side == "LONG" and t.entry_qty == 2
    assert t.gross_pnl == 40.0          # 10 pts * 2 contracts * $2
    assert t.commission == 2.0 and t.net_pnl == 38.0
    assert t.trade_date == "2026-01-05"


def test_simple_short():
    trips, _ = build_round_trips([F(1, "A", "10:00:00", "SELL", 3, 100.0),
                                  F(2, "A", "10:05:00", "BUY", 3, 95.0)])
    assert trips[0].side == "SHORT" and trips[0].gross_pnl == 30.0


def test_partial_fills_average_price():
    trips, _ = build_round_trips([F(1, "A", "10:00:00", "BUY", 1, 100.0, order_id="ENT"),
                                  F(2, "A", "10:00:01", "BUY", 1, 100.25, order_id="ENT"),
                                  F(3, "A", "10:10:00", "SELL", 2, 101.0)])
    t = trips[0]
    assert t.avg_entry == pytest.approx(100.125)
    assert t.gross_pnl == pytest.approx(3.5)
    assert t.n_executions == 3


def test_scale_out_is_one_round_trip():
    trips, _ = build_round_trips([F(1, "A", "10:00:00", "BUY", 4, 100.0),
                                  F(2, "A", "10:10:00", "SELL", 2, 105.0),
                                  F(3, "A", "10:20:00", "SELL", 2, 110.0)])
    assert len(trips) == 1
    t = trips[0]
    assert t.gross_pnl == 60.0 and t.max_position == 4 and t.avg_exit == pytest.approx(107.5)
    assert t.close_ts.startswith("2026-01-05 10:20")


def test_scale_in_then_out():
    trips, _ = build_round_trips([F(1, "A", "10:00:00", "SELL", 2, 100.0),
                                  F(2, "A", "10:01:00", "SELL", 2, 102.0),
                                  F(3, "A", "10:02:00", "BUY", 1, 99.0),
                                  F(4, "A", "10:03:00", "BUY", 3, 98.0)])
    t = trips[0]
    assert len(trips) == 1 and t.entry_qty == 4 and t.max_position == 4
    # entry notional 404, exit notional 393, short: (404 - 393) * 2 = 22
    assert t.gross_pnl == 22.0


def test_reversal_splits_trade_and_commission():
    trips, still_open = build_round_trips([F(1, "A", "10:00:00", "BUY", 2, 100.0, 2.0),
                                           F(2, "A", "10:05:00", "SELL", 5, 104.0, 5.0),
                                           F(3, "A", "10:09:00", "BUY", 3, 102.0, 3.0)])
    assert not still_open and len(trips) == 2
    long, short = trips
    assert (long.side, long.entry_qty, long.gross_pnl) == ("LONG", 2, 16.0)
    assert long.commission == pytest.approx(2.0 + 2.0)        # own entry + 2/5 of the reversing fill
    assert (short.side, short.entry_qty, short.gross_pnl) == ("SHORT", 3, 12.0)
    assert short.commission == pytest.approx(3.0 + 3.0)       # 3/5 of reversing fill + own exit
    assert short.open_ts == long.close_ts
    assert long.n_executions == 2 and short.n_executions == 2


def test_open_position_reported_not_counted():
    trips, still_open = build_round_trips([F(1, "A", "10:00:00", "BUY", 2, 100.0),
                                           F(2, "A", "10:05:00", "SELL", 1, 101.0)])
    assert trips == []
    assert len(still_open) == 1 and still_open[0].qty == 1 and still_open[0].side == "LONG"


def test_accounts_are_independent_even_when_interleaved():
    fills = [F(1, "A", "10:00:00", "BUY", 2, 100.0), F(2, "B", "10:00:00", "SELL", 2, 100.0),
             F(3, "B", "10:03:00", "BUY", 2, 99.0), F(4, "A", "10:04:00", "SELL", 2, 101.0)]
    trips, _ = build_round_trips(list(reversed(fills)))
    by_acct = {t.account_id: t for t in trips}
    assert by_acct["A"].gross_pnl == 4.0 and by_acct["B"].gross_pnl == 4.0


def test_same_timestamp_keeps_input_order():
    # Exit then re-entry stamped in the same millisecond must stay two trades.
    fills = [F(1, "A", "10:00:00", "BUY", 2, 100.0), F(2, "A", "10:05:00", "SELL", 2, 101.0),
             F(3, "A", "10:05:00", "BUY", 2, 101.0), F(4, "A", "10:09:00", "SELL", 2, 102.0)]
    trips, _ = build_round_trips(fills)
    assert [t.gross_pnl for t in trips] == [4.0, 4.0]


def test_plan_metrics_r_and_slippage():
    plan = Plan("P1", "LONG", entry=100.0, stop=90.0)
    trips, _ = build_round_trips([F(1, "A", "10:00:00", "BUY", 2, 100.25, plan_id="P1"),
                                  F(2, "A", "10:30:00", "SELL", 2, 110.0, plan_id="P1")], {"P1": plan})
    t = trips[0]
    assert t.plan_id == "P1"
    assert t.initial_risk_usd == 40.0                     # 10 pts * $2 * 2 contracts
    assert t.r_multiple == pytest.approx(39.0 / 40.0)
    assert t.entry_slippage_ticks == pytest.approx(1.0)   # paid one tick more than planned


def test_short_slippage_sign():
    plan = Plan("P1", "SHORT", entry=100.0, stop=110.0)
    trips, _ = build_round_trips([F(1, "A", "10:00:00", "SELL", 2, 99.75, plan_id="P1"),
                                  F(2, "A", "10:30:00", "BUY", 2, 95.0)], {"P1": plan})
    assert trips[0].entry_slippage_ticks == pytest.approx(1.0)   # sold lower = worse


def test_random_sequences_conserve_cash():
    """Sum of round-trip gross P&L must equal total cash flow whenever the account ends flat."""
    rng = random.Random(0)
    for _ in range(200):
        pos, fills, n = 0, [], 0
        for step in range(rng.randint(2, 30)):
            qty = rng.randint(1, 6)
            side = rng.choice(["BUY", "SELL"])
            n += 1
            fills.append(Fill(f"E{n}", "A", f"2026-01-05 10:{step:02d}:00.000", side, qty,
                              round(rng.uniform(90, 110) * 4) / 4))
            pos += qty if side == "BUY" else -qty
        if pos:
            n += 1
            fills.append(Fill(f"E{n}", "A", "2026-01-05 11:00:00.000", "SELL" if pos > 0 else "BUY",
                              abs(pos), 100.0))
        trips, still_open = build_round_trips(fills)
        cash = sum((-f.qty if f.side == "BUY" else f.qty) * f.price for f in fills) * 2.0
        assert not still_open
        assert sum(t.gross_pnl for t in trips) == pytest.approx(cash, abs=0.01 * max(1, len(trips)))
