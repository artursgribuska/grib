"""Dashboard: one self-contained HTML page built from the database.

    uv run python -m app.build              # top 50 alerts -> data/dashboard/index.html
    uv run python -m app.build --top 100

No server and no extra dependencies: the page is static, works offline and can be
shared as a single file. It reads only the derived tables (position_scores,
signal_results, alert_events, alert_wallets, case_files) plus the canonical tables
for headline counts. Wallets are only ever shown in truncated form.
"""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
from cases.bundle import DISCLAIMER, case_id
from scoring.run import ROLLUP_MIN_SCORE
from scoring.score import ALERT_THRESHOLD, LABELS, short_account

OUT = config.DATA_DIR / "dashboard" / "index.html"
SIGNAL_ORDER = ["pre_resolution_timing", "low_probability_conviction", "fresh_single_purpose_wallet",
                "size_anomaly", "category_prior", "coordinated_entry"]
COLORS = {"pre_resolution_timing": "var(--c1)", "low_probability_conviction": "var(--c2)",
          "fresh_single_purpose_wallet": "var(--c3)", "size_anomaly": "var(--c4)",
          "category_prior": "var(--c5)", "coordinated_entry": "var(--c6)"}

e = lambda x: html.escape(str(x), quote=True)


def _list(x) -> list:
    """DuckDB list columns arrive as arrays, lists or NA — normalise to a list."""
    try:
        return [] if x is None or pd.isna(x) is True else list(x)
    except (TypeError, ValueError):
        return list(x)


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def load(con: duckdb.DuckDBPyConnection, top: int) -> dict:
    t = _tables(con)
    alerts = con.execute("SELECT * FROM position_scores ORDER BY score DESC LIMIT ?", [top]).df()
    ids = list(alerts.position_id)
    sigs = con.execute("SELECT * FROM signal_results WHERE position_id IN (SELECT unnest(?))", [ids]).df()
    cases = (con.execute("SELECT position_id, risk_level, risk_confidence, checks_passed, case_json FROM case_files")
             .df() if "case_files" in t else pd.DataFrame(columns=["position_id"]))
    stats = con.execute(f"""SELECT (SELECT count(*) FROM markets), (SELECT count(*) FROM trades),
                                   (SELECT count(*) FROM position_scores),
                                   (SELECT count(*) FROM position_scores WHERE score >= {ALERT_THRESHOLD})""").fetchone()
    events = con.execute("SELECT * FROM alert_events WHERE wallets >= 2 LIMIT 15").df() if "alert_events" in t else pd.DataFrame()
    wallets = con.execute("SELECT * FROM alert_wallets LIMIT 15").df() if "alert_wallets" in t else pd.DataFrame()
    funding = (con.execute("SELECT * FROM funding_clusters LIMIT 15").df() if "funding_clusters" in t else pd.DataFrame())
    scans = (dict(con.execute("SELECT status, count(*) FROM wallet_funding_scans GROUP BY 1").fetchall())
             if "wallet_funding_scans" in t else {})
    bt_path = config.DATA_DIR / "backtest.csv"
    backtest = pd.read_csv(bt_path) if bt_path.exists() else None
    return dict(alerts=alerts, sigs=sigs, cases=cases.set_index("position_id") if len(cases) else cases,
                stats=stats, events=events, wallets=wallets, backtest=backtest,
                funding=funding, scans=scans)


def bar(contrib: dict) -> str:
    parts = []
    for k in SIGNAL_ORDER:
        v = max(0.0, contrib.get(k, 0.0))
        if v > 0:
            parts.append(f'<span style="width:{v}%;background:{COLORS[k]}" title="{e(LABELS[k])}: {v:.1f}"></span>')
    adj = -(contrib.get("hedge_adjustment", 0) + contrib.get("exit_adjustment", 0))
    if adj > 0:
        parts.append(f'<span class="adj" style="width:{adj}%" title="Deductions: −{adj:.1f}"></span>')
    return f'<div class="bar">{"".join(parts)}</div>'


def case_html(c: dict) -> str:
    rr = c["risk_rating"]
    li = lambda xs: "".join(f"<li>{e(x)}</li>" for x in xs)
    return f"""
      <h4>Case file — {e(rr['level'])} risk (confidence {e(rr['confidence'])})</h4>
      <p>{e(c['summary'])}</p>
      <p><b>Rating rationale.</b> {e(rr['rationale'])}</p>
      <h5>Innocent explanations considered</h5>
      <ul>{"".join(f"<li><b>{e(i['explanation'])}</b> — <i>{e(i['fits'])}</i>. {e(i['assessment'])}</li>" for i in c['innocent_explanations'])}</ul>
      <h5>Recommended next steps</h5><ol>{li(c['next_steps'])}</ol>
      <h5>Open questions</h5><ul>{li(c['open_questions'])}</ul>"""


def alert_rows(d: dict) -> str:
    rows = []
    sigs = d["sigs"]
    for rank, a in enumerate(d["alerts"].itertuples(), 1):
        contrib = json.loads(a.contributions_json)
        s = sigs[sigs.position_id == a.position_id]
        sig_list = "".join(
            f'<tr><td><i class="dot" style="background:{COLORS.get(r.signal, "var(--muted)")}"></i>{e(LABELS[r.signal])}</td>'
            f'<td class="num">{r.score:.2f}</td><td class="num">{r.contribution:+.1f}</td><td>{e(r.reason)}</td></tr>'
            for r in s.sort_values("contribution", ascending=False).itertuples())
        for k in ("hedge_adjustment", "exit_adjustment"):
            if contrib.get(k):
                sig_list += f'<tr><td>{e(LABELS[k])}</td><td></td><td class="num">{contrib[k]:+.1f}</td><td></td></tr>'
        case = d["cases"].loc[a.position_id] if len(d["cases"]) and a.position_id in d["cases"].index else None
        badge = (f'<span class="badge {e(case.risk_level.lower())}">{e(case.risk_level)}</span>'
                 + ("" if case.checks_passed else ' <span class="badge warn">check</span>')) if case is not None else ""
        body = case_html(json.loads(case.case_json)) if case is not None else "<p class=muted>No case file yet.</p>"
        rows.append(f"""
    <details class="alert" data-cat="{e(a.category)}">
      <summary>
        <span class="rank">{rank}</span>
        <span class="score">{a.score:.1f}</span>
        {bar(contrib)}
        <span class="q">{e(a.question)}<small>{e(a.category.replace('_', ' '))} · {e(short_account(a.account_id))} ·
          ${a.position_notional:,.0f} on '{e(a.outcome)}' at {a.avg_price:.2f}</small></span>
        {badge}
      </summary>
      <div class="body">
        <p class="expl">{e(a.explanation)}</p>
        <p class="muted">Case ref {e(case_id(a.position_id))} · first buy {e(str(a.first_buy_ts)[:16])} ·
          outcome public (est.) {e(str(a.certainty_at)[:16])} · resolved {e(str(a.resolved_at)[:16])}</p>
        <div class="scroll"><table class="sig"><thead><tr><th>Signal</th><th>0–1</th><th>Points</th><th>Why</th></tr></thead>
          <tbody>{sig_list}</tbody></table></div>
        {body}
      </div>
    </details>""")
    return "".join(rows)


def rollups(d: dict) -> str:
    ev, wl = d["events"], d["wallets"]
    ev_rows = "".join(f"<tr><td>{e(r.event_key)}</td><td class=num>{r.wallets}</td><td class=num>{r.low_history_wallets}</td>"
                      f"<td class=num>{r.markets}</td><td class=num>${r.total_usd:,.0f}</td><td class=num>{r.max_score:.1f}</td></tr>"
                      for r in ev.itertuples()) or "<tr><td colspan=6 class=muted>None</td></tr>"
    wl_rows = "".join(f"<tr><td>{e(short_account(r.account_id))}</td><td class=num>{r.alerts}</td>"
                      f"<td class=num>${r.total_usd:,.0f}</td><td class=num>{r.max_score:.1f}</td>"
                      f"<td>{e('; '.join(_list(r.questions)[:3]))}</td></tr>"
                      for r in wl.itertuples()) or "<tr><td colspan=5 class=muted>None</td></tr>"
    fc, scans = d["funding"], d["scans"]
    n_ok = scans.get("ok", 0)
    n_other = sum(v for k, v in scans.items() if k != "ok")
    fc_rows = "".join(f"<tr><td>{e(short_account(r.sender))}</td><td class=num>{r.wallets_funded}</td>"
                      f"<td class=num>{r.flagged_wallets}</td>"
                      f"<td>{e('; '.join(_list(r.flagged_markets)[:3]))}</td></tr>"
                      for r in fc.itertuples()) if len(fc) else ""
    fc_note = (f"{n_ok} wallets scanned; {n_other} could not be (history too old for the public node)." if scans
               else "Funding not scanned — run <code>uv run python -m ingest.funding</code>.")
    funding_html = f"""
  <section><h2>Shared funding</h2>
    <p class="muted">Personal addresses (not contracts, bridges or exchanges) that sent USDC to two or more flagged
      or entry-cohort wallets when they started trading. {fc_note}</p>
    <div class="scroll"><table><thead><tr><th>Sender</th><th>Wallets funded</th><th>Flagged</th><th>Flagged markets</th></tr></thead>
    <tbody>{fc_rows or '<tr><td colspan=4 class=muted>None found</td></tr>'}</tbody></table></div></section>"""
    return funding_html + f"""
  <section><h2>Events with several flagged wallets</h2>
    <p class="muted">Alerts scoring ≥ {ROLLUP_MIN_SCORE:.0f}, grouped by event (related markets on the same story).</p>
    <div class="scroll"><table><thead><tr><th>Event</th><th>Wallets</th><th>Low-history</th><th>Markets</th><th>Total</th><th>Max score</th></tr></thead>
    <tbody>{ev_rows}</tbody></table></div></section>
  <section><h2>Wallets flagged in several markets</h2>
    <div class="scroll"><table><thead><tr><th>Wallet</th><th>Alerts</th><th>Total</th><th>Max score</th><th>Markets</th></tr></thead>
    <tbody>{wl_rows}</tbody></table></div></section>"""


def backtest_html(bt: pd.DataFrame | None) -> str:
    if bt is None or bt.empty:
        return ""
    bt = bt.assign(group=bt.category.where(bt.category.isin(["sports", "crypto_price"]), "other categories"))
    n_scored = int(bt["rank"].max() / bt["top_pct"].max()) if bt["top_pct"].max() else 0
    rows = ""
    for (kind, grp), g in bt.groupby(["kind", "group"]):
        s = g.dropna(subset=["score"])
        rows += (f"<tr><td>{e(kind)}</td><td>{e(grp)}</td><td class=num>{len(g)}</td>"
                 f"<td class=num>{(s.top_pct <= 0.01).mean():.0%}</td><td class=num>{(s.score >= ALERT_THRESHOLD).mean():.0%}</td>"
                 f"<td class=num>{s.score.median():.1f}</td></tr>")
    return f"""
  <section><h2>Backtest (planted trades)</h2>
    <p class="muted">Latest <code>eval.backtest</code> run: synthetic positions planted among ~{n_scored:,} real ones.
      <b>informed</b> = fresh wallet buying the winner 2–36 h before public; <b>early</b> = same, 10–30 days before;
      <b>flipper</b> = informed-style buy sold before public.</p>
    <div class="scroll"><table><thead><tr><th>Planted</th><th>Markets</th><th>n</th><th>Top 1%</th><th>≥ threshold</th><th>Median score</th></tr></thead>
    <tbody>{rows}</tbody></table></div></section>"""


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--accent:#2f5d8a;
--c1:#2f6db3;--c2:#c2571a;--c3:#2e8b57;--c4:#8a5cb8;--c5:#9a8a3a;--c6:#c23b6b;--adj:#b9b9b2;
--low:#2e8b57;--med:#b7791f;--high:#c0392b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141413;--panel:#1e1e1c;--ink:#ecebe6;
--muted:#9c9b95;--line:#33332f;--accent:#7fb0e0;--c1:#5b9be0;--c2:#e8844a;--c3:#5cbf86;--c4:#b28ce0;--c5:#cdbb62;
--c6:#e86b98;--adj:#55554f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px}
h4{margin:18px 0 6px}h5{margin:12px 0 4px;font-size:14px}.muted{color:var(--muted)}small{display:block;color:var(--muted);font-size:12.5px}
.disc{border-left:3px solid var(--accent);padding:8px 12px;background:var(--panel);margin:12px 0 20px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 14px}.stat b{display:block;font-size:22px}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin:8px 0 12px;font-size:13px}.dot{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}.filters button{border:1px solid var(--line);background:var(--panel);color:var(--ink);
border-radius:14px;padding:3px 10px;cursor:pointer;font-size:13px}.filters button.on{border-color:var(--accent);color:var(--accent)}
.alert{background:var(--panel);border:1px solid var(--line);border-radius:8px;margin-bottom:6px}
.alert summary{display:grid;grid-template-columns:28px 46px 160px 1fr auto;gap:10px;align-items:center;padding:8px 12px;cursor:pointer;list-style:none}
.alert summary::-webkit-details-marker{display:none}.rank{color:var(--muted);text-align:right}.score{font-weight:600;font-variant-numeric:tabular-nums}
.bar{display:flex;height:12px;background:var(--line);border-radius:3px;overflow:hidden}.bar span{display:block;height:100%}.bar .adj{background:repeating-linear-gradient(45deg,var(--adj) 0 3px,transparent 3px 6px)}
.q{min-width:0;overflow-wrap:anywhere}.body{padding:4px 16px 16px;border-top:1px solid var(--line)}.expl{font-weight:500}
.badge{font-size:12px;border-radius:10px;padding:1px 8px;border:1px solid currentColor}.badge.low{color:var(--low)}.badge.medium{color:var(--med)}
.badge.high{color:var(--high)}.badge.warn{color:var(--high)}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.sig td:first-child{white-space:nowrap}
@media (max-width:640px){.alert summary{grid-template-columns:24px 40px 1fr;}.alert summary .bar{grid-column:2/4}.alert summary .q{grid-column:1/4}
.alert summary .badge{grid-column:1/4;justify-self:start}.sig{min-width:560px}.body{padding:4px 10px 14px}}
"""

JS = """
document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  const c=b.dataset.cat;document.querySelectorAll('.alert').forEach(a=>{a.style.display=(c==='all'||a.dataset.cat===c)?'':'none'})}));
"""


def render(d: dict) -> str:
    n_markets, n_trades, n_pos, n_alert = d["stats"]
    cats = sorted(d["alerts"].category.unique())
    legend = "".join(f'<span><i class="dot" style="background:{COLORS[k]}"></i>{e(LABELS[k])}</span>' for k in SIGNAL_ORDER)
    legend += '<span><i class="dot" style="background:var(--adj)"></i>Hedge / exit deductions</span>'
    filters = '<button class="on" data-cat="all">All</button>' + "".join(
        f'<button data-cat="{e(c)}">{e(c.replace("_", " "))}</button>' for c in cats)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Integrity Analyst Alerts</title>
<style>{CSS}</style></head><body><main>
  <h1>Integrity Analyst — alerts</h1>
  <p class="muted">Built {built} from public Polymarket data.</p>
  <div class="disc">{e(DISCLAIMER)}</div>
  <div class="stats">
    <div class="stat"><b>{n_markets:,}</b>markets</div><div class="stat"><b>{n_trades:,}</b>trade rows</div>
    <div class="stat"><b>{n_pos:,}</b>positions scored</div><div class="stat"><b>{n_alert:,}</b>at/above review threshold ({ALERT_THRESHOLD:.0f})</div>
  </div>
  <h2>Top {len(d['alerts'])} alerts</h2>
  <p class="muted">Bar length = points from each signal (out of 100); hatched = deductions. Click a row for the
    signal reasons and, where written, the AI case file.</p>
  <div class="legend">{legend}</div>
  <div class="filters">{filters}</div>
  {alert_rows(d)}
  {rollups(d)}
  {backtest_html(d['backtest'])}
</main><script>{JS}</script></body></html>"""


def build(con: duckdb.DuckDBPyConnection, top: int = 50, out=OUT):
    page = render(load(con, top))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50)
    args = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    print(f"Dashboard written to {build(con, args.top)}")


if __name__ == "__main__":
    main()
