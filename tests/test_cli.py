import csv

from mnq_platform import cli, db


def test_run_all_end_to_end(tmp_path, capsys):
    dbp, dash, reports = tmp_path / "t.db", tmp_path / "d.html", tmp_path / "reports"
    cli.main(["run-all", "--db", str(dbp), "--accounts", "5", "--plans", "40", "--seed", "3",
              "--reports", str(reports), "--dashboard", str(dash)])
    out = capsys.readouterr().out
    assert "rebuilt" in out and "STRATEGY" in out
    assert "Cumulative net P&amp;L" in dash.read_text() or "Cumulative net P&L" in dash.read_text()
    assert any(reports.glob("recon_*_summary.csv"))


def test_size_command(capsys):
    cli.main(["size", "--entry", "21000", "--stop", "20980"])
    assert "APPROVED" in capsys.readouterr().out
    cli.main(["size", "--entry", "21000", "--stop", "20980", "--loss-streak", "3"])
    assert "LOSS_STREAK" in capsys.readouterr().out


def test_csv_import_roundtrip(tmp_path, capsys):
    acc, ex = tmp_path / "accounts.csv", tmp_path / "executions.csv"
    with open(acc, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerows([["account_id", "is_lead", "starting_balance"], ["L", 1, 50000], ["F", 0, 50000]])
    with open(ex, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["exec_id", "account_id", "ts", "symbol", "side", "qty", "price", "commission", "order_id"])
        w.writerows([["1", "L", "2026-01-05 10:00:00.000", "MNQ", "buy", 2, 100, 1.0, "A"],
                     ["2", "L", "2026-01-05 10:05:00.000", "MNQ", "sell", 2, 101, 1.0, "B"],
                     ["3", "F", "2026-01-05 10:00:00.100", "MNQ", "buy", 2, 100, 1.0, "A"]])
    dbp = tmp_path / "i.db"
    cli.main(["import", "--db", str(dbp), "--accounts", str(acc), "--executions", str(ex)])
    cli.main(["rebuild", "--db", str(dbp)])
    out = capsys.readouterr().out
    assert "rebuilt 1 round trips from 3 executions" in out
    assert "WARNING open position: F LONG 2" in out
    conn = db.connect(dbp)
    assert conn.execute("SELECT net_pnl FROM round_trips").fetchone()[0] == 2.0
