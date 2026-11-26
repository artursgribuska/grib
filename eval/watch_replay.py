"""Replay the live watch over history: lead time on the known cases, and alert volume.

    uv run python -m watch.live --replay        # uses data/known_cases.duckdb

For each known-case market, the watch is replayed every 2 hours over the 14 days before
the outcome became public, using only trades up to each moment. For every other market
(the ordinary universe), the same replay at 6-hour steps measures how many alerts a team
would receive, and how often an alerted long shot actually won compared with the odds
the market gave it — the honest test of whether the alerts carry information.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from watch.live import _account_facts, case_lead_times, load_trades, replay

DAYS = 14


def _replay_markets(con, market_ids, step_h, facts, seen) -> pd.DataFrame:
    st = con.execute("SELECT market_id, certainty_at FROM market_state WHERE market_id IN (SELECT unnest(?))",
                     [list(market_ids)]).df().set_index("market_id").certainty_at
    frames = []
    for mid in market_ids:
        if mid not in st.index:
            continue
        end = st[mid].to_pydatetime()
        tr = load_trades(con, [mid])
        tr = tr.assign(resolved_at=end)        # replay as if the market closed when the outcome became public
        frames.append(replay(con, end - timedelta(days=DAYS), end, step_h, trades=tr, facts=facts, markets_seen=seen))
    frames = [f for f in frames if not f.empty]
    return case_lead_times(con, pd.concat(frames)) if frames else pd.DataFrame()


def report(con) -> str:
    from scoring.score import short_account
    facts = _account_facts(con)
    seen = con.execute("SELECT account_id, count(DISTINCT market_id) n FROM trades GROUP BY 1").df() \
        .set_index("account_id").n
    cases = con.execute("SELECT \"case\", market_id, question FROM known_case_markets").df()
    lines = ["# Live watch — replay", "",
             f"Replayed with only the trades available at each moment, over the {DAYS} days before each outcome "
             "became public. Alerts are leads for review, not findings.", ""]
    # ---- known cases (lead time is per market; the 487-market Google case is skipped)
    for key, g in cases.groupby("case"):
        if len(g) > 100:
            continue
        for mid, q in zip(g.market_id, g.question):
            a = _replay_markets(con, [mid], 2, facts, seen)
            if a.empty and len(g) > 5:     # many sibling markets: list only those where something fired
                continue
            lines += [f"## {key}: {q}", ""]
            if a.empty:
                lines += ["No alert fired before the outcome became public.", ""]
                continue
            w = a[a.on_winner & (a.hours_before_public > 0)].sort_values("at")
            lines.append(f"{len(w)} alert(s) on the eventual winner before it was public; "
                         f"{int((~a.on_winner).sum())} on other outcomes.")
            if len(w):
                first = w.iloc[0]
                lines.append(f"First: **{first.hours_before_public:.1f} hours before** — {first.kind.replace('_', ' ')}, "
                             f"{short_account(first.account_id)}, ${first.usd:,.0f} at {first.avg_price:.2f} ({first.detail}).")
            lines.append("")
    # ---- alert volume on ordinary markets
    ordinary = [m for m in con.execute("SELECT market_id FROM market_state").df().market_id
                if m not in set(cases.market_id)]
    a = _replay_markets(con, ordinary, 6, facts, seen)
    market_days = len(ordinary) * DAYS
    lines += ["## Alert volume on ordinary markets", ""]
    if a.empty:
        lines += ["No alerts.", ""]
        return "\n".join(lines) + "\n"
    a = a[a.hours_before_public > 0]
    lines.append(f"{len(a)} alerts over {len(ordinary)} markets × {DAYS} days = "
                 f"**{1000 * len(a) / market_days:.1f} per 1,000 market-days**.")
    for kind, g in a.groupby("kind"):
        implied = g.avg_price.mean()
        lines.append(f"- {kind.replace('_', ' ')}: {len(g)} alerts; the alerted side won **{g.on_winner.mean():.0%}** "
                     f"of the time, against an average entry price (the market's implied chance) of {implied:.0%}.")
    from watch.follow_up import excess_wins
    x = excess_wins(a.assign(won=a.on_winner))
    lines.append(f"- All kinds: the alerted side won **{x['wins']} of {x['resolved']}**; the prices paid implied "
                 f"{x['expected_wins']:.1f} (z = {x['z']:+.1f}; the same test the live scorecard runs).")
    lines.append("")
    return "\n".join(lines) + "\n"
