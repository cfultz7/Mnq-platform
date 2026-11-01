import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mnq_platform import db  # noqa: E402
from mnq_platform.rebuild import Fill, rebuild_round_trips  # noqa: E402
from mnq_platform.synth import SynthConfig, generate  # noqa: E402


def make_fill(n, account, ts, side, qty, price, commission=0.0, order_id=None, plan_id=None):
    """Short-hand fill builder: ts is 'HH:MM:SS' on a fixed date."""
    return Fill(exec_id=f"E{n:04d}", account_id=account, ts=f"2026-01-05 {ts}.000", side=side, qty=qty,
                price=price, commission=commission, order_id=order_id or f"O{n}", plan_id=plan_id)


@pytest.fixture(scope="session")
def synth_db():
    """Full-size synthetic dataset (20 accounts, 300 plans), rebuilt once for the whole test run."""
    conn = db.connect(":memory:")
    db.init_db(conn)
    result = generate(conn, SynthConfig())
    rebuild_round_trips(conn)
    return conn, result
