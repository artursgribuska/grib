"""One page per top market of the account-free market screen (scoring/market_screen.py).

The screen's counterpart to the event dossiers, for venues without account ids: the
winner's price path with the estimated public moment, the 48-hour window the screen looks
at, and what in that window drove the score.

    uv run python -m app.screen_pages --top 10
    GRIB_DB_PATH=data/kalshi.duckdb uv run python -m app.screen_pages     # → data/kalshi/screen/
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import duckdb
import pandas as pd

import config
from app.dossier import CHART_JS, CSS, e, price_chart, slug
from scoring import market_screen as ms

EXPLAIN = {
    "cheap_flow": "buying of the eventual winner at ≤ {cheap:.2f} in the window, as a share of all volume before the public moment",
    "rise": "how far the winner's price drifted up over the window (from ≤ {rise_max:.2f})",
    "surge": "trading rate in the window against the market's earlier rate (natural log; +1 ≈ 2.7×)",
    "one_sided": "the winner's share of all buying in the window",
}


def market_page(con, r) -> str:
    start = r.certainty_at - pd.Timedelta(hours=ms.WINDOW_H * 3)
    end = r.certainty_at + pd.Timedelta(hours=6)
    px = con.execute("""
        SELECT time_bucket(INTERVAL 30 MINUTE, ts) AS t, arg_max(winner_price, ts) AS p
        FROM trades_w WHERE market_id = ? AND winner_price IS NOT NULL AND ts BETWEEN ? AND ?
        GROUP BY 1 ORDER BY 1""", [r.market_id, start, end]).df()
    winner = con.execute("SELECT winning_outcome FROM markets WHERE market_id = ?", [r.market_id]).fetchone()[0]
    chart = price_chart({"price": px, "certainty_at": r.certainty_at, "chart_market": r.market_id,
                         "flagged": pd.DataFrame(columns=["market_id"]), "winner": winner,
                         "chart_question": r.question})
    chart = chart.replace("Dots: first buys of flagged accounts (hover for details). ", "")
    feats = "".join(
        f"<tr><td>{e(k.replace('_', ' '))}</td><td class=num>{fmt}</td><td class=num>{getattr(r, k + '_pct'):.0%}</td>"
        f"<td class=muted>{e(EXPLAIN[k].format(cheap=ms.CHEAP, rise_max=ms.RISE_FROM_MAX))}</td></tr>"
        for k, fmt in (("cheap_flow", f"{r.cheap_flow:.1%}"), ("rise", f"{r.rise:+.2f}"),
                       ("surge", f"{r.surge:+.1f}"), ("one_sided", f"{r.one_sided:.0%}")))
    notes = []
    if r.deadline_decay:
        notes.append("This 'No' became certain close to its deadline, so the price drift is not credited.")
    w0 = r.certainty_at - pd.Timedelta(hours=ms.WINDOW_H + ms.GAP_H)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(r.question[:60])}</title><style>{CSS}</style></head>
<body><main>
<p class="muted"><a href="index.html">All screened markets</a></p>
<h1>{e(r.question)}</h1>
<div class="disc">Market screen: worth a closer look, not a finding. Built from trades alone (no account data).</div>
<div class="stats">
  <div class="stat"><b>{r.score:.1f}</b><span>screen score (0–100)</span></div>
  <div class="stat"><b>{e(r.category.replace('_', ' '))}</b><span>category</span></div>
  <div class="stat"><b>{e(winner)}</b><span>result</span></div>
  <div class="stat"><b>${r.market_volume_pre:,.0f}</b><span>traded before public</span></div>
  <div class="stat"><b>${r.window_volume:,.0f}</b><span>traded in the window</span></div>
</div>
<section><h2>Price path</h2>{chart}
<p class="muted">Window screened: {w0:%d %b %H:%M} to {r.certainty_at - pd.Timedelta(hours=ms.GAP_H):%d %b %H:%M} UTC
(the {ms.WINDOW_H} hours before the estimated public moment, {r.certainty_at:%d %b %H:%M} UTC).</p></section>
<section><h2>What drove the score</h2>
<div class="scroll"><table><thead><tr><th>Feature</th><th class=num>Value</th><th class=num>Percentile</th><th>Meaning</th></tr></thead>
<tbody>{feats}</tbody></table></div>
{"".join(f"<p class=muted>{e(n)}</p>" for n in notes)}</section>
</main><script>{CHART_JS}</script></body></html>"""


def build_all(con, top: int = 10, out_dir=None) -> list:
    out_dir = out_dir or config.OUTPUT_DIR / "screen"
    out_dir.mkdir(parents=True, exist_ok=True)
    s = ms.screen(con).head(top)
    made = []
    for r in s.itertuples():
        f = out_dir / f"{slug(r.market_id + ' ' + r.question)}.html"
        f.write_text(market_page(con, r))
        made.append((r, f))
    rows = "".join(f'<tr><td class=num>{i}</td><td class=num>{r.score:.1f}</td><td><a href="{e(f.name)}">{e(r.question)}</a>'
                   f'<small>{e(r.category.replace("_", " "))}</small></td></tr>' for i, (r, f) in enumerate(made, 1))
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    (out_dir / "index.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Market screen</title><style>{CSS}</style></head>
<body><main><h1>Market screen</h1><p class="muted">Top markets by the account-free screen, built {built} UTC.
Worth a closer look, not findings.</p>
<div class="scroll"><table><thead><tr><th>#</th><th class=num>Score</th><th>Market</th></tr></thead>
<tbody>{rows}</tbody></table></div></main></body></html>""")
    return made


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    made = build_all(con, a.top)
    con.close()
    print(f"{len(made)} pages written to {config.OUTPUT_DIR / 'screen'}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
