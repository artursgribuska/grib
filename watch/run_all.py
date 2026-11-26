"""Run every live watch on a schedule: Polymarket (accounts) and Kalshi (account-free).

    uv run python -m watch.run_all                # one cycle
    uv run python -m watch.run_all --every 3      # a cycle every 3 hours (the container's default)

Each watch runs in turn; a failure in one (an API outage, say) is logged and does not stop the
other or the schedule. Each sends its own new alerts to GRIB_ALERT_WEBHOOK_URL if set.
"""

from __future__ import annotations

import argparse
import json
import logging
import time

log = logging.getLogger("watch")


def watches() -> dict:
    from watch import kalshi_live, run_live
    return {"polymarket": lambda: run_live.run(150), "kalshi": lambda: kalshi_live.run(100)}


def cycle(ws: dict | None = None) -> dict:
    out = {}
    for name, fn in (ws or watches()).items():
        try:
            out[name] = fn()
        except Exception as e:                      # one venue failing must not stop the others
            log.exception("%s watch failed", name)
            out[name] = {"error": str(e)[:200]}
    return out


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--every", type=float, default=None, help="repeat every N hours (runs until stopped)")
    a = ap.parse_args(argv)
    while True:
        log.info("cycle: %s", json.dumps(cycle(), default=str))
        if not a.every:
            return 0
        time.sleep(a.every * 3600)


if __name__ == "__main__":
    raise SystemExit(main())
