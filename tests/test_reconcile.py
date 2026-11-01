from mnq_platform import db
from mnq_platform.reconcile import daily_summary, reconcile, write_daily_report


def _db(fills):
    conn = db.connect(":memory:")
    db.init_db(conn)
    conn.executemany("INSERT INTO accounts VALUES (?, ?, ?)",
                     [("L", 1, 50000), ("F1", 0, 50000), ("F2", 0, 50000), ("F3", 0, 50000)])
    rows = [(f"E{i}", acct, f"2026-01-05 {ts}.000", "MNQ", side, qty, px, 0.0, oid, None)
            for i, (acct, ts, side, qty, px, oid) in enumerate(fills)]
    conn.executemany("INSERT INTO executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    return conn


LEAD = [("L", "10:00:00", "BUY", 3, 100.00, "P1-ENT"),
        ("L", "10:20:00", "SELL", 3, 110.00, "P1-TP")]


def _types(flags):
    return {(f.account_id, f.order_id, f.flag_type) for f in flags}


def test_clean_copy_within_tolerance_has_no_flags():
    conn = _db(LEAD + [
        ("F1", "10:00:00", "BUY", 2, 100.25, "P1-ENT"), ("F1", "10:00:01", "BUY", 1, 100.50, "P1-ENT"),
        ("F1", "10:20:00", "SELL", 3, 109.75, "P1-TP"),
        ("F2", "10:00:00", "BUY", 3, 100.00, "P1-ENT"), ("F2", "10:20:00", "SELL", 3, 110.00, "P1-TP"),
        ("F3", "10:00:00", "BUY", 3, 100.50, "P1-ENT"), ("F3", "10:20:00", "SELL", 3, 110.00, "P1-TP"),
    ])
    assert reconcile(conn, "2026-01-05") == []      # F3 entry is exactly 2 ticks off: allowed


def test_each_fault_type_is_flagged():
    conn = _db(LEAD + [
        # F1: missing exit + a manual trade the lead never placed
        ("F1", "10:00:00", "BUY", 3, 100.00, "P1-ENT"),
        ("F1", "11:00:00", "SELL", 1, 105.00, "MANUAL-1"), ("F1", "11:05:00", "BUY", 1, 104.00, "MANUAL-1b"),
        # F2: partial + mispriced entry (VWAP 101.0 = 4 ticks worse), matching partial exit
        ("F2", "10:00:00", "BUY", 2, 101.00, "P1-ENT"), ("F2", "10:20:00", "SELL", 2, 110.00, "P1-TP"),
        # F3: overfilled entry, wrong-side exit
        ("F3", "10:00:00", "BUY", 4, 100.00, "P1-ENT"), ("F3", "10:20:00", "BUY", 3, 110.00, "P1-TP"),
    ])
    got = _types(reconcile(conn, "2026-01-05"))
    assert got == {
        ("F1", "P1-TP", "MISSING"),
        ("F1", "MANUAL-1", "UNEXPECTED"), ("F1", "MANUAL-1b", "UNEXPECTED"),
        ("F2", "P1-ENT", "PARTIAL"), ("F2", "P1-ENT", "MISPRICED"), ("F2", "P1-TP", "PARTIAL"),
        ("F3", "P1-ENT", "OVERFILL"), ("F3", "P1-TP", "WRONG_SIDE"),
    }


def test_mispriced_uses_vwap_and_direction():
    conn = _db(LEAD + [
        # 1 @ 100.00 + 1 @ 101.25 + 1 @ 101.25 -> VWAP 100.8333 = 3.33 ticks worse on a BUY
        ("F1", "10:00:00", "BUY", 1, 100.00, "P1-ENT"), ("F1", "10:00:01", "BUY", 2, 101.25, "P1-ENT"),
        ("F1", "10:20:00", "SELL", 3, 110.00, "P1-TP"),
        # selling 3 ticks HIGHER than the lead is better, but still outside tolerance
        ("F2", "10:00:00", "BUY", 3, 100.00, "P1-ENT"), ("F2", "10:20:00", "SELL", 3, 110.75, "P1-TP"),
        ("F3", "10:00:00", "BUY", 3, 100.00, "P1-ENT"), ("F3", "10:20:00", "SELL", 3, 110.00, "P1-TP"),
    ])
    flags = {(f.account_id, f.order_id): f for f in reconcile(conn, "2026-01-05")}
    assert flags[("F1", "P1-ENT")].price_diff_ticks == 3.33
    assert flags[("F2", "P1-TP")].price_diff_ticks == -3.0
    assert len(flags) == 2


def test_reconcile_is_idempotent_and_reports_write(tmp_path):
    conn = _db(LEAD + [("F1", "10:00:00", "BUY", 3, 100.0, "P1-ENT")])
    reconcile(conn, "2026-01-05")
    reconcile(conn, "2026-01-05")
    n = conn.execute("SELECT COUNT(*) FROM recon_flags").fetchone()[0]
    assert n == 1 + 2 + 2          # F1 missing exit; F2 and F3 missing both orders
    summary = daily_summary(conn, "2026-01-05")
    assert [r["status"] for r in summary] == ["REVIEW", "REVIEW", "REVIEW", "OK"]   # F1, F2, F3, L
    s_path, f_path = write_daily_report(conn, "2026-01-05", tmp_path)
    assert s_path.exists() and len(f_path.read_text().strip().splitlines()) == 1 + n


def test_synthetic_faults_found_exactly(synth_db):
    """Every injected copy fault is flagged, and nothing else is."""
    conn, result = synth_db
    flags = reconcile(conn)
    assert len(result.ground_truth) > 0
    assert _types(flags) == result.ground_truth
