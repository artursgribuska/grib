# Integrity Analyst — demo (prediction markets)

An AI compliance analyst for betting operators and prediction-market venues. It watches
trading activity, flags **anomalous activity warranting review** (e.g. large, early,
confident bets on the eventual winner), scores it with explainable signals and writes
a case file. This demo runs on **public Polymarket data**, on an operator's own bet export
through the same engine, and (market-level, without accounts) on Kalshi's public trades.

**Does it work?** Run blind on markets behind five publicly reported suspected-leak episodes,
two of them added only after the engine was frozen, the unchanged engine ranks those stories
**#1, #2, #4, #5 (tied) and #7 of 128 events**. Its top alert in the Maduro market matches the trading in
the CFTC's first event-contract insider complaint (436,760 Yes shares). Replayed with only the data
available at each moment, the live watch flags the cases 10 to 248 hours before the news. On
planted trades, 82–84% of informed buys land in the top 1%. For venues that publish no accounts
(Kalshi's public feed), a market screen working from trades alone ranks the same stories #1, #2, #3,
#14 and #19 of 126. See *Validation* below,
`docs/demo.md` for a five-minute walkthrough and `docs/investor-brief.md` for the business case.

> **Disclaimer:** demo built on public data. Alerts are statistical signals, not legal
> findings, and do not identify or accuse any person. Wallets are pseudonymous.

## Quick start

**One command for everything:** `./grib` lists every step (`./grib demo`, `./grib pipeline`,
`./grib watch --every 3`, `./grib review queue`, `./grib test`, …); each is a shortcut for the
`uv run python -m …` command shown below.

**Try it first (≈10–15 minutes):** `uv sync`, set `GRIB_ANTHROPIC_API_KEY` (optional: without it
you get everything but the AI case files), then `uv run python -m demo`. It loads 40 ordinary
markets plus the Maduro and Nobel cases into its own `data/demo/`, runs the unchanged engine, the
live-watch replay and the account-free market screen, and writes event dossiers and a dashboard. On a fresh machine it took 15 minutes; the two cases
ranked #1 and #2 of 2,389 positions (the ordinary sample changes as new markets resolve, so exact
ranks vary run to run), flagged 16.6 h and 10 h before the news.

**Day to day:** `uv run python -m pipeline` runs everything below in one go and is safe to repeat
(e.g. daily): new markets are added, caches are reused, and AI case files are only written for
alerts that are new or whose evidence changed. Each run writes a digest to `data/digest/latest.md`. Then open `data/dashboard/index.html`, review alerts,
and record decisions (see *Reviewing alerts*). Step by step, first time:

1. Install [uv](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. `uv sync` (installs Python 3.12 + dependencies)
3. `uv run python -m ingest.run` — downloads ~200 recently resolved markets and all their trades into `data/integrity.duckdb` (≈20 min first time; cached afterwards)
4. `uv run python -m ingest.enrich` — fetches public wallet facts (join date, lifetime markets) for candidate wallets (≈12 min first time)
5. `uv run python -m scoring.run` — scores every candidate position and prints the top 25 alerts
6. `uv run python -m ingest.funding`, then `uv run python -m scoring.run` again — optional: scans on-chain USDC / pUSD deposits into flagged wallets and their entry-cohort peers (Polygon public RPC, no key; ≈10 min; `--rescan` to redo) so scoring can see shared funders
7. Put your Anthropic API key in the environment (`export GRIB_ANTHROPIC_API_KEY=sk-ant-...`, or a `.env` file — see `.env.example`; plain `ANTHROPIC_API_KEY` also works locally, but Claude Code cloud sessions strip it)
8. `uv run python -m ingest.news` — optional: searches GDELT (Google News as fallback) for news about each alerted market (needs the key; ≈1 min per 10 markets)
9. `uv run python -m cases.run` — writes AI case files for the top 10 alerts into `data/cases/` (`--bundles-only` works without a key)
10. `uv run python -m eval.backtest --markets 200 --seed 1` (and `uv run python -m eval.known_cases`) — plants synthetic trades in an in-memory copy of the data and measures how the engine ranks them (≈10 s)
11. `uv run python -m app.build` — writes a self-contained dashboard to `data/dashboard/index.html` (top 50 alerts with score bars, signal reasons, case files, event/wallet roll-ups and the latest backtest); open it in any browser
12. `uv run python -m app.dossier` — writes one **event dossier** per top story to `data/dossiers/`: the winning outcome's price path with every flagged first buy marked against the estimated public moment, when the flagged money came in, and the flagged accounts with funding links and case files. The dashboard's event table links to them (the pipeline builds them)
13. `uv run pytest` — run the tests

## Running on a server

A `Dockerfile` runs the live watch on a schedule (a scan every 3 hours) with `data/` mounted so
the databases persist:

    docker build -t integrity-analyst .
    docker run -d -v $PWD/data:/app/data -e GRIB_ANTHROPIC_API_KEY -e GRIB_ALERT_WEBHOOK_URL integrity-analyst
    docker run --rm -v $PWD/data:/app/data -e GRIB_ANTHROPIC_API_KEY integrity-analyst python -m pipeline

Tests run on every push (`.github/workflows/ci.yml`); they need no network and no API key.

## Your own data (operators)

An operator can run the same engine on its own bet export (milestone 5). Prepare up to three
CSVs — markets, bets (decimal odds + stake, or price + size) and optionally accounts (opening
date) — then load them into a **separate** database and run the pipeline on it:

    uv run python -m ingest.csv_loader --venue mybook --markets markets.csv --trades bets.csv --accounts accounts.csv
    GRIB_DB_PATH=data/mybook.duckdb uv run python -m pipeline --skip-ingest --no-funding

(set `GRIB_DB_PATH` for the loader too). Outputs then go to `data/mybook/` so they never mix
with the public demo. Column definitions are in `ingest/csv_loader.py`; rows that fail checks
(bad timestamps, odds ≤ 1, unknown markets …) are counted and reported, not silently dropped.
`examples/operator/` holds a synthetic sportsbook export (2,021 bets, 6 markets) with one planted
pattern — two day-old accounts backing a 9.0 outsider in a CEO-appointment market hours before
the announcement. The engine ranks those two accounts first (scores 73 and 71) and the case
writer rates both High.

## Reviewing alerts

Each alert on the dashboard has a review box: pick **confirmed**, **dismissed**,
**escalated** or **needs info**, add a note (required to dismiss) and save. Decisions stay
in your browser until you click **Export decisions**, then record them:

    uv run python -m app.review import decisions-2026-11-24.json --by <initials>

or straight from the terminal:

    uv run python -m app.review queue                          # open alerts
    uv run python -m app.review decide CASE-99B29C1E78 needs_info --note "KYC requested" --by ag
    uv run python -m app.review history CASE-99B29C1E78        # audit trail
    uv run python -m app.review stats                          # which signals reviewers confirm

Decisions are append-only (who, when, why, and the score at the time) so the trail survives for
audit. `stats` compares confirmed and dismissed alerts per signal and reports a *lift*; it warns
until there are 20 closed decisions and never changes weights by itself.

## Case files

For each alert, `cases/bundle.py` builds an **evidence bundle** (JSON): market facts, account
facts (truncated ID only), the account's trades, the market's price path, every signal's
reason and numbers, market and event context, funding and news evidence where available, a
rule-based starting risk rating, and the data limitations. The model
(`claude-sonnet-5-5`) sees only this bundle and returns a structured case file: summary,
timeline, evidence per signal, innocent explanations considered, risk rating with
confidence, next steps and open questions.

Each case is written as Markdown and as a printable `.html` (A4, self-contained: open it and
use Print → Save as PDF to file it).

A case file costs about 7.6¢ in model usage (average of 33: 11.3k input and 5.3k output
tokens at Claude Sonnet 5.5 list prices); each run prints its spend.

Two automatic checks run on every draft (`cases/numbers.py`):
- **Number check** — every number in the text must appear in the bundle (rounding and
  0–1 shares written as percentages are allowed). Invented figures are sent back to the
  model once; if they persist the case file is flagged "needs attention".
- **Language check** — no accusatory terms ("insider", "guilty", "fraud"…) and no full wallet addresses.

## How an alert is scored

A *candidate position* is an account's buying of the eventual winner, worth at least
$1,000, done **before the outcome became public**. "Became public" is estimated from the
market itself: the moment after which the winner never again traded below 0.95. This
is often hours or days before the official resolution, and it stops us flagging people
who simply reacted to the news.

Score (0–100) = sum of points from each signal; every point is stored against its signal.

| Signal | Max pts | What it measures |
|---|---|---|
| Pre-resolution timing | 19 | How much *later than the market's own money* the position was bought, at a low price. A market that trades almost entirely in its final hours (sports, 5-minute crypto) gives no timing signal |
| Low-probability conviction | 19 | Size of bets below 0.30 implied probability, relative to market volume (log(1/price) "surprise") |
| Fresh / single-purpose account | 15 | New account, few markets ever traded, most of its money in this market |
| Size anomaly | 11 | Position size vs. this market (robust z-score) and vs. the account's own history |
| Category prior | 10 | How likely a small group knows the answer early (personnel, corporate, government ≫ sports, prices) |
| Coordinated entry | 11 | Other low-history accounts buying the same long shot within ±24 h, compared against the same flow on the losing side; shared funding where scanned |
| Price leadership | 9 | Did the market move toward the winner *after* the account bought, before the outcome was public? Works at any entry price |
| Long-shot track record | 6 | Long shots won in other markets vs. the number the prices paid implied (Σ price) |
| Hedge adjustment | −20 | Deducted if the same account also bought the losing side |
| Exit adjustment | −30 | Deducted in proportion to the shares sold back *before* the outcome became public |

**Wallet funding** (`ingest/funding.py`, optional). Polymarket accounts are proxy wallets
on Polygon. For flagged wallets and the low-history wallets that entered alongside them,
we scan USDC and pUSD (Polymarket USD, the venue's collateral) transfers *into* the wallet
around when it started trading. pUSD minted on deposit comes from the zero address and links nothing. Two wallets funded
by the same personal address, or one funding the other, count as a funding link; a
coordinated-entry score of at least 0.70 follows from one linked peer, 1.0 from two.
Transfers under $50 are ignored: address-poisoning spam sends zero or tiny amounts to
many wallets and would otherwise look like a shared funder. Senders that are contracts or
fund 8+ scanned wallets are treated as infrastructure
(bridges, exchanges, on-ramps) and link nothing. Public RPC nodes only serve recent
history without a key; older wallets are marked `archive_unavailable` (point
`POLYGON_RPC_URL` at an archive node to cover them).

**News timestamps** (`ingest/news.py`, optional, uses the Anthropic key). For each alerted
market, Claude writes a search query, GDELT (a free index of online news) returns articles
from 72 h before the first flagged buy to 6 h after the outcome became public, and Claude marks
headlines that report or clearly signal the winning outcome. The bundle then says whether
relevant news was published *before the account's first buy* — the strongest innocent
explanation — and the starting risk rating drops one level if it was. GDELT misses wires,
social media and official releases and records when it *saw* an article, so "no relevant
article" is weak evidence; case files say so. When GDELT is unreachable, or the window is older
than its ~3 months, the search falls back to **Google News** (RSS). Its dates are often day-precise
and can be a day early (the 2025 Nobel announcement appears in an article dated the day before), so
a day-precise item is timed at the end of the *following* day: it is listed, marked date-only, and
never counts as preceding a buy.

`scoring.run` also rolls alerts up **by event** (related markets on the same story, e.g.
several "…by date?" variants) and **by wallet** (accounts flagged in several markets),
in the `alert_events` and `alert_wallets` tables.

Each evidence bundle carries a **starting risk rating** from fixed rules (Low if most of
the position was sold before public, if no behavioural signal is at least supporting, or
if the score is under 50; High at 65+ with three strong signals including a behavioural
one; Medium otherwise). The case writer keeps it unless it names a specific fact in the
bundle that justifies moving one level.

Alerts are **anomalous activity warranting review**, not findings of wrongdoing.

## Backtest

There are no labelled insider cases in public data, so `eval/backtest.py` plants
positions whose nature is known into an in-memory copy of the real data (the database
on disk is untouched) and re-scores everything:

- **informed**: a wallet that joined the day before buys the eventual winner 2–36 h before it became public, at the prevailing price (< 0.60), and holds
- **early**: same wallet and size, bought 10–30 days before public (a long-held view)
- **flipper**: an informed-style buy sold back before the outcome became public

Results over all 200 markets, seeds 1–3 (≈30 planted positions per kind and seed),
for categories where non-public information is plausible, with the current eight signals:

| Planted | Top 1% of ~7,000 positions | At/above review threshold | Median score |
|---|---|---|---|
| informed | 82–84% | 42–60% | 48–51 |
| early | 22–39% | 7–11% | 32–34 |
| flipper | 4–14% | 0–3% | 19–21 |

Adding price leadership and track record (`uv run python -m eval.backtest --compare`) raised
informed trades in the top 1% from 80–82% to 82–90% while pushing long-held "early" trades at
the threshold down from 15–32% to 4–14%: better on both sides, so they were adopted.

Informed trades in sports and 5-minute crypto markets are **not** caught (0% at threshold):
those markets trade almost entirely in their final hours, so timing cannot tell an
informed buyer from the crowd. That is a deliberate trade-off, not a bug. Detection
falls off sharply above an entry price of 0.30 (0% caught at 0.45–0.60). Tapering the
low-price cutoff to 0.60 lifted the at-threshold rate but cut the top-1% rate (81% → 74%)
by also lifting real positions, so it was not adopted.

## Validation on publicly reported cases

Planted trades only show the engine finds what we told it to look for. `eval/known_cases.py`
runs the **unchanged engine** on markets that were the subject of public reports of suspected
trading on non-public information, placed *inside* the ordinary universe so case positions
must out-rank everyday ones. The engine sees trades only — no names, times or wallets.

    uv run python -m eval.known_cases     # ≈15 min first time; report in data/known_cases/report.md

| Case (public reports) | Markets | Event rank (of 128 stories) | Best position rank (of 10,908) | Alerts at threshold |
|---|---|---|---|---|
| US strikes on Iran, February 2026 — **out of sample** | 34 | **1** | 1 | 167 |
| Maduro removed from power, January 2026 — "Maduro out by January 31" | 1 | **2** | 5 | 4 |
| US–Iran ceasefire, April 2026 — **out of sample** | 3 | **4** | 35 | 19 |
| 2025 Nobel Peace Prize — winner's price jumped from 3¢ to 75¢ ten hours before the announcement | 1 | **5** | 61 | 3 |
| Google Year in Search 2025 — ten "most searched" list events, all markets | 487 | **7** | 134 | 1 |

*Out of sample* means added after every engine change, so the engine could not have been tuned to it.

**Why events, not positions.** A big geopolitical event draws a wave of new accounts buying in its
final hours: the February strikes produced 167 alerts at threshold, and in a raw position ranking
they bury every other story (24 of the top 25 positions come from case markets). Some of that wave
may be informed and much of it is probably rumour-chasing; without news data for February the
engine cannot tell. Reviewers triage stories, so the review queue groups alerts by event (at
most 3 shown per event, with a count of the rest), and the event rank is the headline measure.
The reports are allegations and the accounts are pseudonymous: this is evidence about the
engine, not about any person.

**Match with a CFTC complaint.** The CFTC's first insider-trading complaint on event contracts
(SDNY 26-cv-3369, April 2026) alleges the purchase of "more than 436,000 Yes shares" of
"Maduro out by January 31, 2026?" between December 2025 and January 2026. The engine's top
alert in that market is a position of **436,760 Yes shares**, bought 31 December 2025 –
3 January 2026 for $32,538. Identity cannot be confirmed from on-chain data and we do not try;
market, share count, amount and dates match.

**A prospective flag.** On 24 November 2026, before any of the changes described below and
before we had seen any reporting, the first scoring run put a "Jensen Huang rejoins NVIDIA"
position at #1 of 6,966 and grouped 5 flagged accounts (4 low-history) on that event. Reports
published around then ([Benzinga](https://www.benzinga.com/markets/prediction-markets/26/11/63568662/20000-in-bets-350000-in-profit-suspicious-polymarket-clusters-nailed-jensen-huangs-nvidia-return),
[Cryptopolitan](https://www.cryptopolitan.com/bubblemaps-flags-16-polymarket-accounts-that-won-350k-on-huangs-nvidia-return/))
described clusters of accounts, several funded hours before the news, winning about $350k on
Huang's return. We have not verified those reports beyond the trading itself.

**What the study changed.** The first run missed most of the Nobel case, and the reasons were
engine flaws, now fixed (each checked against the planted-trade backtest, which stayed at parity):

- The leak frenzy made the market's own money "late", so timing scored ~0. A market's lateness
  baseline is now capped at the 90th percentile for markets of its age: a 93-day market doing
  96% of its business in its last 72 hours is itself anomalous.
- The account whose buying moved the price was not credited for it. Price leadership now
  includes *move initiation*: the share of the buying that took the price halfway up the
  market's sharpest pre-public run-up.
- Selling into that run-up at a large gain was penalised as an exit; the waiver applies only to
  move initiators (waiving it for everyone lifted flippers in the backtest).
- Long-shot conviction was measured against total volume, including post-news frenzies that
  dwarf the market beforehand (d4vd: $1.6M before, $6.9M total). It now uses pre-public volume.

**Limits.** Five cases is still small evidence, chosen because they were reported; two were
added after the engine was frozen. Where
the public reports describe many modest wins across sibling markets (Google), the engine is
weaker: no single position stands out, and the largest cheap buyer of d4vd
in our data is a very large, diversified trader whose positions look like market-making.

## Before the news: the cases, replayed

`uv run python -m app.timemachine` (≈3 min) writes `data/known_cases/timemachine.html`, one
self-contained page. Pick a reported case and press play: the winner's price, who was buying it
(flagged accounts against everyone else), each flagged account's first buy, and the moment the live
watch would have raised its alert, using only the trades available then, with a countdown to the
news. When the alert fires, the feed also shows the AI first look drafted blind at that moment
(only the trades up to then, no venue profit figures): Maduro and Nobel *escalate*, the strikes and
the ceasefire *monitor* (`--triage draft` drafts missing notes; they are cached). At the public
moment the page shows what the engine concluded afterwards. A fifth tab shows
an ordinary market for comparison, picked by rule (leak-prone category, no link to any reported
story, the biggest price move before the news, nothing flagged): the price moves there too, and the
watch stays quiet. Each tab has a link of its own (`timemachine.html#maduro-2026`). Hover for details;
space plays and pauses; the arrow keys step. It reads `data/known_cases.duckdb`, so run
`eval.known_cases` first.

## Market screen: venues without account ids

Some venues publish trades without accounts (Kalshi's public feed), and some operators can share
only anonymised trades. `scoring/market_screen.py` ranks resolved markets using each trade's time,
outcome, side, price and size only: cheap buying of the eventual winner in the 48 hours before the
public moment, the winner's price drift, the trading surge, and how one-sided the buying was, plus
the same 10% category prior as the position engine.

On the known-cases universe, with **no account ids at all**, the reported stories rank **#1, #2, #3,
#14 and #19 of 126** (Google, Nobel, strikes, Maduro, ceasefire; `uv run python -m eval.screen_cases`).
The full engine, which sees accounts, ranks them 1, 2, 4, 5 (tied) and 7: accounts matter most for slow,
drip-fed positions such as Maduro's. Disclosures: the feature weights were set before the evaluation.
Two rules came after looking at results. The category prior was added because without it sports and
tweet-count markets crowd the top (ranks then 1, 7, 12, 22, 23). A story's public moment became the
earliest certainty among sibling markets with the same result, after a Kalshi run showed post-news
trading in slower siblings being counted. That second rule *lowered* the ceasefire case from #2 to #19,
because its earlier rank had counted trading after the 7 April announcement. A 'No' that became certain
within 72 hours of its deadline gets no price-drift credit (time ran out in public view).

Does the position engine have the same sibling problem? Checked: on both the 200-market and the
known-cases databases, **0** alerts at the review threshold have a first buy after their story's
sibling-level public moment (2 and 5 at score ≥ 40), so the engine keeps per-market timing for now.
The dashboard shows the screen's top 10 under the event roll-ups.

`uv run python -m ingest.kalshi --markets 200` loads recently settled Kalshi markets (Kalshi's public
feed appears to keep about two months of trades), and
`GRIB_DB_PATH=data/kalshi.duckdb uv run python -m scoring.market_screen --rebuild` screens them
(`data/kalshi/market_screen.md`); `GRIB_DB_PATH=data/kalshi.duckdb uv run python -m app.screen_pages` writes
one page per top market (`data/kalshi/screen/`): the winner's price path around the public moment
and what drove the score. A screened market is worth a closer look, not a finding.

**Do live alerts beat the market's odds? Not established.** `uv run python -m eval.watch_volume`
(with `--exclude-db` for out-of-sample runs) replays the live watch on every ordinary market of a
database and asks whether alerted sides win more often than the prices paid implied. Counting one
alert per market (alerts on one market are not independent), with exact Poisson-binomial p-values:

| Sample | Markets alerted | Won | Implied | p (one-sided) |
|---|---:|---:|---:|---:|
| First out-of-sample set (402 markets, resolved Sep–Nov 2026) | 18 | 11 | 7.0 | 0.024 |
| **Pre-registered replication** (573 older markets, Jun–Sep 2026; `docs/preregistration.md`) | 54 | 20 | 19.2 | **0.46** |
| Both, pooled (exploratory) | 72 | 31 | 26.2 | 0.12 |

The first result did not replicate. Long-shot entries, the pattern of the reported cases, won 2 of 24
times against 3.0 implied across both sets. So the evidence for the live watch is what it does on the
reported cases (flagged hours to days ahead) and its manageable alert volume (3–8 per 1,000
market-days), not a general edge over the price. That fits the product's purpose: alerts are leads,
and a win, or a loss, says nothing about any individual account. The account-free watch below shows
no edge either.

## Account-free live watch (Kalshi)

`watch/flow.py` raises a `flow_surge` alert from anonymous trades: a long shot (≤ 0.35) rises at least
0.15 in 6 hours, with at least 70% of all buying and $5,000 of it, while the market trades at 5× its
usual rate. `uv run python -m watch.kalshi_live` runs it on the ~100 most active open Kalshi markets
(sports, crypto and financial prices excluded); `--every 3` keeps it running, and new alerts go to
`data/kalshi_live/watch.md` and the team channel. First live run: 100 markets, 59,035 trades, 1 alert.

The thresholds were fixed before the first evaluation (`uv run python -m eval.flow_replay`). Results:

| Test | Result |
|---|---|
| Reported cases, accounts ignored | Fired on the winner before the news for the strikes (0.9 h), the ceasefire (20.6 h) and Google (2.3 h); no alerts on losing outcomes. **Missed Maduro and Nobel.** |
| Why Maduro was missed | The informed buying was drip-fed over three days, with no surge. The account-level watch catches it (16.6 h ahead). |
| Why Nobel was missed | Polymarket stores both counterparties of each fill, which caps "share of buying" near 50–65% even in the 3¢→75¢ run-up (trading at 840× its usual rate). Kalshi's rows are taker buys only, so there the measure means what it says. |
| Ordinary markets | 2.4–2.5 alerts per 1,000 market-days (Polymarket 200 and 600 markets, Kalshi 200 settled markets). |
| Do alerted outcomes win more than their price said? | **No.** Polymarket 600 markets: 14 wins vs 16.2 implied (z −1.3); Kalshi: 5 vs 4.9. |

So the account-free watch is a **timeliness tool**: it tells a venue without account data, within hours,
that money is moving hard and one-sidedly into a long shot, which is when a hold can still matter. It
does not by itself separate informed money from news. That needs accounts, which is the case for
venues sharing them with the full engine.

## Category index

`uv run python -m eval.category_index` re-scores every position **without** its category points and
compares categories on trading behaviour alone (writes `data/category_index.md`). It checks the
hand-set category prior against the data and is the seed of a public integrity index. On the
200-market sample, elections and sports show the highest behaviour-only scores (rank correlation
with the prior +0.25). That is the expected pattern rather than a contradiction: vote counts and live
play make late, confident buying normal there, which is exactly what a low prior damps.

## Live watch: alerts before payout

The engine above reviews markets after they resolve. `watch/live.py` runs on markets that are
**still open**, using only trades up to the moment of the scan, so a venue can hold a withdrawal
before it is paid out. Two patterns, both visible without knowing the winner:

- **Long-shot entry**: a fresh or low-history account's *cumulative* buying of one outcome at
  ≤ 20¢ reaches $10,000 or 5% of the market's buying in the last 7 days (cumulative, so
  drip-buying in small chunks does not slip under the bar).
- **Run-up initiator**: an outcome jumps by ≥ 20 points from ≤ 50¢ within 2 hours, and one
  account did ≥ 40% of the buying that took it halfway up.

Replayed over history with only the data available at each moment (`uv run python -m watch.live --replay`):

| | First alert before the news | Alerts on the eventual winner / on other outcomes |
|---|---|---|
| Maduro, January 2026 | **16.6 hours**: the engine's #1 account (5 days old), once its drip-bought total passed $10k at 7¢ | 2 / 0 |
| Nobel Peace Prize 2025 | **10 hours**: the account that started the run-up, on its first $2,675 at 8¢ | 1 / 0 |
| US–Iran ceasefire, April 2026 (**out of sample**) — "by April 7" / "by April 15" | **248 h** / **158 h**: accounts 0 and 1 days old buying at 11–15¢ | 6 / 2 and 4 / 0 |
| US strikes on Iran, February 2026 (**out of sample**) — "by February 28" | **336 h** (the start of the 14-day replay window): an account 0 days old at 12¢ | 21 / 0; none of the losing "next strike on [date]" markets fired |
| 200 ordinary markets, last 14 days each | 6.1 alerts per 1,000 market-days | alerted long shots won 20% vs 16% implied; run-up initiators 43% vs 30%; together 5 of 17 vs 3.6 implied (z +0.8: not significant) |
| 402 further markets, never used in any design decision (out of sample) | 3.4 per 1,000 market-days (watch scope) | first alert per market: 11 of 18 won vs 7.0 implied (exact p = 0.024) |
| **573 older markets, pre-registered replication** | 8.1 per 1,000 market-days | **20 of 54 vs 19.2 implied (p = 0.46): did not replicate**; long-shot entries 0 of 18 vs 2.1 |

The ordinary-market samples are small (11 and 7 alerts): the run-up edge is early evidence of
signal, the long-shot edge is within noise. An account counts as fresh or low-history from the
venue's own record (join date, lifetime markets) when known; only accounts the venue knows
nothing about fall back to what our sample has seen.

**Running it live:** `uv run python -m watch.run_live` loads the ~150 most active open markets
(sports and crypto excluded), their last 7 days of trades, enriches candidate accounts, scans,
and writes new alerts to `data/live/watch.md` (each alert is recorded once, with the time it was
first raised). Keep it running with `--every 3` (a scan every 3 hours; a failed scan is logged and
retried at the next interval), or schedule single runs.

**Notifications.** Set `GRIB_ALERT_WEBHOOK_URL` to a Slack-compatible incoming webhook and new
live-watch alerts, and pipeline runs with new alerts, are posted to the channel (truncated wallets,
as in the digests). Unset, nothing is sent.

**Alert pages.** Each live alert gets a page (`data/live/alerts/`, rebuilt after every scan, linked
from the dashboard and the digest): the outcome's price over the trades held with the account's buys
on it, the account's share of the market's buying, what the venue says about the account (join date,
lifetime markets, volume, profit) and other alerts on the same market. A long lifetime record usually
points to an active trader, which lets a reviewer close the alert quickly; a days-old account with one
market is the case to escalate before payout. `uv run python -m app.live_pages` rebuilds them.

**AI first look.** After each scan, Claude drafts a short note on every new or changed alert
(`watch/triage.py`): what happened, the strongest ordinary explanation, and a suggested action
(close, monitor or escalate) with the reason. It writes from a fact sheet of our own data only. Every
number must appear in that sheet and the language must be neutral (the case-file checks), with one
retry. Notes are cached by the facts and the prompt, so unchanged alerts cost nothing: about 1¢ per
note. The note is shown on the alert page, in the alert list, on the dashboard and in the digest
(escalations first). It is a suggestion for a human reviewer, never a decision.

How the guidance was checked (`eval/triage_eval.py`): the 33 historical alerts of the 600-market
sample were triaged blind, each with only the trades up to its moment. The first version treated a
long record as reason to close, and the alerts it closed won 10 of 13 times against 5.0 implied, the
opposite of what closing should mean; the guidance now says a long record alone does not clear an
alert. That evaluation also exposed a hindsight leak: the venue's lifetime profit and volume are as
of today and can include the very win being judged, so evaluations now remove them. Without them, on
the same 33 alerts, the revised guidance escalates 5 (3 won vs 1.7 implied), monitors 27 (12 vs 8.9)
and closes 1. Its triage value is therefore **not yet demonstrated**; what it does provide is a
checked, readable first look on every alert within seconds. The suggestion can also differ between
drafts on borderline cases. On the pre-registered fresh sample (`docs/preregistration.md`) it again
did not separate outcomes (escalate 2: 1 won vs 1.1 implied; monitor 59; close 4), while all 65 notes
passed the checks: reliable drafting, unproven triage. `uv run python -m watch.triage` drafts
`uv run python -m watch.triage` drafts
notes on demand.

**Scorecard: checking live alerts prospectively.** Every live alert is a prediction made before
the outcome was known. After each scan, `watch/follow_up.py` looks up the markets of earlier alerts
and, once they resolve, records whether the flagged side won (`data/live/scorecard.md`, and a
*Result* column on the dashboard). One win says nothing about an account; across many alerts the
question is whether flagged positions win more often than the prices paid implied (a 0.10 buy is
priced to win 10% of the time). The scorecard reports wins against implied wins with a z-score,
and says when there are too few to conclude. Run it on its own with `uv run python -m watch.follow_up`.

## How it is organised

| Folder    | What it does |
|-----------|--------------|
| `ingest/` | Polite API clients (disk cache, rate limit, retries) and the loader |
| `schema/` | The venue-agnostic canonical schema and the Polymarket → canonical mapper |
| `signals/` | One module per detection signal; each returns a score, a reason and evidence |
| `scoring/` | Feature tables (from canonical tables only) and the weighted score |
| `eval/`   | Backtest (planted trades), the known-cases study (publicly reported episodes), watch replay |
| `watch/`  | Live watch: alerts on open markets before the winner is known |
| `app/`    | Static HTML dashboard, event dossiers (`app/dossier.py`) and the analyst review loop (`app/review.py`) |
| `pipeline.py` | One-command incremental run, run log and digest |
| `examples/operator/` | Synthetic sportsbook export for the operator path |
| `docs/` | Demo walkthrough, investor brief, market research notes |
| `cases/` | Evidence bundles, the case-file prompt, the LLM writer and the number/language checks |

**Canonical schema** — every signal reads only these tables, which is how an operator's CSV
(`ingest/csv_loader.py`) runs through the same engine:

- `accounts(account_id, venue, first_seen_at, metadata_json)`
- `markets(market_id, venue, question, category, opened_at, closes_at, resolved_at, winning_outcome, metadata_json)`
- `trades(trade_id, venue, market_id, account_id, ts, side, outcome, price, size, notional)`

## Data sources

- Polymarket **Data API v2** (`https://data-api.polymarket.com/v2`, public): trades, wallet stats, price history.
- Polymarket **Gamma API** (`https://gamma-api.polymarket.com`, public): market questions, tags, close time, final outcome.

- Polygon **public RPC** (`polygon-bor-rpc.publicnode.com`, or `POLYGON_RPC_URL`): USDC / pUSD transfers into wallets (funding).
- **GDELT DOC 2.0** (`api.gdeltproject.org`, public): news articles around alerted markets (covers roughly the last three months).
- **Google News RSS** (`news.google.com/rss/search`, public): fallback news search, older windows too; dates often day-precise.
- **Kalshi trade API** (`api.elections.kalshi.com`, public): settled markets and their trades, without accounts.
- **Anthropic API**: case files, and news queries / headline relevance.

All responses are cached under `data/cache/` and requests are rate-limited (≤4 req/s per host;
GDELT 1 per 5 s).
