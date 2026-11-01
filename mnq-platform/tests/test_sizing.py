import pytest

from mnq_platform.config import DEFAULT_RULES as R
from mnq_platform.sizing import (BAD_INPUT, DAILY_LOSS, LOSS_STREAK, OK, STOP_TOO_WIDE, TRADE_CAP, DayState,
                                 max_stop_ticks, risk_per_contract, size_position)


def test_risk_per_contract_includes_buffer_and_fee():
    # 20-point stop = 80 ticks; (80 + 2 buffer) * $0.50 + $1.00 fee = $42.00
    assert risk_per_contract(80) == pytest.approx(42.0)


def test_twenty_point_stop_sizes_five_contracts():
    d = size_position(21000.0, 20980.0)
    assert d.approved and d.code == OK
    assert d.side == "LONG"
    assert d.stop_ticks == 80
    assert d.contracts == 5               # floor(250 / 42) = 5
    assert d.total_risk == pytest.approx(210.0)


def test_tight_stop_is_capped_at_six():
    d = size_position(21000.0, 20990.0)   # 40 ticks -> $22/contract -> 11 would fit
    assert d.approved and d.contracts == 6
    assert any("capped" in n for n in d.notes)


def test_wide_stop_minimum_two_contracts():
    assert size_position(21000.0, 20940.0).contracts == 2           # 60 pts: 2 x $122 = $244
    d = size_position(21000.0, 20938.0)                             # 62 pts: 2 x $126 = $252
    assert not d.approved and d.code == STOP_TOO_WIDE


def test_short_side_inferred_and_mismatch_rejected():
    assert size_position(21000.0, 21015.0).side == "SHORT"
    bad = size_position(21000.0, 21015.0, side="LONG")
    assert not bad.approved and bad.code == BAD_INPUT


@pytest.mark.parametrize("entry,stop", [(21000.1, 20980.0), (21000.0, 21000.0)])
def test_bad_prices_rejected(entry, stop):
    assert size_position(entry, stop).code == BAD_INPUT


def test_trade_cap():
    assert size_position(21000.0, 20980.0, day=DayState(trades_taken=6)).code == TRADE_CAP
    assert size_position(21000.0, 20980.0, day=DayState(trades_taken=5)).approved


def test_loss_streak_blocks_after_three():
    assert size_position(21000.0, 20980.0, day=DayState(trades_taken=3, consecutive_losses=3)).code == LOSS_STREAK
    assert size_position(21000.0, 20980.0, day=DayState(trades_taken=3, consecutive_losses=2)).approved


def test_daily_loss_limit_shrinks_then_blocks():
    assert size_position(21000.0, 20980.0, day=DayState(realized_pnl=-750.0)).code == DAILY_LOSS
    shrunk = size_position(21000.0, 20980.0, day=DayState(realized_pnl=-600.0))   # $150 left
    assert shrunk.approved and shrunk.contracts == 3 and shrunk.total_risk <= 150.0
    assert size_position(21000.0, 20980.0, day=DayState(realized_pnl=-700.0)).code == STOP_TOO_WIDE


def test_profit_on_day_does_not_raise_per_trade_risk():
    d = size_position(21000.0, 20980.0, day=DayState(realized_pnl=+900.0))
    assert d.risk_budget == R.risk_per_trade and d.contracts == 5


def test_day_state_streak_resets_on_non_loss():
    day = DayState()
    for pnl in (-100, -100, 50, -100):
        day.record(pnl)
    assert day.consecutive_losses == 1 and day.trades_taken == 4 and day.realized_pnl == -250


@pytest.mark.parametrize("contracts", range(2, 7))
def test_max_stop_ticks_is_the_exact_boundary(contracts):
    ticks = max_stop_ticks(contracts)
    at = size_position(21000.0, 21000.0 - ticks * 0.25)
    beyond = size_position(21000.0, 21000.0 - (ticks + 1) * 0.25)
    assert at.contracts >= contracts
    assert beyond.contracts < contracts


def test_every_approval_respects_all_limits():
    """Property check over a grid of stops and day states."""
    for stop_ticks in range(4, 300, 3):
        for day_pnl in (-740, -500, -250, 0, 400):
            for streak in (0, 2):
                day = DayState(trades_taken=2, realized_pnl=day_pnl, consecutive_losses=streak)
                d = size_position(21000.0, 21000.0 - stop_ticks * 0.25, day=day)
                if d.approved:
                    assert R.min_contracts <= d.contracts <= R.max_contracts
                    assert d.total_risk <= R.risk_per_trade + 1e-9
                    assert d.total_risk <= R.daily_loss_limit + day_pnl + 1e-9
