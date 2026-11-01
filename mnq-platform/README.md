# MNQ multi-account risk & analytics platform — reference implementation

A Python + SQLite platform for a rules-based MNQ strategy traded on one lead
account and copied to 19 follower accounts. It sizes trades before entry,
rebuilds every account's round trips from raw broker executions, reconciles
each follower's copied fills against the lead every day, audits the trading
rules, and renders a Plotly dashboard.

```
pip install -r requirements.txt
python -m mnq_platform.cli run-all          # synthetic data -> rebuild -> reconcile -> audit -> report -> dashboard.html
python -m pytest -q                         # 47 tests, ~3 s
```

`run-all` creates `mnq.db`, daily reconciliation CSVs in `reports/`, and
`dashboard.html` (add `--offline` to inline plotly.js). A pre-built
`sample_output/dashboard.html` is included.

## Layout

```
mnq_platform/
  config.py      contract spec (tick 0.25, $2/pt) and every risk rule, in one place
  schema.sql     SQLite schema: accounts, trade_plans, executions, round_trips, recon_flags
  db.py          connection, schema setup, CSV import
  sizing.py      pre-trade sizing + day-level rule gates
  rebuild.py     executions -> account-level round trips (+ R multiple, entry slippage)
  reconcile.py   daily lead-vs-follower copy reconciliation and sign-off CSVs
  rules.py       post-trade rule audit and trailing-drawdown account status
  metrics.py     win rate, profit factor, R stats, drawdown, time-of-day, rolling expectancy
  dashboard.py   Plotly HTML dashboard
  synth.py       seeded synthetic data with ground-truth copy faults
  cli.py         command-line entry point
tests/           pytest suite
```

## Spec → code

| Spec item | Where | How it is checked |
|---|---|---|
| 20 copied $50K accounts, one lead | `synth.generate`, `accounts.is_lead` | `test_metrics_rules` |
| $250 risk per account per trade | `sizing.size_position` | `test_every_approval_respects_all_limits` |
| $750 daily loss limit | sizing gate (shrinks budget to remaining room) + `rules.audit_days` | `test_daily_loss_limit_shrinks_then_blocks`, audit tests |
| Six-trade daily cap | sizing gate + audit | `test_trade_cap`, `test_audit_trade_cap_and_daily_loss` |
| Stop after three straight losses | sizing gate + audit (counts only trades closed before the next one opened) | `test_loss_streak_*` |
| Size 2–6 contracts from entry and stop | `sizing.size_position`, `sizing.max_stop_ticks` | boundary test for every size 2–6 |
| Rebuild round trips from executions | `rebuild.build_round_trips` / `rebuild_round_trips` | partials, scale in/out, reversals, cash-conservation property test |
| Win rate, profit factor, avg winner/loser R | `metrics.summarize` | `test_summary_stats` |
| Max account-level drawdown, accounts stay active | `metrics.max_drawdown`, `rules.account_status` | `test_max_drawdown_known_series`, `test_account_status_trailing_breach` |
| Daily reconciliation: missing / partial / mispriced fills | `reconcile.reconcile`, `write_daily_report` | each fault type, VWAP + direction, and an exact match against injected synthetic faults |
| Dashboards: P&L, R-expectancy, drawdown, slippage, time of day | `dashboard.build_dashboard` | `test_run_all_end_to_end` |

## Design decisions

1. **Risk per contract** = (stop ticks + 2 buffer ticks) × $0.50 + round-turn fee. The buffer covers one tick of entry slippage plus one tick of stop slippage, so a normal stop-out never exceeds the $250 plan and three of them never exceed $750.
2. **Contracts** = floor(budget / risk per contract), capped at 6; fewer than 2 is a rejection, not a 1-lot. Budget = min($250, $750 + day's realized P&L).
3. **Loss** means net P&L < 0. A scratch resets the losing streak and counts in the win-rate denominator.
4. **Round trip** = flat to flat. P&L uses average prices, which equals FIFO lot matching once flat. A fill that crosses zero is split, and its commission is prorated by quantity.
5. **R multiple** = net P&L ÷ (|plan entry − plan stop| × $2 × contracts entered). Entry slippage is in ticks vs. the plan, positive = worse.
6. **Reconciliation** matches on the copier's order tag (same `order_id` on lead and copies), aggregates fills to order-level qty and VWAP, and flags price differences over 2 ticks. One order can carry two flags (e.g. PARTIAL + MISPRICED).
7. **Account status** uses a trailing drawdown on closed-trade equity (`account_drawdown_limit`, default $2,000). Fees and the drawdown limit in `config.py` are placeholders; set them to the actual broker/firm values.

## Importing real data

```
python -m mnq_platform.cli import --db real.db --accounts accounts.csv --executions executions.csv [--plans plans.csv]
python -m mnq_platform.cli rebuild   --db real.db
python -m mnq_platform.cli reconcile --db real.db --out reports
python -m mnq_platform.cli audit     --db real.db
python -m mnq_platform.cli report    --db real.db
python -m mnq_platform.cli dashboard --db real.db --out dashboard.html
```

* `accounts.csv`: `account_id, is_lead, starting_balance`
* `executions.csv`: `exec_id, account_id, ts, symbol, side (BUY/SELL), qty, price, order_id` [, `commission`, `plan_id`]
* `plans.csv`: `plan_id, ts, side (LONG/SHORT), entry, stop, contracts, risk_usd` [, `target`]

Timestamps are `YYYY-MM-DD HH:MM:SS.fff`. Without plans, R and slippage are left empty.

Pre-trade check for a single trade:

```
python -m mnq_platform.cli size --entry 21000 --stop 20980 [--trades-today 2 --day-pnl -300 --loss-streak 1]
```

## About the sample data

`synth.py` generates a realistic-shaped dataset (multi-execution fills, scale-outs, copy latency, ≤1 tick copy slippage, and injected missing/partial/mispriced copies). Its win probability and R ranges are generic, not calibrated to any real track record, so the performance numbers it produces are illustrative and will not match figures measured from live fills. What the synthetic data does verify is the mechanics: the lead account obeys every rule by construction, and reconciliation flags exactly the injected faults and nothing else.
