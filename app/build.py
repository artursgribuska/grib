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

OUT = config.OUTPUT_DIR / "dashboard" / "index.html"
SIGNAL_ORDER = ["pre_resolution_timing", "low_probability_conviction", "fresh_single_purpose_wallet",
                "size_anomaly", "category_prior", "coordinated_entry", "price_leadership", "long_shot_track_record"]
COLORS = {"pre_resolution_timing": "var(--c1)", "low_probability_conviction": "var(--c2)",
          "fresh_single_purpose_wallet": "var(--c3)", "size_anomaly": "var(--c4)",
          "category_prior": "var(--c5)", "coordinated_entry": "var(--c6)",
          "price_leadership": "var(--c7)", "long_shot_track_record": "var(--c8)"}

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
    venues = [r[0] for r in con.execute("SELECT DISTINCT venue FROM markets ORDER BY 1").fetchall()]
    stats = con.execute(f"""SELECT (SELECT count(*) FROM markets), (SELECT count(*) FROM trades),
                                   (SELECT count(*) FROM position_scores),
                                   (SELECT count(*) FROM position_scores WHERE score >= {ALERT_THRESHOLD})""").fetchone()
    events = con.execute("SELECT * FROM alert_events WHERE wallets >= 2 LIMIT 15").df() if "alert_events" in t else pd.DataFrame()
    wallets = con.execute("SELECT * FROM alert_wallets LIMIT 15").df() if "alert_wallets" in t else pd.DataFrame()
    funding = (con.execute("SELECT * FROM funding_clusters LIMIT 15").df() if "funding_clusters" in t else pd.DataFrame())
    scans = (dict(con.execute("SELECT status, count(*) FROM wallet_funding_scans GROUP BY 1").fetchall())
             if "wallet_funding_scans" in t else {})
    reviews = (con.execute("""SELECT position_id, decision, reviewer, note, decided_at FROM (
                    SELECT *, row_number() OVER (PARTITION BY position_id ORDER BY decided_at DESC) rn FROM reviews)
                  WHERE rn = 1""").df().set_index("position_id") if "reviews" in t else pd.DataFrame())
    news: dict = {}
    if "news_queries" in t:
        for mid in con.execute("SELECT market_id FROM news_queries WHERE status = 'ok'").fetchall():
            news[mid[0]] = []
        for mid, ts, dom, title in con.execute("""SELECT market_id, seen_at, domain, title FROM news_articles
                                                  WHERE label IS NOT NULL ORDER BY seen_at""").fetchall():
            news.setdefault(mid, []).append((ts, dom, title))
    bt_path = config.OUTPUT_DIR / "backtest.csv"   # read at call time
    backtest = pd.read_csv(bt_path) if bt_path.exists() else None
    return dict(alerts=alerts, sigs=sigs, cases=cases.set_index("position_id") if len(cases) else cases,
                stats=stats, events=events, wallets=wallets, backtest=backtest,
                funding=funding, scans=scans, reviews=reviews, venues=venues, news=news)


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
        nw = d["news"].get(a.market_id)
        news_line = ""
        if nw is not None:
            before = [r for r in nw if r[0] < a.first_buy_ts]
            if before:
                t0, dom, title = before[0]
                news_line = (f'<p class="newsflag">Relevant news before the first buy: <b>{e(title)}</b> '
                             f'({e(dom)}, {e(str(t0)[:16])} UTC)</p>')
            elif nw:
                t0, dom, title = nw[0]
                news_line = (f'<p class="muted">Earliest relevant news came after the first buy: {e(title)} '
                             f'({e(dom)}, {e(str(t0)[:16])} UTC)</p>')
            else:
                news_line = '<p class="muted">News searched: no relevant headline found.</p>'
        rv = d["reviews"].loc[a.position_id] if len(d["reviews"]) and a.position_id in d["reviews"].index else None
        status = rv.decision if rv is not None else "open"
        cid = case_id(a.position_id)
        rv_line = (f'<p class="muted">Last decision: <b>{e(rv.decision)}</b> by {e(rv.reviewer)} on '
                   f'{e(str(rv.decided_at)[:16])}{" — " + e(rv.note) if rv.note else ""}</p>') if rv is not None else ""
        review = f"""
        <div class="review" data-case="{e(cid)}">
          <h4>Review</h4>{rv_line}
          <div class="rv-row">
            <select aria-label="Decision"><option value="">Decision…</option>
              <option value="confirmed">Confirmed</option><option value="dismissed">Dismissed</option>
              <option value="escalated">Escalated</option><option value="needs_info">Needs info</option></select>
            <input type="text" class="rv-note" placeholder="Note (required to dismiss)" aria-label="Note">
            <button type="button" class="rv-save">Save</button><span class="rv-msg muted"></span>
          </div>
        </div>"""
        badge += f' <span class="badge st-{e(status)}" data-status>{e(status.replace("_", " "))}</span>'
        rows.append(f"""
    <details class="alert" data-cat="{e(a.category)}" data-status="{e(status)}" data-case="{e(cid)}">
      <summary>
        <span class="rank">{rank}</span>
        <span class="score">{a.score:.1f}</span>
        {bar(contrib)}
        <span class="q">{e(a.question)}<small>{e(a.category.replace('_', ' '))} · {e(short_account(a.account_id))} ·
          ${a.position_notional:,.0f} on '{e(a.outcome)}' at {a.avg_price:.2f}</small></span>
        <span class="badges">{badge}</span>
      </summary>
      <div class="body">
        <p class="expl">{e(a.explanation)}</p>
        <p class="muted">Case ref {e(case_id(a.position_id))} · first buy {e(str(a.first_buy_ts)[:16])} ·
          outcome public (est.) {e(str(a.certainty_at)[:16])} · resolved {e(str(a.resolved_at)[:16])}</p>
        {news_line}
        <div class="scroll"><table class="sig"><thead><tr><th>Signal</th><th>0–1</th><th>Points</th><th>Why</th></tr></thead>
          <tbody>{sig_list}</tbody></table></div>
        {review}
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
  <section><h2>Accounts flagged in several markets</h2>
    <div class="scroll"><table><thead><tr><th>Account</th><th>Alerts</th><th>Total</th><th>Max score</th><th>Markets</th></tr></thead>
    <tbody>{wl_rows}</tbody></table></div></section>"""


def live_watch_html(limit: int = 25) -> str:
    """Open-market alerts from the live watch (watch/run_live.py), newest first, if it has run."""
    db = config.DATA_DIR / "live.duckdb"
    if not db.exists():
        return ""
    try:
        con = duckdb.connect(str(db), read_only=True)
        rows = con.execute("""SELECT kind, question, outcome, account_id, first_alerted_at, activity_at, usd, avg_price,
                                     detail FROM watch_alerts
                              ORDER BY first_alerted_at DESC, activity_at DESC LIMIT ?""", [limit]).fetchall()
        n = con.execute("SELECT count(*), count(DISTINCT market_id) FROM watch_alerts").fetchone()
        con.close()
    except duckdb.Error:          # missing table, or the live run holds the file right now
        return ""
    body = "".join(
        f"<tr><td>{e(k.replace('_', ' '))}</td><td>{e(q)}<small>'{e(o)}' · {e(short_account(a))}</small></td>"
        f"<td class=num>${u:,.0f}</td><td class=num>{p:.2f}</td><td>{e(str(act)[:16])}<small>raised {e(str(t)[:16])}</small></td>"
        f"<td>{e(d)}</td></tr>"
        for k, q, o, a, t, act, u, p, d in rows) or "<tr><td colspan=6 class=muted>No alerts yet</td></tr>"
    return f"""
  <section><h2>Live watch — open markets</h2>
    <p class="muted">Alerts on markets that have not resolved yet, raised before anyone knows the winner
      ({n[0]} alerts across {n[1]} markets so far). Leads for review, not findings.</p>
    <div class="scroll"><table><thead><tr><th>Alert</th><th>Market</th><th>Position</th><th>Avg price</th>
      <th>Last activity (UTC)</th><th>Why</th></tr></thead><tbody>{body}</tbody></table></div></section>"""


def known_cases_html() -> str:
    """Headline of the known-cases study (eval/known_cases.py), if it has been run."""
    f = config.DATA_DIR / "known_cases" / "summary.json"
    try:
        sm = json.loads(f.read_text())
    except (OSError, ValueError):
        return ""
    rows = "".join(f"<tr><td>{e(c['case'])}</td><td class=num>{c['markets']}</td><td class=num>{c['candidates']}</td>"
                   f"<td class=num>{c['at_threshold']}</td><td class=num><b>{c['best_rank']:,}</b></td><td>{e(c['about'])}</td></tr>"
                   for c in sm.get("cases", []))
    return f"""
  <section><h2>Validation on publicly reported cases</h2>
    <p class="muted">The unchanged engine was run on markets that were the subject of public reports of suspected
      trading on non-public information, inside the ordinary universe of {sm['positions']:,} positions (rank 1 = top alert).
      It saw trades only. {sm.get('case_share_of_top25', 0)} of the top 25 alerts came from case markets, which hold
      {sm.get('case_positions', 0):,} of the positions. Reports are allegations; see <code>data/known_cases/report.md</code>.</p>
    <div class="scroll"><table><thead><tr><th>Case</th><th>Markets</th><th>Candidates</th><th>≥ threshold</th>
      <th>Best rank</th><th>Why it is a case</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""


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
--c1:#2f6db3;--c2:#c2571a;--c3:#2e8b57;--c4:#8a5cb8;--c5:#9a8a3a;--c6:#c23b6b;--c7:#1f8a8a;--c8:#6b6b2f;--adj:#b9b9b2;
--low:#2e8b57;--med:#b7791f;--high:#c0392b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141413;--panel:#1e1e1c;--ink:#ecebe6;
--muted:#9c9b95;--line:#33332f;--accent:#7fb0e0;--c1:#5b9be0;--c2:#e8844a;--c3:#5cbf86;--c4:#b28ce0;--c5:#cdbb62;
--c6:#e86b98;--c7:#4fc4c4;--c8:#b5b56a;--adj:#55554f}}
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
.badges{display:flex;flex-wrap:wrap;gap:4px;justify-content:flex-end;align-items:center}
table{border-collapse:collapse;width:100%;font-size:13.5px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500}.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.sig td:first-child{white-space:nowrap}
.badge.st-open{color:var(--muted)}.badge.st-confirmed{color:var(--high)}.badge.st-dismissed{color:var(--low)}
.badge.st-escalated,.badge.st-needs_info{color:var(--med)}
.review{margin-top:14px;padding:10px 12px;border:1px dashed var(--line);border-radius:8px}.review h4{margin:0 0 6px}
.rv-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}.rv-row select,.rv-row input,.rv-row button,.toolbar button,.toolbar input{
font:inherit;font-size:13px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.rv-row input{flex:1;min-width:160px}.rv-row button,.toolbar button{cursor:pointer}
.newsflag{border-left:3px solid var(--med);padding:4px 10px;background:var(--panel)}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 10px}
@media (max-width:640px){.alert summary{grid-template-columns:24px 40px 1fr;}.alert summary .bar{grid-column:2/4}.alert summary .q{grid-column:1/4}
.alert summary .badges{grid-column:1/4;justify-self:start}.sig{min-width:560px}.body{padding:4px 10px 14px}}
"""

JS = """
const KEY='grib-review-decisions';
const load=()=>{try{return JSON.parse(localStorage.getItem(KEY)||'[]')}catch(e){return []}};
const store=v=>{try{localStorage.setItem(KEY,JSON.stringify(v))}catch(e){}};
let cat='all';
const openOnly=document.getElementById('open-only');
function applyFilters(){document.querySelectorAll('.alert').forEach(a=>{
  const st=a.dataset.status, closed=(st==='confirmed'||st==='dismissed');
  a.style.display=((cat==='all'||a.dataset.cat===cat)&&!(openOnly.checked&&closed))?'':'none'})}
document.querySelectorAll('.filters button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
  cat=b.dataset.cat;applyFilters()}));
openOnly.addEventListener('change',applyFilters);
function mark(caseId,decision){const a=document.querySelector('.alert[data-case="'+caseId+'"]');if(!a)return;
  a.dataset.status=decision;const b=a.querySelector('[data-status]');b.textContent=decision.replace('_',' ')+' (unsent)';
  b.className='badge st-'+decision}
function count(){const n=load().length;document.getElementById('rv-count').textContent=
  n?n+' decision(s) not yet imported':'No unsent decisions';document.getElementById('rv-export').disabled=!n}
load().forEach(x=>mark(x.case_id,x.decision));count();applyFilters();
document.querySelectorAll('.review').forEach(r=>r.querySelector('.rv-save').addEventListener('click',()=>{
  const d=r.querySelector('select').value,n=r.querySelector('.rv-note').value.trim(),msg=r.querySelector('.rv-msg');
  const who=document.getElementById('rv-who').value.trim();
  if(!d){msg.textContent='Pick a decision.';return}
  if(d==='dismissed'&&!n){msg.textContent='A dismissal needs a note.';return}
  const all=load().filter(x=>x.case_id!==r.dataset.case);
  all.push({case_id:r.dataset.case,decision:d,note:n,reviewer:who,decided_at:new Date().toISOString()});
  store(all);mark(r.dataset.case,d);count();msg.textContent='Saved in this browser — export to record it.';applyFilters()}));
document.getElementById('rv-export').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify(load(),null,1)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);
  a.download='decisions-'+new Date().toISOString().slice(0,10)+'.json';a.click();URL.revokeObjectURL(a.href)});
document.getElementById('rv-clear').addEventListener('click',()=>{if(confirm('Forget unsent decisions in this browser?')){store([]);location.reload()}});
"""


def render(d: dict) -> str:
    n_markets, n_trades, n_pos, n_alert = d["stats"]
    cats = sorted(d["alerts"].category.unique())
    legend = "".join(f'<span><i class="dot" style="background:{COLORS[k]}"></i>{e(LABELS[k])}</span>' for k in SIGNAL_ORDER)
    legend += '<span><i class="dot" style="background:var(--adj)"></i>Hedge / exit deductions</span>'
    filters = '<button class="on" data-cat="all">All</button>' + "".join(
        f'<button data-cat="{e(c)}">{e(c.replace("_", " "))}</button>' for c in cats)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    source = ", ".join("public Polymarket" if v == "polymarket" else v for v in d.get("venues", [])) or "loaded"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Integrity Analyst Alerts</title>
<style>{CSS}</style></head><body><main>
  <h1>Integrity Analyst — alerts</h1>
  <p class="muted">Built {built} from {e(source)} data.</p>
  <div class="disc">{e(DISCLAIMER)}</div>
  <div class="stats">
    <div class="stat"><b>{n_markets:,}</b>markets</div><div class="stat"><b>{n_trades:,}</b>trade rows</div>
    <div class="stat"><b>{n_pos:,}</b>positions scored</div><div class="stat"><b>{n_alert:,}</b>at/above review threshold ({ALERT_THRESHOLD:.0f})</div>
  </div>
  {live_watch_html() if config.DB_PATH == config.DATA_DIR / "integrity.duckdb" else ""}
  <h2>Top {len(d['alerts'])} alerts</h2>
  <p class="muted">Bar length = points from each signal (out of 100); hatched = deductions. Click a row for the
    signal reasons and, where written, the AI case file.</p>
  <div class="legend">{legend}</div>
  <div class="filters">{filters}</div>
  <div class="toolbar">
    <label><input type="checkbox" id="open-only"> Open alerts only</label>
    <input type="text" id="rv-who" placeholder="Your initials" aria-label="Reviewer" size="10">
    <button type="button" id="rv-export">Export decisions</button>
    <button type="button" id="rv-clear">Clear unsent</button>
    <span id="rv-count" class="muted"></span>
  </div>
  <p class="muted">Decisions are kept in this browser until exported; record them with
    <code>uv run python -m app.review import decisions-….json</code>.</p>
  {alert_rows(d)}
  {rollups(d)}
  {known_cases_html()}
  {backtest_html(d['backtest'])}
</main><script>{JS}</script></body></html>"""


def build(con: duckdb.DuckDBPyConnection, top: int = 50, out=None):
    out = out or config.OUTPUT_DIR / "dashboard" / "index.html"   # resolved now, not at import
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
