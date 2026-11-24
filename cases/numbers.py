"""Number check: every number in an AI-written case file must appear in its evidence bundle.

A number in the text "matches" if some number in the bundle, rounded to the same
number of decimals, equals it — directly, or ×100 (a 0–1 share written as a percent).
Numbers are collected from every bundle value, including numbers inside strings
(reasons, timestamps), and dates/times are therefore allowed if the bundle has them.

Numbers with a unit suffix are checked too: "$45k" / "$1.2M" / "2bn" are scaled and must
match a bundle number rounded at that scale; "3x", "72h", "2nd" are checked as plain numbers.
Ordered-list markers ("1.", "2.") at the start of a line are not counted as numbers.
This is a guard against invented figures, not a full fact-checker: a correct number
used in the wrong place would still pass. The case file is still reviewed by a human.
"""

from __future__ import annotations

import re

_NUM = re.compile(r"(?<![\w.…])[-−]?\$?(\d[\d,]*(?:\.\d+)?)(%?)"
                  r"(k|K|M|bn|x|×|h|hrs?|min|mins|d|days?|st|nd|rd|th)?(?![\w])")
_SCALE = {"k": 1e3, "K": 1e3, "M": 1e6, "bn": 1e9}
_HEX = re.compile(r"0x[0-9a-fA-F…]+")
_LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)


def extract_numbers(text: str) -> list[tuple[str, float, int]]:
    """Return (raw token, value, decimals) for each number in free text (value in plain units)."""
    return [(raw, val, dec) for raw, val, dec, _ in extract_scaled(text)]


def extract_scaled(text: str) -> list[tuple[str, float, int, float]]:
    """Return (raw token, value as written, decimals, scale) — e.g. '$1.2M' -> (…, 1.2, 1, 1e6)."""
    text = _HEX.sub(" ", text)
    text = _LIST_MARKER.sub(" ", text)
    out = []
    for m in _NUM.finditer(text):
        raw = m.group(1).replace(",", "")
        if not raw or raw.count(".") > 1:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        dec = len(raw.split(".")[1]) if "." in raw else 0
        scale = _SCALE.get(m.group(3) or "", 1.0)
        out.append((m.group(0), val, dec, scale))
    return out


def bundle_numbers(obj) -> set[float]:
    vals: set[float] = set()

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            vals.add(abs(float(x)))
        elif isinstance(x, str):
            for _, v, _, sc in extract_scaled(x):
                vals.add(v * sc)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k)
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return vals


def _matches(val: float, dec: int, pool: set[float], scale: float = 1.0) -> bool:
    tol = 0.5 * 10 ** -dec + 1e-9
    for v in pool:
        v = v / scale
        if abs(round(v, dec) - val) < tol or (scale == 1.0 and abs(round(v * 100, dec) - val) < tol):
            return True
    return False


def check_numbers(text: str, bundle: dict) -> list[str]:
    """Return the tokens in `text` that do not trace to any number in `bundle`."""
    pool = bundle_numbers(bundle)
    return [raw for raw, val, dec, sc in extract_scaled(text) if not _matches(abs(val), dec, pool, sc)]


BANNED_TERMS = ["insider", "guilty", "fraud", "criminal", "illegal", "crime", "cheat", "manipulat"]
_FULL_WALLET = re.compile(r"0x[0-9a-fA-F]{40}")


def check_language(text: str) -> list[str]:
    """Guardrails: no accusatory language and no full wallet addresses."""
    low = text.lower()
    problems = [f"banned term '{t}'" for t in BANNED_TERMS if t in low]
    if _FULL_WALLET.search(text):
        problems.append("full wallet address present")
    return problems
