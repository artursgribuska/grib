# Evidence ledger

Every test we have run on the engine, in one place, including the ones that did not go our
way. *As of 26 November 2026.* Each row can be reproduced with the command shown; data is
public (Polymarket, Kalshi) and nothing here names or accuses anyone.

## What holds

| Claim | Test | Result | Reproduce |
|---|---|---|---|
| The engine finds publicly reported cases blind | Five reported episodes placed inside 128 ordinary stories; engine unchanged, trades only | Ranked **#1, #2, #4, #5 (tied), #7**; two cases added after every engine change (out of sample) ranked #1 and #4 | `eval.known_cases` |
| Its top Maduro alert matches the CFTC complaint | Compare with SDNY 26-cv-3369 | 436,760 Yes shares, 31 Dec–3 Jan, vs "more than 436,000" alleged | `eval.known_cases` |
| The live watch flags those cases before the news | Replay using only trades available at each moment, every 2 h | Maduro **16.6 h**, Nobel **10.1 h**, ceasefire 6–10 days, strikes 14 days ahead | `watch.live --replay` |
| Alert volume is manageable | Replays on ordinary markets | **3–8 per 1,000 market-days** (200, 600 and 600 markets); about one alert a day on 150 markets | `eval.watch_volume` |
| Informed trades of known shapes rank high | Planted trades in 200 real markets, 3 seeds | 82–84% in the top 1%; accounts that sold before the news reach the threshold 0–3% | `eval.backtest` |
| Works without account ids, on the cases | Market screen, no accounts, same universe | Reported stories **#1, #2, #3, #14, #19 of 126** (two rules added after looking, disclosed) | `eval.screen_cases` |
| AI drafting is reliable | Number and language checks on every draft | Case files: 9–10 of 10 pass per run; triage notes: **164 of 164** passed across all evaluations | `cases.run`, `eval.triage_eval` |
| AI first look reads the cases sensibly | Blind notes at each case's first alert, no hindsight fields | Maduro and Nobel *escalate*; strikes and ceasefire *monitor* | `app.timemachine --triage draft` |

## What does not hold, or is not established

| Claim | Test | Result | Reproduce |
|---|---|---|---|
| Live alerts in general beat the market's odds | First out-of-sample set (402 markets), one alert per market | 11 of 18 won vs 7.0 implied, p = 0.024 | `eval.watch_volume --exclude-db …` |
| | **Pre-registered replication** (573 older markets) | **20 of 54 vs 19.2, p = 0.46: not supported** | `docs/preregistration.md` |
| | Both pooled (exploratory) | 31 of 72 vs 26.2, p = 0.12 | |
| Long-shot entries beat their odds | Both out-of-sample sets | 2 of 24 won vs 3.0 implied | |
| The AI's close / monitor / escalate separates outcomes | Blind triage of 33 and (pre-registered) 65 historical alerts | Not supported on either (the first version's apparent split was partly a hindsight leak, since fixed) | `eval.triage_eval` |
| The account-free watch beats the odds | Replays on Polymarket (200, 600) and Kalshi (200) | No edge (p 0.59–0.94); it is a timeliness tool | `eval.flow_replay` |
| The account-free watch catches every case | Replay on the reported cases | Catches strikes, ceasefire, Google; **misses Maduro and Nobel** | `eval.flow_replay` |

## How to read this

The product's claim is narrow and supported: on the cases that became public, it would have
raised the alarm hours to days before the news, at an alert volume a small team can handle, with
every alert arriving with checked context and an AI first look. The broader claim, that its
alerts in general pick winners, is not supported by our own pre-registered test, and we do not
make it. Winning is not the point of an alert: a reviewer decides whether a position was informed.

## Methods notes

- Out of sample means the markets were never used in any design decision. The pre-registration
  (`docs/preregistration.md`) was committed, with the code frozen, before the sample was loaded.
- P-values are exact one-sided Poisson-binomial tests against the win rate implied by the prices
  paid. Several alerts on one market are not independent, so the primary count uses one per market.
- Venue account records are as of today; evaluations remove lifetime profit, biggest win and
  volume, which can contain the outcome being judged.
