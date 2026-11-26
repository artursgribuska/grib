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
