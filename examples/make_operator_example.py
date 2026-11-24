"""Generate the synthetic operator export in examples/operator/ (deterministic).

    uv run python examples/make_operator_example.py

Ordinary customers bet across six markets over several weeks. One pattern is planted:
two accounts opened the day before back the 9.0 outsider in the CEO-appointment market
a few hours before the announcement. Everything here is made up.
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

OUT = Path(__file__).parent / "operator"
rng = random.Random(11)

MARKETS = [  # id, question, category, opens (days before), resolves, winner, outcomes {label: decimal odds}
    ("M1", "Who will be appointed CEO of Northwind plc?", "personnel", 40, datetime(2026, 10, 14, 9, 0), "Dana Ruiz",
     {"Dana Ruiz": 9.0, "Sam Patel": 1.6, "Lee Chen": 3.5}),
    ("M2", "Riverside FC v Harbour Town — match result", "sports", 6, datetime(2026, 10, 12, 17, 0), "Harbour Town",
     {"Riverside FC": 1.9, "Draw": 3.4, "Harbour Town": 4.2}),
    ("M3", "Best Picture at the Lumen Awards", "awards_entertainment", 30, datetime(2026, 10, 20, 3, 0), "The Long Field",
     {"The Long Field": 2.2, "Glass Harbour": 2.8, "Night Choir": 6.0}),
    ("M4", "Will Northwind announce a merger by 31 October?", "corporate", 25, datetime(2026, 10, 31, 23, 0), "No",
     {"Yes": 3.0, "No": 1.4}),
    ("M5", "Coastal Derby — match result", "sports", 5, datetime(2026, 10, 18, 15, 0), "Coastal Utd",
     {"Coastal Utd": 2.1, "Draw": 3.2, "Bayside": 3.6}),
    ("M6", "Central bank rate decision, October", "economics_data", 20, datetime(2026, 10, 29, 12, 0), "Hold",
     {"Hold": 1.3, "Cut 25bp": 3.8}),
]

customers = [f"C{n:05d}" for n in range(1, 401)]
joined = {c: datetime(2025, 1, 1) + timedelta(days=rng.randint(0, 600)) for c in customers}
bets = []
for mid, q, cat, open_days, resolved, winner, odds in MARKETS:
    start = resolved - timedelta(days=open_days)
    n = rng.randint(250, 450)
    for _ in range(n):
        # ordinary flow: spread over the market's life, more of it near the end
        frac = 1 - rng.random() ** 2.2
        ts = start + (resolved - start) * frac - timedelta(minutes=rng.randint(5, 120))
        outcome = rng.choices(list(odds), weights=[1 / o for o in odds.values()])[0]
        drift = rng.uniform(0.9, 1.1)
        bets.append({"market_id": mid, "account_id": rng.choice(customers), "ts": ts.isoformat(),
                     "outcome": outcome, "decimal_odds": round(odds[outcome] * drift, 2),
                     "stake": round(rng.lognormvariate(3.3, 1.0), 2)})

# planted pattern: two fresh accounts, outsider in the personnel market, hours before announcement
announce = MARKETS[0][4]
for acct, hours, stake in (("C90001", 7.5, 1800.0), ("C90002", 6.2, 1500.0), ("C90001", 5.1, 900.0)):
    joined[acct] = announce - timedelta(days=1, hours=3)
    bets.append({"market_id": "M1", "account_id": acct, "ts": (announce - timedelta(hours=hours)).isoformat(),
                 "outcome": "Dana Ruiz", "decimal_odds": 8.5, "stake": stake})

OUT.mkdir(exist_ok=True)
with open(OUT / "markets.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["market_id", "question", "category", "opened_at", "resolved_at", "winning_outcome"])
    for mid, q, cat, open_days, resolved, winner, _ in MARKETS:
        w.writerow([mid, q, cat, (resolved - timedelta(days=open_days)).isoformat(), resolved.isoformat(), winner])
with open(OUT / "bets.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["market_id", "account_id", "ts", "outcome", "decimal_odds", "stake"])
    w.writeheader()
    w.writerows(sorted(bets, key=lambda b: b["ts"]))
used = {b["account_id"] for b in bets}
with open(OUT / "accounts.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["account_id", "joined_at"])
    for c in sorted(used):
        w.writerow([c, joined[c].isoformat()])
print(f"wrote {len(MARKETS)} markets, {len(bets)} bets, {len(used)} accounts to {OUT}")
