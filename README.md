# Mnq-platform
## What it does

A risk and analytics platform for a futures strategy where one lead account's trades are copied to several follower accounts. It's built around MNQ (Micro E-mini Nasdaq-100) futures.

- **Pre-trade sizing.** Give it an entry and a stop and it tells you how many contracts to trade (2 to 6) while keeping risk under $250 per trade. It also blocks new trades after six in a day, after three straight losses, or when the day is too close to the $750 loss limit.
- **Round-trip rebuild.** Raw broker executions go into SQLite, and the platform rebuilds each account's trades from flat to flat. It handles partial fills, scaling in and out, and reversals, and records P&L, R multiple and entry slippage for every trade.
- **Copy reconciliation.** Each day it compares every follower account to the lead, order by order, and flags fills that are missing, partial, overfilled, on the wrong side, or more than 2 ticks off the lead's price. Results are written to a CSV for each day.
- **Rules audit.** Checks every account-day against the trading rules and tracks each account's trailing drawdown against the firm's limit.
- **Dashboard.** A Plotly HTML report with equity curves for every account, drawdown, R distribution, rolling expectancy, slippage, results by time of day, and the reconciliation flags.

The repo includes a seeded synthetic data generator, with copy errors injected on purpose, so the whole pipeline can be run and tested without real account data. Performance numbers from the sample data are illustrative only. Real broker exports can be loaded with the CSV importer.

**Built with:** Python, SQLite, pandas, Plotly, pytest (47 tests)
