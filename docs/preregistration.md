# Pre-registration: second independent sample

*Written and committed on 26 November 2026, before any result from this sample was seen.
Code frozen at commit `ba1a0fa`; the analysis below runs that code unchanged.*

## Sample

About 600 resolved Polymarket markets that **closed between 60 and 150 days ago**, excluding
every market in the earlier samples (`watch_eval`, `known_cases`, `integrity`). Built with:

    GRIB_DB_PATH=data/watch_eval2.duckdb uv run python -m ingest.run --markets 600 --refresh-listing \
        --resolved-between 60 150 --exclude-db data/watch_eval.duckdb \
        --exclude-db data/known_cases.duckdb --exclude-db data/integrity.duckdb

followed by `ingest.enrich` and `scoring.run`.

## Hypotheses and tests

**H1. Live alerts carry information beyond the price.** Replay the live watch
(`eval/watch_volume.py`, unchanged thresholds) on the markets it covers (no sports, no
crypto prices). Primary test: counting only the first alert on each market, the alerted side
wins more often than the prices paid implied, exact one-sided Poisson-binomial p < 0.05.
Reported whatever the result, with the all-alerts version alongside.

**H2. The revised AI triage separates alerts.** Run `eval/triage_eval.py` (prompt as frozen
at this commit; no venue profit or volume figures) on the in-scope alerts. Test: alerts it
marks *escalate* beat their implied odds by more, per alert, than alerts it marks *monitor*
or *close*. With the expected handful of escalations this is descriptive; it is reported
whatever it shows and is not called significant unless the escalate group alone has exact
one-sided p < 0.05.

**H3. Alert volume.** The watch raises fewer than 10 alerts per 1,000 market-days in scope.

**No other analyses** of this sample will be reported as confirmatory. Anything else is labelled
exploratory.

## Known caveats

Venue account records (join date, lifetime market count) are as of today, not as of each
alert; older markets make this hindsight larger, mostly in the conservative direction (accounts
that were fresh then may have traded more since, so fewer alerts fire).

## Results (added after the analysis; nothing above was changed)

Sample: 600 markets resolved 29 June – 27 September 2026, none in any earlier sample; 573 in the
watch's scope. The code was unchanged from the commit above. One operational fix was needed: the
output folder `data/watch_eval2/` did not exist and the script does not create it, so it was
created by hand and the script rerun (fixed in code afterwards).

| | Pre-registered test | Result | Verdict |
|---|---|---|---|
| **H1** | First alert per market, exact one-sided p < 0.05 | 54 markets: the alerted side won **20** vs **19.2** implied, p = **0.46**. All 65 alerts: 26 vs 23.3, p = 0.26 | **Not supported** |
| **H2** | *Escalate* beats its odds by more than *monitor*/*close* | Escalate 2 (1 won vs 1.1 implied), monitor 59 (25 vs 21.7), close 4 (0 vs 0.4) | **Not supported** |
| **H3** | Fewer than 10 alerts per 1,000 market-days | 8.1 | Supported |

Exploratory (not pre-registered): long-shot entries won **0 of 18** against 2.1 implied. Pooling
both out-of-sample sets, one alert per market: 72 markets, 31 won vs 26.2 implied, exact p = 0.12;
long-shot entries 2 of 24 vs 3.0. All 65 triage notes passed the number and language checks.

**What this means.** The earlier out-of-sample result (11 of 18, p = 0.024) did not replicate. The
claim that live alerts, taken together, beat the market's odds is **not established**. What stands:
the watch flags the publicly reported cases hours to days before the news, it raises a manageable
number of alerts, and every alert arrives with checked context. Whether flagged positions are
informed has to be judged case by case, which is the reviewer's job.
