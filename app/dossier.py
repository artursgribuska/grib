"""Event dossiers: one page per story, for triaging a surge of alerts as a whole.

    uv run python -m app.dossier                 # the 10 highest-ranked events
    uv run python -m app.dossier --top 25

Analysts triage stories, not single positions: the February 2026 strikes on Iran
produced 167 alerts on one event. A dossier shows, for one event (related markets on the
same story): the winning outcome's price path with every flagged buy marked and the
estimated public moment; when the flagged money came in, relative to the news; every
flagged account (truncated) with its signals, history and funding links to the others;
and links to the AI case files that exist. Deterministic — no model calls.

Output: <outputs>/dossiers/<event>.html and an index; the dashboard's event table links to them.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
from cases.bundle import case_id
from scoring.run import ROLLUP_MIN_SCORE
from scoring.score import ALERT_THRESHOLD, LABELS, short_account

e = lambda x: html.escape(str(x), quote=True)
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 720, 230, 44, 16, 14, 30
LEAD_BUCKETS = [(0, 6, "< 6 h"), (6, 24, "6–24 h"), (24, 72, "1–3 days"), (72, 168, "3–7 days"), (168, 1e9, "> 7 days")]


def slug(event_key: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", event_key.lower()).strip("-")[:60] or "event"
    return f"{base}-{hashlib.sha1(event_key.encode()).hexdigest()[:6]}"


def top_events(con, n: int) -> list[str]:
    return [r[0] for r in con.execute("""
        SELECT pe.event_key FROM position_events pe JOIN position_scores ps USING (position_id)
        GROUP BY 1 ORDER BY max(ps.score) DESC LIMIT ?""", [n]).fetchall()]


def load_event(con, event_key: str) -> dict:
    pos = con.execute("""
        SELECT ps.*, pe.event_rank, s.market_volume_pre
        FROM position_scores ps JOIN position_events pe USING (position_id)
        JOIN market_state s USING (market_id)
        WHERE pe.event_key = ? ORDER BY ps.score DESC""", [event_key]).df()
    flagged = pos[pos.score >= ROLLUP_MIN_SCORE].copy()
    # the market to chart: the one with most flagged money
    chart_mid = (flagged if len(flagged) else pos).groupby("market_id").position_notional.sum().idxmax()
    st = con.execute("SELECT question, winning_outcome, certainty_at, resolved_at FROM market_state WHERE market_id = ?",
                     [chart_mid]).fetchone()
    first = flagged[flagged.market_id == chart_mid].first_buy_ts.min() if len(flagged) else pos.first_buy_ts.min()
    start = min(first, st[2]) - (st[2] - min(first, st[2])) * 0.25 - pd.Timedelta(hours=6)
    px = con.execute("""
        SELECT time_bucket(INTERVAL 30 MINUTE, ts) AS t, arg_max(winner_price, ts) AS p
        FROM trades_w WHERE market_id = ? AND notional >= 5 AND winner_price IS NOT NULL AND ts BETWEEN ? AND ?
        GROUP BY 1 ORDER BY 1""", [chart_mid, start, st[3]]).df()
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    links = set()
    if "funding_edges" in tables and len(flagged):
        accts = list(flagged.account_id.unique())
        links = {tuple(sorted(r)) for r in con.execute(
            "SELECT a, b FROM funding_edges WHERE a IN (SELECT unnest(?)) AND b IN (SELECT unnest(?))",
            [accts, accts]).fetchall()}
    low = {}
    if len(flagged):
        low = dict(con.execute("""
            SELECT account_id, coalesce(lifetime_markets, dataset_markets) <= 5 FROM account_profile
            WHERE account_id IN (SELECT unnest(?))""", [list(flagged.account_id.unique())]).fetchall())
    return {"event": event_key, "positions": pos, "flagged": flagged, "chart_market": chart_mid,
            "chart_question": st[0], "winner": st[1], "certainty_at": st[2], "price": px,
            "funding_links": links, "low_history": low,
            "n_markets": pos.market_id.nunique()}


def price_chart(d: dict) -> str:
    px = d["price"]
    if px.empty:
        return "<p class=muted>No price data for this market.</p>"
    t0, t1 = px.t.min(), max(px.t.max(), d["certainty_at"])
    span = max((t1 - t0).total_seconds(), 1)
    x = lambda t: PAD_L + (W - PAD_L - PAD_R) * (t - t0).total_seconds() / span
    y = lambda p: PAD_T + (H - PAD_T - PAD_B) * (1 - p)
    pts = " ".join(f"{x(t):.1f},{y(p):.1f}" for t, p in zip(px.t, px.p))
    grid = "".join(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                   f'<text x="{PAD_L - 6}" y="{y(v) + 4:.1f}" class="tick" text-anchor="end">{v:.0%}</text>'
                   for v in (0, 0.25, 0.5, 0.75, 1))
    # x ticks: start, middle, end
    xt = "".join(f'<text x="{x(t):.1f}" y="{H - 8}" class="tick" text-anchor="{a}">{t:%d %b %H:%M}</text>'
                 for t, a in ((t0, "start"), (t0 + (t1 - t0) / 2, "middle"), (t1, "end")))
    pub = x(d["certainty_at"])
    public = (f'<line x1="{pub:.1f}" x2="{pub:.1f}" y1="{PAD_T}" y2="{H - PAD_B}" class="public"/>'
              f'<text x="{pub - 4:.1f}" y="{PAD_T + 10}" class="tick" text-anchor="end">public (est.)</text>')
    fl = d["flagged"][d["flagged"].market_id == d["chart_market"]]
    marks = []
    for r in fl.itertuples():
        if not (t0 <= r.first_buy_ts <= t1):
            continue
        near = px.iloc[(px.t - r.first_buy_ts).abs().argmin()].p
        marks.append(f'<circle cx="{x(r.first_buy_ts):.1f}" cy="{y(near):.1f}" r="4.5" class="mark">'
                     f'<title>{e(short_account(r.account_id))}: ${r.position_notional:,.0f} at {r.avg_price:.2f}, '
                     f'first buy {r.first_buy_ts:%d %b %H:%M} UTC, score {r.score:.0f}</title></circle>')
    data = json.dumps([[t.strftime("%d %b %H:%M"), round(float(p), 3), round(x(t), 1)] for t, p in zip(px.t, px.p)])
    return f"""
<figure class="chart"><svg viewBox="0 0 {W} {H}" role="img" aria-label="Price of '{e(d['winner'])}' over time with flagged buys">
{grid}{xt}{public}
<polyline points="{pts}" class="line"/>
{"".join(marks)}
<line class="cross" x1="0" x2="0" y1="{PAD_T}" y2="{H - PAD_B}" visibility="hidden"/>
<rect class="hit" x="{PAD_L}" y="{PAD_T}" width="{W - PAD_L - PAD_R}" height="{H - PAD_T - PAD_B}"/>
</svg><div class="tip" hidden></div>
<figcaption class="muted">Implied price of the winning outcome ('{e(d['winner'])}') in “{e(d['chart_question'])}”, UTC.
Dots: first buys of flagged accounts (hover for details). Dashed: the estimated moment the outcome became public.</figcaption>
<script type="application/json" class="pts">{data}</script></figure>"""


CHART_JS = """
document.querySelectorAll('figure.chart').forEach(f=>{
  const svg=f.querySelector('svg'),tip=f.querySelector('.tip'),cross=f.querySelector('.cross');
  const pts=JSON.parse(f.querySelector('.pts').textContent);if(!pts.length)return;
  f.querySelector('.hit').addEventListener('mousemove',ev=>{
    const r=svg.getBoundingClientRect(),vx=(ev.clientX-r.left)*svg.viewBox.baseVal.width/r.width;
    let best=pts[0];for(const p of pts){if(Math.abs(p[2]-vx)<Math.abs(best[2]-vx))best=p}
    cross.setAttribute('x1',best[2]);cross.setAttribute('x2',best[2]);cross.setAttribute('visibility','visible');
    tip.hidden=false;tip.textContent=best[0]+' UTC · '+Math.round(best[1]*100)+'%';
    tip.style.left=(best[2]*r.width/svg.viewBox.baseVal.width)+'px'});
  f.querySelector('.hit').addEventListener('mouseleave',()=>{tip.hidden=true;cross.setAttribute('visibility','hidden')});
});
"""


def lead_bars(flagged: pd.DataFrame) -> str:
    if flagged.empty:
        return ""
    lead = (flagged.certainty_at - flagged.first_buy_ts).dt.total_seconds() / 3600
    rows = []
    mx = 1
    counts = []
    for lo, hi, label in LEAD_BUCKETS:
        m = (lead >= lo) & (lead < hi)
        counts.append((label, int(m.sum()), float(flagged[m].position_notional.sum())))
        mx = max(mx, int(m.sum()))
    for label, n, usd in counts:
        rows.append(f'<tr><td>{label}</td><td class="barcell"><span class="hbar" style="width:{100 * n / mx:.0f}%"></span></td>'
                    f'<td class=num>{n}</td><td class=num>${usd:,.0f}</td></tr>')
    return ('<table class="leads"><thead><tr><th>First buy before public</th><th></th><th class=num>Accounts</th>'
            f'<th class=num>Money</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def account_table(d: dict, case_dir) -> str:
    fl = d["flagged"]
    if fl.empty:
        return "<p class=muted>No flagged positions.</p>"
    linked = {}
    for a, b in d["funding_links"]:
        linked.setdefault(a, set()).add(b)
        linked.setdefault(b, set()).add(a)
    rows = []
    for r in fl.head(60).itertuples():
        contrib = json.loads(r.contributions_json)
        tops = ", ".join(LABELS[k] for k, v in sorted(contrib.items(), key=lambda kv: -kv[1])[:2] if v > 0)
        lead = (r.certainty_at - r.first_buy_ts).total_seconds() / 3600
        cid = case_id(r.position_id)
        files = list(case_dir.glob(f"*_{cid}.html")) if case_dir.exists() else []
        cf = f'<a href="../cases/{e(files[0].name)}">{e(cid)}</a>' if files else e(cid)
        fund = ", ".join(short_account(x) for x in sorted(linked.get(r.account_id, ()))) or "—"
        rows.append(f"<tr><td class=num>{r.score:.0f}</td><td>{e(short_account(r.account_id))}"
                    f"{' <small>low history</small>' if d['low_history'].get(r.account_id) else ''}</td>"
                    f"<td>{e(r.question[:60])}</td><td class=num>${r.position_notional:,.0f}</td>"
                    f"<td class=num>{r.avg_price:.2f}</td><td class=num>{lead:,.1f} h</td><td>{e(tops)}</td>"
                    f"<td>{fund}</td><td>{cf}</td></tr>")
    more = f'<p class=muted>Showing 60 of {len(fl)}.</p>' if len(fl) > 60 else ""
    return ('<div class="scroll"><table><thead><tr><th class=num>Score</th><th>Account</th><th>Market</th>'
            '<th class=num>Cost</th><th class=num>Avg price</th><th class=num>Before public</th><th>Top signals</th>'
            f'<th>Funding links</th><th>Case</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>{more}')


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#191919;--muted:#666;--line:#e3e2dc;--series:#257de0;--surface:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141413;--panel:#191919;--ink:#efefef;
--muted:#9c9c9c;--line:#33332f;--series:#338ced;--surface:#191919}}
:root[data-theme=dark]{--bg:#141413;--panel:#191919;--ink:#efefef;--muted:#9c9c9c;--line:#33332f;--series:#338ced;--surface:#191919}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 56px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.muted,small{color:var(--muted)}small{font-size:12px}a{color:var(--series)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px}.stat b{display:block;font-size:20px}
.chart{position:relative;margin:0;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px}
svg{width:100%;height:auto;display:block}.grid{stroke:var(--line);stroke-width:1}.tick{fill:var(--muted);font-size:11px}
.line{fill:none;stroke:var(--series);stroke-width:2;stroke-linejoin:round}
.mark{fill:var(--series);stroke:var(--surface);stroke-width:2}.public{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:5 4}
.cross{stroke:var(--muted);stroke-width:1}.hit{fill:transparent;cursor:crosshair}
.tip{position:absolute;top:8px;transform:translateX(-50%);background:var(--ink);color:var(--panel);font-size:12px;padding:2px 8px;
border-radius:4px;pointer-events:none;white-space:nowrap}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.leads{max-width:560px;background:var(--panel);border:1px solid var(--line);border-radius:8px}.barcell{width:45%}
.hbar{display:block;height:10px;background:var(--series);border-radius:0 4px 4px 0;min-width:2px}
.disc{border-left:3px solid var(--muted);padding:6px 12px;background:var(--panel);margin:10px 0}
"""


def render(d: dict, case_dir) -> str:
    fl, pos = d["flagged"], d["positions"]
    at = int((pos.score >= ALERT_THRESHOLD).sum())
    low_n = sum(1 for a in fl.account_id.unique() if d["low_history"].get(a))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Event dossier</title><style>{CSS}</style></head>
<body><main>
<p class="muted"><a href="index.html">All dossiers</a> · built {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>
<h1>{e(d['event'])}</h1>
<div class="disc">Anomalous activity warranting review, based on statistical signals. Not a legal finding; it does not
identify or accuse any person. Accounts are pseudonymous and shown truncated.</div>
<div class="stats">
 <div class="stat"><b>{d['n_markets']}</b>market(s)</div>
 <div class="stat"><b>{len(pos):,}</b>candidate positions</div>
 <div class="stat"><b>{len(fl):,}</b>flagged (score ≥ {ROLLUP_MIN_SCORE:.0f})</div>
 <div class="stat"><b>{at:,}</b>at/above threshold ({ALERT_THRESHOLD:.0f})</div>
 <div class="stat"><b>{low_n:,}</b>low-history accounts</div>
 <div class="stat"><b>${fl.position_notional.sum():,.0f}</b>flagged money</div>
</div>
<h2>Price path</h2>{price_chart(d)}
<h2>When the flagged money came in</h2>{lead_bars(fl)}
<h2>Flagged accounts</h2>
<p class="muted">Funding links: other flagged accounts on this event funded by the same personal address, or by / to this
account directly (where funding was scanned).</p>
{account_table(d, case_dir)}
</main><script>{CHART_JS}</script></body></html>"""


def build_all(con, top: int = 10, out_dir=None) -> list:
    out_dir = out_dir or config.OUTPUT_DIR / "dossiers"
    out_dir.mkdir(parents=True, exist_ok=True)
    case_dir = config.OUTPUT_DIR / "cases"
    made = []
    for ev in top_events(con, top):
        d = load_event(con, ev)
        f = out_dir / f"{slug(ev)}.html"
        f.write_text(render(d, case_dir))
        made.append((ev, f, len(d["flagged"]), float(d["positions"].score.max())))
    rows = "".join(f'<tr><td><a href="{e(f.name)}">{e(ev)}</a></td><td class=num>{n}</td><td class=num>{mx:.0f}</td></tr>'
                   for ev, f, n, mx in made)
    (out_dir / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Event dossiers</title><style>{CSS}</style></head>
<body><main><h1>Event dossiers</h1><p class="muted">Highest-ranked events (stories), strongest first.
<a href="../dashboard/index.html">Dashboard</a></p>
<div class="scroll"><table><thead><tr><th>Event</th><th class=num>Flagged</th><th class=num>Top score</th></tr></thead>
<tbody>{rows}</tbody></table></div></main></body></html>""")
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    made = build_all(con, a.top)
    con.close()
    for ev, f, n, mx in made:
        print(f"{mx:>5.0f}  {n:>4} flagged  {ev[:70]}")
    print(f"\nWritten to {config.OUTPUT_DIR / 'dossiers'}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
