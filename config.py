"""Project-wide settings. Change numbers here, not deep inside modules."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
# GRIB_DB_PATH points the whole pipeline at another database (e.g. an operator's own data)
DB_PATH = Path(os.environ["GRIB_DB_PATH"]) if os.environ.get("GRIB_DB_PATH") else DATA_DIR / "integrity.duckdb"
# Generated outputs (case files, dashboard, backtest) live next to their database so two
# datasets never overwrite each other: data/ for the default database, data/<db name>/ otherwise.
OUTPUT_DIR = DATA_DIR if DB_PATH == DATA_DIR / "integrity.duckdb" else DATA_DIR / DB_PATH.stem
DB_PATH.parent.mkdir(parents=True, exist_ok=True)   # a fresh checkout has no data/ folder yet

DATA_API = "https://data-api.polymarket.com/v2"
GAMMA_API = "https://gamma-api.polymarket.com"

# Politeness: maximum requests per second per host, and retry policy.
REQUESTS_PER_SECOND = {"data-api.polymarket.com": 4.0, "gamma-api.polymarket.com": 3.0,
                       "api.gdeltproject.org": 0.2,   # GDELT asks for at most one request per 5 s
                       "api.elections.kalshi.com": 3.0}
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
MAX_RETRIES = 6
HTTP_TIMEOUT_S = 30.0
USER_AGENT = "integrity-analyst-demo/0.1 (research; public data)"

# Market selection for the demo dataset (milestone 1).
SELECTION = {
    "target_markets": 200,
    "lookback_days": 60,          # markets resolved within this many days of today
    "min_volume_usd": 5_000,      # skip near-empty markets (nothing to analyse)
    "max_volume_usd": 2_500_000,  # skip mega-markets to keep the demo fast to build
    "max_markets_per_event": 4,   # stop one 100-candidate event dominating the sample
    "max_trade_rows_per_market": 80_000,  # safety cap; markets that hit it are flagged
}

# Tags we crawl on Gamma to get a mixed sample of categories.
CRAWL_TAGS = [
    "politics", "geopolitics", "business", "tech", "economy", "culture",
    "crypto", "sports", "science", "world", "elections", "awards",
]

# Per-category caps so sports and crypto price markets don't swamp the sample.
CATEGORY_QUOTAS = {
    "sports": 40,
    "crypto_price": 25,
    "price_threshold": 15,
    "public_metric": 20,
    "elections": 30,
}
