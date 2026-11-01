"""Plotly HTML dashboard: P&L, R-expectancy, drawdown, slippage, time of day, copy health.

Writes one self-contained page. By default plotly.js loads from its CDN; pass
``offline=True`` to inline it (bigger file, works with no network).
"""
from __future__ import annotations

import html
import math
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from . import metrics, rules
from .config import DEFAULT_RULES, RiskRules

# Light-theme palette (roles, not decoration).
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e7e6e2"
LEAD = "#2a78d6"        # categorical slot 1: the lead account / strategy series
CONTEXT = "#b9b8b2"     # followers drawn as recessive context lines
CRITICAL = "#d03b3b"
GOOD = "#0ca30c"


def _layout(fig: go.Figure, title: str, y_title: str = "", x_title: str = "", height: int = 340) -> go.Figure:
    fig.update_layout(
        title=dict(text=title, x=0, xanchor="left", font=dict(size=15, color=TEXT)),
        height=height, margin=dict(l=56, r=16, t=48, b=44),
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE,
        font=dict(family="-apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif", size=12, color=TEXT_2),
        hoverlabel=dict(bgcolor="white", font_color=TEXT, bordercolor=GRID),
        showlegend=False,
        bargap=0.15,
    )
    fig.update_xaxes(title_text=x_title, gridcolor=GRID, zeroline=False, showline=False)
    fig.update_yaxes(title_text=y_title, gridcolor=GRID, zerolinecolor="#cfcec9", zerolinewidth=1)
    return fig


def _fig_html(fig: go.Figure, include_plotlyjs: bool | str = False) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs,
                       config={"displaylogo": False, "responsive": True})


def equity_figure(rt: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    for acct, g in rt.sort_values(["close_ts", "rt_id"]).groupby("account_id", sort=True):
        is_lead = bool(g.is_lead.iloc[0])
        if is_lead:
            continue
        fig.add_trace(go.Scatter(x=g.close_ts, y=g.net_pnl.cumsum(), mode="lines", name=acct,
                                 line=dict(color=CONTEXT, width=1), legendgroup="followers",
                                 showlegend=False,
                                 hovertemplate=f"{acct}<br>%{{x|%b %d %H:%M}}<br>$%{{y:,.0f}}<extra></extra>"))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name="Follower accounts",
                             line=dict(color=CONTEXT, width=1), legendgroup="followers"))
    lead = rt[rt.is_lead == 1].sort_values(["close_ts", "rt_id"])
    fig.add_trace(go.Scatter(x=lead.close_ts, y=lead.net_pnl.cumsum(), mode="lines", name="Lead account",
                             line=dict(color=LEAD, width=2.5),
                             hovertemplate="Lead<br>%{x|%b %d %H:%M}<br>$%{y:,.0f}<extra></extra>"))
    _layout(fig, "Cumulative net P&L by account", "USD", height=400)
    fig.update_layout(showlegend=True, margin=dict(b=72),
                      legend=dict(orientation="h", yanchor="top", y=-0.12, xanchor="left", x=0))
    return fig


def drawdown_figure(rt: pd.DataFrame) -> go.Figure:
    lead = rt[rt.is_lead == 1].sort_values(["close_ts", "rt_id"])
    dd = metrics.drawdown_series(lead.net_pnl.reset_index(drop=True))
    fig = go.Figure(go.Scatter(x=lead.close_ts, y=dd.values, mode="lines", fill="tozeroy",
                               line=dict(color=LEAD, width=2), fillcolor="rgba(42,120,214,0.15)",
                               name="Drawdown", hovertemplate="%{x|%b %d %H:%M}<br>$%{y:,.0f}<extra></extra>"))
    return _layout(fig, "Lead drawdown from peak (USD)", "USD")


def r_hist_figure(rt: pd.DataFrame) -> go.Figure:
    lead = rt[(rt.is_lead == 1) & rt.r_multiple.notna()]
    fig = go.Figure(go.Histogram(x=lead.r_multiple, xbins=dict(size=0.25), marker_color=LEAD,
                                 marker_line=dict(color=SURFACE, width=2), name="Trades",
                                 hovertemplate="%{x} R<br>%{y} trades<extra></extra>"))
    return _layout(fig, "R-multiple distribution (lead)", "Trades", "R multiple")


def expectancy_figure(rt: pd.DataFrame, window: int = 30) -> go.Figure:
    lead = rt[rt.is_lead == 1].sort_values(["close_ts", "rt_id"]).reset_index(drop=True)
    roll = metrics.rolling_expectancy(lead, window)
    fig = go.Figure(go.Scatter(x=list(range(1, len(roll) + 1)), y=roll, mode="lines",
                               line=dict(color=LEAD, width=2), name=f"{window}-trade mean R",
                               hovertemplate="trade %{x}<br>%{y:+.3f} R<extra></extra>"))
    fig.add_hline(y=0, line_color="#8a8984", line_width=1)
    return _layout(fig, f"Rolling {window}-trade R expectancy (lead)", "Mean R", "Trade number")


def slippage_figure(rt: pd.DataFrame) -> go.Figure:
    s = rt.entry_slippage_ticks.dropna().round(2)
    counts = s.value_counts().sort_index()
    fig = go.Figure(go.Bar(x=counts.index, y=counts.values, marker_color=LEAD, width=0.8,
                           marker_line=dict(color=SURFACE, width=2), name="Round trips",
                           hovertemplate="%{x} ticks<br>%{y} round trips<extra></extra>"))
    _layout(fig, "Entry slippage vs. plan, all accounts (ticks, + = worse)", "Round trips", "Ticks", height=320)
    fig.update_xaxes(dtick=1)
    return fig


def time_of_day_figure(rt: pd.DataFrame) -> go.Figure:
    lead = rt[rt.is_lead == 1]
    tod = metrics.time_of_day(lead)
    fig = go.Figure(go.Bar(
        x=tod.bucket, y=tod.avg_r, marker_color=LEAD, marker_line=dict(color=SURFACE, width=2), name="Avg R",
        customdata=list(zip(tod.trades, tod.win_rate, tod.net_pnl)),
        hovertemplate="%{x}<br>avg %{y:+.2f} R<br>%{customdata[0]} trades, "
                      "%{customdata[1]:.0%} won<br>$%{customdata[2]:,.0f} net<extra></extra>"))
    fig.add_hline(y=0, line_color="#8a8984", line_width=1)
    return _layout(fig, "Average R by entry time, 30-min buckets (lead)", "Avg R", "Entry time", height=320)


def flags_figure(flags: pd.DataFrame) -> go.Figure:
    counts = flags.flag_type.value_counts() if len(flags) else pd.Series(dtype=int)
    order = ["MISSING", "PARTIAL", "MISPRICED", "OVERFILL", "WRONG_SIDE", "UNEXPECTED"]
    counts = counts.reindex(order, fill_value=0)
    fig = go.Figure(go.Bar(x=counts.index, y=counts.values, marker_color=LEAD,
                           marker_line=dict(color=SURFACE, width=2), name="Flags",
                           hovertemplate="%{x}<br>%{y} flags<extra></extra>"))
    return _layout(fig, "Copy reconciliation flags by type", "Flags", height=300)


def _fmt(v, kind: str) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    formats = {"pct": "{:.1%}", "r": "{:.2f}R", "x": "{:.2f}", "usd": "${:,.0f}", "tick": "{:+.2f}"}
    if kind == "int":
        return f"{int(v):,}"
    if kind in formats:
        return formats[kind].format(v)
    return html.escape(str(v))


def _table(df: pd.DataFrame, kinds: dict[str, str], max_rows: int | None = None) -> str:
    rows = df if max_rows is None else df.head(max_rows)
    head = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in df.columns)
    body = "".join(
        "<tr>" + "".join(f"<td class='{'num' if kinds.get(c) not in (None, 'text') else ''}'>"
                         f"{_fmt(r[c], kinds.get(c, 'text'))}</td>" for c in df.columns) + "</tr>"
        for _, r in rows.iterrows())
    more = f"<p class='muted'>Showing {len(rows)} of {len(df)} rows.</p>" if len(rows) < len(df) else ""
    return f"<div class='tablewrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}"


def build_dashboard(conn: sqlite3.Connection, out_path: str | Path, risk_rules: RiskRules = DEFAULT_RULES,
                    offline: bool = False) -> Path:
    rt = metrics.load_round_trips(conn)
    if rt.empty:
        raise ValueError("no round trips; run the rebuild first")
    flags = pd.read_sql_query("SELECT * FROM recon_flags ORDER BY trade_date, account_id, order_id", conn)
    lead = rt[rt.is_lead == 1]
    s = metrics.summarize(lead)
    status = rules.account_status(rt, risk_rules)
    per_acct = metrics.by_account(rt).merge(status[["account_id", "max_trailing_dd", "status"]], on="account_id")
    violations = rules.audit_days(rt, risk_rules)
    n_days = lead.trade_date.nunique()
    max_dd_any = per_acct.max_drawdown.max()
    active = int((status.status == "ACTIVE").sum())

    tiles = [
        ("Strategy trades", f"{s['trades']:,}", f"{n_days} trading days"),
        ("Win rate", f"{s['win_rate']:.1%}", f"{s['wins']} W / {s['losses']} L"),
        ("Profit factor", _fmt(s["profit_factor"], "x"), "gross won / gross lost"),
        ("Avg winner / loser", f"{s['avg_win_r']:.2f}R / {s['avg_loss_r']:.2f}R", "net of fees"),
        ("Expectancy", f"{s['expectancy_r']:+.3f}R", "mean R per trade"),
        ("Max account drawdown", f"${max_dd_any:,.0f}", "worst of all accounts"),
        ("Accounts active", f"{active} / {len(status)}", f"${risk_rules.account_drawdown_limit:,.0f} trailing limit"),
        ("Copy flags", f"{len(flags):,}", f"{len(rt):,} account round trips"),
    ]
    tiles_html = "".join(f"<div class='tile'><div class='label'>{html.escape(a)}</div>"
                         f"<div class='value'>{html.escape(b)}</div><div class='muted'>{html.escape(c)}</div></div>"
                         for a, b, c in tiles)

    figs = [equity_figure(rt), drawdown_figure(rt), r_hist_figure(rt), expectancy_figure(rt),
            time_of_day_figure(rt), slippage_figure(rt), flags_figure(flags)]
    # plotly.js is embedded once, with the first chart: inline when offline, else from Plotly's CDN.
    js_mode = True if offline else "cdn"
    charts = "".join(f"<section class='card{' wide' if i == 0 else ''}'>"
                     f"{_fig_html(f, js_mode if i == 0 else False)}</section>"
                     for i, f in enumerate(figs))

    acct_kinds = {"trades": "int", "win_rate": "pct", "profit_factor": "x", "avg_win_r": "r", "avg_loss_r": "r",
                  "expectancy_r": "r", "net_pnl": "usd", "max_drawdown": "usd", "ending_balance": "usd",
                  "avg_entry_slippage_ticks": "tick", "max_trailing_dd": "usd"}
    flag_cols = ["trade_date", "account_id", "order_id", "flag_type", "expected_qty", "filled_qty", "detail"]
    flag_kinds = {"expected_qty": "int", "filled_qty": "int"}
    viol_html = (_table(violations, {}) if len(violations)
                 else "<p class='muted'>No rule violations on any account-day.</p>")

    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MNQ Multi-Account Dashboard</title>
<style>
  :root {{ color-scheme: light; --surface:{SURFACE}; --text:{TEXT}; --text-2:{TEXT_2}; --grid:{GRID}; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#f4f3f0; color:var(--text);
         font:14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  main {{ max-width:1280px; margin:0 auto; padding:24px 16px 48px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 10px; }}
  .muted {{ color:var(--text-2); font-size:12px; }}
  .tiles {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:10px; margin:18px 0; }}
  @media (max-width: 720px) {{ .tiles {{ grid-template-columns:repeat(2, 1fr); }} }}
  .tile {{ background:var(--surface); border:1px solid var(--grid); border-radius:10px; padding:12px 14px; }}
  .tile .label {{ color:var(--text-2); font-size:12px; }} .tile .value {{ font-size:22px; font-weight:600; margin:2px 0; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(min(100%, 520px), 1fr)); gap:12px; }}
  .card {{ background:var(--surface); border:1px solid var(--grid); border-radius:10px; padding:6px; min-width:0; }}
  .card.wide {{ grid-column:1 / -1; }}
  .tablewrap {{ overflow-x:auto; background:var(--surface); border:1px solid var(--grid); border-radius:10px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th, td {{ padding:7px 10px; border-bottom:1px solid var(--grid); text-align:left; white-space:nowrap; }}
  th {{ color:var(--text-2); font-weight:600; text-transform:capitalize; position:sticky; top:0; background:var(--surface); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
</style></head>
<body><main>
  <h1>MNQ multi-account risk &amp; analytics</h1>
  <div class="muted">{len(status)} accounts · {lead.trade_date.min()} to {lead.trade_date.max()} ·
  strategy stats are the lead account; every account's round trips are rebuilt from its own fills.</div>
  <div class="tiles">{tiles_html}</div>
  <div class="grid">{charts}</div>
  <h2>Accounts</h2>
  {_table(per_acct, acct_kinds)}
  <h2>Rule audit</h2>
  {viol_html}
  <h2>Copy reconciliation flags</h2>
  {_table(flags[flag_cols], flag_kinds, max_rows=200) if len(flags) else "<p class='muted'>No flags.</p>"}
</main></body></html>"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out
