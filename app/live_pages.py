"""One page per live-watch alert: the context a reviewer needs to act before payout.

A live alert arrives as one line. This page shows the alerted outcome's price over the
trades we hold (the last 7 days), the account's own buys on it, what the venue says about
the account (join date, lifetime markets, volume), the account's share of the market's
buying, other alerts on the same market, and whether the market has resolved since.

    uv run python -m app.live_pages        # → data/live/alerts/index.html (also run after each live scan)

Alerts are leads for review, not findings; accounts are pseudonymous and shown truncated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
from app.dossier import CHART_JS, CSS, H, PAD_B, PAD_L, PAD_R, PAD_T, W, e, slug
from scoring.score import short_account

DB = config.DATA_DIR / "live.duckdb"
OUT = config.DATA_DIR / "live" / "alerts"


def page_name(a) -> str:
    """Readable and stable; the account appears only as a short hash (never its address)."""
    acct = hashlib.sha1(f"{a.market_id}:{a.account_id}".encode()).hexdigest()[:6]
    return f"{slug(f'{a.kind} {a.question} {a.outcome}')}-{acct}.html"


def outcome_prices(con, market_id: str, outcome: str) -> pd.DataFrame:
    """Hourly implied price of `outcome`. In a two-outcome market a trade on the other side at p implies 1 − p."""
    return con.execute("""
        WITH n AS (SELECT count(DISTINCT outcome) AS k FROM trades WHERE market_id = ?)
        SELECT time_bucket(INTERVAL 1 HOUR, ts) AS t,
               arg_max(CASE WHEN outcome = ? THEN price WHEN (SELECT k FROM n) = 2 THEN 1 - price END, ts) AS p
        FROM trades WHERE market_id = ? AND notional >= 5
          AND (outcome = ? OR (SELECT k FROM n) = 2)
        GROUP BY 1 ORDER BY 1""", [market_id, outcome, market_id, outcome]).df().dropna()


def chart(px: pd.DataFrame, buys: pd.DataFrame, alert_at, outcome: str) -> str:
    if px.empty:
        return "<p class=muted>No price data for this market.</p>"
    t0, t1 = px.t.min(), px.t.max()
    span = max((t1 - t0).total_seconds(), 1)
    x = lambda t: PAD_L + (W - PAD_L - PAD_R) * (pd.Timestamp(t) - t0).total_seconds() / span
    y = lambda p: PAD_T + (H - PAD_T - PAD_B) * (1 - p)
    grid = "".join(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                   f'<text x="{PAD_L - 6}" y="{y(v) + 4:.1f}" class="tick" text-anchor="end">{v:.0%}</text>'
                   for v in (0, 0.25, 0.5, 0.75, 1))
    xt = "".join(f'<text x="{x(t):.1f}" y="{H - 8}" class="tick" text-anchor="{a}">{t:%d %b %H:%M}</text>'
                 for t, a in ((t0, "start"), (t0 + (t1 - t0) / 2, "middle"), (t1, "end")))
    pts = " ".join(f"{x(t):.1f},{y(p):.1f}" for t, p in zip(px.t, px.p))
    marks = []
    for b in buys.itertuples():
        if not (t0 <= b.ts <= t1 + pd.Timedelta(hours=1)):
            continue
        marks.append(f'<circle cx="{x(b.ts):.1f}" cy="{y(b.price):.1f}" r="{3.5 + min(b.notional, 20000) ** 0.5 / 40:.1f}" '
                     f'class="mark"><title>${b.notional:,.0f} at {b.price:.2f}, {b.ts:%d %b %H:%M} UTC</title></circle>')
    at = ""
    if t0 <= alert_at <= t1 + pd.Timedelta(hours=1):
        xa = min(x(alert_at), W - PAD_R)
        at = (f'<line x1="{xa:.1f}" x2="{xa:.1f}" y1="{PAD_T}" y2="{H - PAD_B}" class="public"/>'
              f'<text x="{xa - 4:.1f}" y="{PAD_T + 10}" class="tick" text-anchor="end">alert</text>')
    data = json.dumps([[t.strftime("%d %b %H:%M"), round(float(p), 3), round(x(t), 1)] for t, p in zip(px.t, px.p)])
    return f"""
<figure class="chart"><svg viewBox="0 0 {W} {H}" role="img" aria-label="Price of '{e(outcome)}' with the account's buys">
{grid}{xt}{at}<polyline points="{pts}" class="line"/>{"".join(marks)}
<line class="cross" x1="0" x2="0" y1="{PAD_T}" y2="{H - PAD_B}" visibility="hidden"/>
<rect class="hit" x="{PAD_L}" y="{PAD_T}" width="{W - PAD_L - PAD_R}" height="{H - PAD_T - PAD_B}"/>
</svg><div class="tip" hidden></div>
<figcaption class="muted">Implied price of '{e(outcome)}', hourly, UTC, over the trades held (the last 7 days at the
latest scan). Dots: this account's buys of '{e(outcome)}', sized by amount (hover for details). Dashed: when the
activity behind the alert happened.</figcaption>
<script type="application/json" class="pts">{data}</script></figure>"""


def account_facts(con, acct: str) -> list[tuple[str, str]]:
    r = con.execute("SELECT first_seen_at, metadata_json FROM accounts WHERE account_id = ?", [acct]).fetchone()
    if not r:
        return [("Venue record", "not available")]
    first, meta = r[0], json.loads(r[1] or "{}")
    out = []
    if meta.get("first_seen_source") == "venue_join_date" and first is not None:
        out.append(("Joined the venue", f"{first:%Y-%m-%d}"))
    for k, label, fmt in (("lifetime_markets_traded", "Markets traded, lifetime", "{:,.0f}"),
                          ("lifetime_volume_usd", "Volume, lifetime", "${:,.0f}"),
                          ("lifetime_realized_pnl_usd", "Realised profit, lifetime", "${:,.0f}"),
                          ("lifetime_biggest_win_usd", "Biggest single win", "${:,.0f}")):
        if meta.get(k) is not None:
            out.append((label, fmt.format(meta[k])))
    return out or [("Venue record", "not available")]


def render(con, a) -> str:
    buys = con.execute("""SELECT ts, price, notional FROM trades
                          WHERE market_id = ? AND account_id = ? AND outcome = ? AND side = 'buy' ORDER BY ts""",
                       [a.market_id, a.account_id, a.outcome]).df()
    sells = con.execute("""SELECT coalesce(sum(notional), 0) FROM trades
                           WHERE market_id = ? AND account_id = ? AND outcome = ? AND side = 'sell'""",
                        [a.market_id, a.account_id, a.outcome]).fetchone()[0]
    market_buy = con.execute("SELECT coalesce(sum(notional), 0) FROM trades WHERE market_id = ? AND side = 'buy'",
                             [a.market_id]).fetchone()[0]
    others = con.execute("""SELECT kind, outcome, account_id, usd, avg_price, first_alerted_at FROM watch_alerts
                            WHERE market_id = ? AND NOT (kind = ? AND outcome = ? AND account_id = ?)
                            ORDER BY first_alerted_at""", [a.market_id, a.kind, a.outcome, a.account_id]).fetchall()
    res = con.execute("SELECT winning_outcome, closed FROM watch_outcomes WHERE market_id = ?", [a.market_id]).fetchone() \
        if "watch_outcomes" in {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()} else None
    status = "open" if not res or not res[1] else (f"resolved: '{res[0]}' won" if res[0] else "closed without a clean result")
    held = float(buys.notional.sum()) if len(buys) else 0.0
    facts = "".join(f"<tr><td>{e(k)}</td><td>{e(v)}</td></tr>" for k, v in account_facts(con, a.account_id))
    other_rows = "".join(f"<tr><td>{e(k.replace('_', ' '))}</td><td>'{e(o)}'</td><td>{e(short_account(acc))}</td>"
                         f"<td class=num>${u:,.0f}</td><td class=num>{p:.2f}</td><td>{t:%Y-%m-%d %H:%M}</td></tr>"
                         for k, o, acc, u, p, t in others) or "<tr><td colspan=6 class=muted>None</td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Live alert: {e(a.question[:60])}</title><style>{CSS}{TRIAGE_CSS}</style></head>
<body><main>
<p class="muted"><a href="index.html">All live alerts</a></p>
<h1>{e(a.question)}</h1>
<div class="disc">Live-watch alert on an open market: anomalous activity warranting review, not a finding. It does not
identify or accuse any person; the account is pseudonymous and shown truncated.</div>
<div class="stats">
  <div class="stat"><b>{e(a.kind.replace('_', ' '))}</b><span>alert</span></div>
  <div class="stat"><b>{e(short_account(a.account_id))}</b><span>account, on '{e(a.outcome)}'</span></div>
  <div class="stat"><b>${held:,.0f}</b><span>bought in the trades held{f', ${sells:,.0f} sold' if sells else ''}</span></div>
  <div class="stat"><b>{held / market_buy:.0%}</b><span>of all buying in this market (held trades)</span></div>
  <div class="stat"><b>{e(status)}</b><span>market status</span></div>
</div>
<p>{e(a.detail)}. Raised {a.first_alerted_at:%Y-%m-%d %H:%M} UTC; the activity behind it ended {a.activity_at:%Y-%m-%d %H:%M} UTC.</p>
{triage_html(con, a)}
<section><h2>Price and this account's buys</h2>{chart(outcome_prices(con, a.market_id, a.outcome), buys, a.activity_at, a.outcome)}</section>
<section><h2>What the venue says about the account</h2><div class="scroll"><table><tbody>{facts}</tbody></table></div>
<p class="muted">A long record of many markets usually points to an active trader rather than an account set up for one bet.</p></section>
<section><h2>Other alerts on this market</h2><div class="scroll"><table><thead><tr><th>Alert</th><th>Side</th><th>Account</th>
<th class=num>Position</th><th class=num>Avg price</th><th>Raised (UTC)</th></tr></thead><tbody>{other_rows}</tbody></table></div></section>
<section><h2>Before deciding</h2><ul>
<li>Is there public news that explains the buying? Search the market's subject around the buy times above.</li>
<li>Does the account trade like this elsewhere? The venue record above gives its lifetime activity.</li>
<li>If a hold on withdrawals is warranted, act before the market resolves.</li></ul></section>
</main><script>{CHART_JS}</script></body></html>"""


ACTION_LABEL = {"close": "Suggests: close", "monitor": "Suggests: monitor", "escalate": "Suggests: escalate"}
TRIAGE_CSS = """
.triage{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0;background:var(--panel)}
.triage h2{margin:0 0 6px;font-size:15px}.act{display:inline-block;font-weight:600;font-size:13px;padding:2px 10px;
border-radius:999px;margin-left:8px;border:1px solid currentColor}
.act.close{color:#0a7d1e}.act.monitor{color:#9a6700}.act.escalate{color:#c42b2c}
@media (prefers-color-scheme:dark){.act.close{color:#53c556}.act.monitor{color:#fbb315}.act.escalate{color:#eb6263}}
.triage p{margin:6px 0}.triage .muted{font-size:12.5px}"""


def triage_html(con, a) -> str:
    from watch.triage import get_note
    n = get_note(con, a.kind, a.market_id, a.outcome, a.account_id)
    if not n:
        return ""
    act = n["suggested_action"]
    checks = "number and language checks passed" if n["passed"] else f"needs attention: {n['problems']}"
    return f"""<section class="triage"><h2>AI first look<span class="act {e(act)}">{e(ACTION_LABEL.get(act, act))}</span></h2>
<p><b>What happened.</b> {e(n['what_happened'])}</p>
<p><b>Strongest ordinary explanation.</b> {e(n['innocent_explanation'])}</p>
<p><b>Why this suggestion.</b> {e(n['action_reason'])}</p>
<p class="muted">Drafted by {e(n['model'])} from the facts on this page only, for a human reviewer; {e(checks)}.
It is a suggestion, not a decision.</p></section>"""


def build_all(con, out_dir=None) -> list:
    out_dir = out_dir or OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    alerts = list(con.execute("""SELECT kind, market_id, question, outcome, account_id, first_alerted_at, activity_at,
                                        usd, avg_price, detail FROM watch_alerts
                                 ORDER BY first_alerted_at DESC, usd DESC""").df().itertuples())
    for a in alerts:
        (out_dir / page_name(a)).write_text(render(con, a))
    from watch.triage import get_note

    def suggestion(a) -> str:
        n = get_note(con, a.kind, a.market_id, a.outcome, a.account_id)
        return e(n["suggested_action"]) if n else "—"
    rows = "".join(f'<tr><td>{a.first_alerted_at:%Y-%m-%d %H:%M}</td><td>{e(a.kind.replace("_", " "))}</td>'
                   f'<td><a href="{e(page_name(a))}">{e(a.question)}</a><small>\'{e(a.outcome)}\' · '
                   f'{e(short_account(a.account_id))}</small></td><td class=num>${a.usd:,.0f}</td>'
                   f'<td>{suggestion(a)}</td></tr>' for a in alerts)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    (out_dir / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Live alerts</title><style>{CSS}</style></head>
<body><main><h1>Live alerts</h1><p class="muted">Alerts raised on open markets, newest first; built {built} UTC.
Leads for review, not findings.</p>
<div class="scroll"><table><thead><tr><th>Raised (UTC)</th><th>Alert</th><th>Market</th><th class=num>Position</th>
<th>AI first look</th></tr></thead>
<tbody>{rows or '<tr><td colspan=5 class=muted>No alerts yet</td></tr>'}</tbody></table></div></main></body></html>""")
    return alerts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=True)
    n = len(build_all(con))
    con.close()
    print(f"{n} alert page(s) written to {OUT}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
