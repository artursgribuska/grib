# Integrity Analyst: investor brief (draft)

*Draft, 26 November 2026. Market facts are from the sources in `docs/market-notes.md`.
Product facts and numbers are reproducible from this repository. Anything marked
**[decide]** is an open choice for the founders, not a claim.*

## The problem

Prediction markets have turned into a large, regulated business, and insider trading is
now the main risk they face.

- **Volume.** Kalshi and Polymarket together traded about $50B in July 2026. Across
  eight exchanges the total reached $120B in October 2026.
- **Regulatory duty.** A CFTC advisory in February 2026 says exchanges have an
  *independent duty* to conduct surveillance and enforce rules against prohibited
  practices. The CFTC filed its first insider-trading case on event contracts in April
  2026, on Maduro-related contracts.
- **The work is piling up.** Kalshi referred 32 suspected cases in a single quarter and
  calls insider monitoring its "No. 1 priority". Polymarket flagged more than 90 traders
  in 2026. Every one of those cases needs an analyst to write it up.
- **It is hard.** Accounts are pseudonymous, so regulators cannot check trades against
  lists of insiders the way they do in equities. The evidence has to come from the trading
  itself.

## What we built

An AI compliance analyst. It reads a venue's trades, finds the positions that look like
trading on non-public information, and writes the first draft of the case file. A human
reviewer decides.

It works at two points in time:

- **Live, before payout.** The watch runs on open markets. It flags fresh accounts building
  heavy long-shot positions, and accounts that start sharp price run-ups. A venue can then
  hold a withdrawal before it is paid out.
- **After resolution.** A full review of each market, with the scored evidence and the case
  file.

1. **Detection: eight signals, every point explained.**
   - *Behaviour:* timing relative to the market's own rhythm, conviction on long shots,
     price leadership (including who started a pre-public price run-up), coordinated entry
     by fresh accounts, and a long-shot track record across markets.
   - *Context:* account freshness, position size, and how leak-prone the market category is.
   - *Deductions:* hedges, and positions sold before the news broke.
2. **Evidence.**
   - Account funding on-chain: shared funders and direct transfers, with bridges,
     exchanges and spam filtered out.
   - News timestamps from GDELT, to test the strongest innocent explanation ("was it
     already in the news?").
3. **AI case files.**
   - Each one includes a summary, timeline, evidence, at least four innocent explanations,
     a risk rating with confidence, next steps and open questions.
   - Every number in a case file is checked against the evidence. Language is checked for
     accusation. Wallets are always truncated.
   - The risk rating starts from fixed rules. The model must name a fact to depart from
     them.
4. **Workflow.**
   - A one-command daily pipeline that only rewrites case files whose evidence changed.
   - A dashboard with review decisions and an append-only audit trail.
   - Per-signal precision computed from reviewer decisions.
   - A run digest for email or chat.
5. **Works beyond Polymarket.** A loader takes an operator's own bet export (odds and
   stake) into the same engine. Where accounts are not available at all, a market-level
   screen ranks markets from anonymous trades (Kalshi loader included).
6. **Keeps score of itself.** Every live alert is followed up when its market resolves: did
   the flagged side win more often than the odds paid implied? This builds an
   out-of-sample track record automatically.

## Proof

| Evidence | Result |
|---|---|
| **Match with the CFTC's first insider case on event contracts** (SDNY 26-cv-3369) | The complaint alleges "more than 436,000 Yes shares" of "Maduro out by January 31, 2026?" bought in Dec 2025–Jan 2026. The engine's top alert in that market, found blind, is 436,760 Yes shares, bought 31 Dec–3 Jan for $32,538. Identity is not confirmed; the trading matches |
| **Prospective flag** | The first run on 24 Nov 2026, before we had seen any reporting, put a Jensen Huang/NVIDIA position at #1 of 6,966. The same days, reports described suspicious account clusters winning about $350k on Huang's return |
| **Live watch, replayed using only data available at each moment** | Maduro: the account matching the CFTC complaint was flagged **16.6 hours before the news** (5 days old, drip-buying at 7¢). Nobel: the account that started the run-up was flagged **10 hours before the announcement**. Iran ceasefire and US strikes on Iran (both out of sample): first alerts **6–14 days before** the news, on accounts 0–1 days old. Alerts on the losing side: none for Maduro, Nobel or the strikes, 2 of 12 for the ceasefire. Ordinary markets: 6.1 alerts per 1,000 market-days |
| Five publicly reported episodes, engine unchanged and given trades only, ranked as stories among 128 events | US strikes on Iran, Feb 2026 (**out of sample**): **#1**. Maduro, Jan 2026: **#2**. Iran ceasefire, Apr 2026 (**out of sample**): **#4**. Nobel Peace Prize 2025: **#5** (tied). Google Year in Search 2025: **#7**. *Out of sample* = added after every engine change |
| **Without account ids** (for venues whose feeds are anonymous, such as Kalshi's public trades) | A market-level screen that sees only times, sides, prices and sizes ranks the same reported stories **#1, #2, #3, #14 and #19 of 126**. A Kalshi loader feeds it today |
| **Live alerts carry information** (out of sample) | Replayed on 402 markets never used in any design decision, the flagged side won **11 of 19** times against **7.6** implied by the prices paid (one-sided p = 0.035). Across all 535 in-scope markets: 16 of 33 vs 10.7 (p = 0.013) |
| Planted trades of known kinds (3 seeds, 200 real markets) | 82–84% of informed trades rank in the top 1%. Accounts that sold before the news reach the threshold 0–3% of the time |
| Synthetic sportsbook export | Two day-old accounts backing a 9.0 outsider hours before an announcement rank #1 and #2, and the AI writes up both as High risk |
| Unit cost | An AI case file costs about **7.6¢** in model usage (33 written: 11.3k input and 5.3k output tokens on average, Claude Sonnet 5.5 list prices). 1 of 33 needed a second attempt to fix a number |
| Engineering | 139 automated tests; a clean checkout installs and passes them. `python -m demo` reproduces the Maduro and Nobel results in about 15 minutes. The full pipeline reruns in minutes from cache. The live watch's first run covered 150 open markets and their last 7 days of trades; it runs on a schedule and posts new alerts to a team channel |

**What the validation changed.** On its first run, the engine missed most of the Nobel
case. The study found four real flaws in it, and a later case showed that one event can flood a raw ranking, so the queue now groups alerts by event. Each was fixed and re-checked against the
planted-trade backtest. This is how we intend to keep improving: on real cases, with the
regressions measured.

**Honest limits.** Five public cases (two out of sample) is still anecdote-sized evidence. On 402 ordinary markets never used to design it, the replayed live watch's flagged sides won 11 of 19 times against 7.6 implied by the prices paid (one-sided p = 0.035): modest but significant evidence that the alerts carry information beyond the price. The account-free variant shows no such edge. The live scorecard now collects this evidence prospectively, one resolved market at a time. Sports markets and
5-minute crypto markets are a blind spot by design, because everyone trades late there.
Funding history for older wallets needs a paid archive node. The engine gives scores and
drafts, not findings: a human decides every case.

## Who buys it

**Primary customers.** Kalshi (Solidus Labs) and Polymarket (Chainalysis PolyGraph) already
have vendors. A second tier of venues now has the same surveillance duty and is newer to it:

- DraftKings Predictions
- FanDuel Predicts
- Fanatics Markets
- Crypto.com, Gemini, Novig, Underdog, PrizePicks
- a planned Robinhood exchange

Many of these are sportsbooks, whose data is odds and stakes. That is the format the
operator loader already takes.

**Secondary customers:**
- Compliance and audit firms that serve these venues.
- Regulators and investigative journalists, as a research product built on public data.
  This one needs care, given the pseudonymity issues above.

## How we differ from the incumbents

The incumbents sell detection. We sell the **analyst's output**: a defensible draft case
file with every number checked, innocent explanations weighed, and a rule-based starting
rating, plus validation on real cases. That targets the expensive part of the job, which
is analyst time per case.

To confirm in customer calls: how much of a compliance team's time goes into writing cases
rather than finding them.

## Business model **[decide]**

These are options to test with customers, not claims:

- An annual licence per venue, tiered by monthly volume.
- A pilot priced per case file.
- A public-data "integrity index" offered as marketing and a research product.

## Next 6 months **[decide priorities]**

1. **Design partners.** Two second-tier venues running it on their own data, with
   reviewer decisions feeding the precision statistics.
2. **More real cases.** Extend the known-cases set as enforcement actions become public,
   with each case checked like the ones above.
3. **Data coverage.** An archive node for older wallet funding. Wire, social and official
   release sources alongside GDELT.
4. **Production hardening.** Scheduled live-watch runs every few hours with notifications,
   access control, and data retention aligned with CFTC record-keeping.

## The ask **[decide]**

Amount, use of funds and team to be filled in by the founders.
