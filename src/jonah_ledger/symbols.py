"""Nasdaq Trader symbol directory (nasdaqlisted + otherlisted). A ticker we cannot resolve here is dropped."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
MAX_AGE_SECONDS = 7 * 86400

_TICKER_RE = re.compile(r"^[A-Z]{1,5}(-[A-Z]{1,2})?$")


class SymbolListUnavailable(RuntimeError):
    pass


def normalize_ticker(raw: str) -> str:
    """'$brk.b' -> 'BRK-B' (the form yfinance uses)."""
    t = raw.strip().upper().lstrip("$").replace(".", "-").replace("/", "-")
    return t


def looks_like_ticker(t: str) -> bool:
    return bool(_TICKER_RE.match(t))


def parse_symbol_files(nasdaq_text: str, other_text: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i, line in enumerate(nasdaq_text.splitlines()):
        parts = line.split("|")
        if i == 0 or line.startswith("File Creation Time") or len(parts) < 8:
            continue
        sym, name, _mc, test, _fs, _lot, etf = parts[:7]
        if test == "Y":
            continue
        out[normalize_ticker(sym)] = {"name": name, "exchange": "NASDAQ", "etf": etf == "Y"}
    for i, line in enumerate(other_text.splitlines()):
        parts = line.split("|")
        if i == 0 or line.startswith("File Creation Time") or len(parts) < 7:
            continue
        act, name, exch, _cqs, etf, _lot, test = parts[:7]
        if test == "Y":
            continue
        out.setdefault(normalize_ticker(act), {"name": name, "exchange": exch, "etf": etf == "Y"})
    return {k: v for k, v in out.items() if looks_like_ticker(k)}


def load_symbols(cache_dir: Path, *, fetch=None, now: float | None = None) -> dict[str, dict]:
    """Return {ticker: info}. Uses a weekly cache; falls back to a stale cache if the network fails."""
    cache = Path(cache_dir) / "symbols.json"
    now = time.time() if now is None else now
    stale = None
    if cache.exists():
        blob = json.loads(cache.read_text())
        stale = blob["symbols"]
        if now - blob["fetched_at"] < MAX_AGE_SECONDS:
            return stale
    try:
        fetch = fetch or (lambda url: requests.get(url, timeout=30).text)
        syms = parse_symbol_files(fetch(NASDAQ_URL), fetch(OTHER_URL))
        if len(syms) < 1000:
            raise SymbolListUnavailable(f"symbol list suspiciously small ({len(syms)})")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched_at": now, "symbols": syms}))
        return syms
    except Exception as exc:  # noqa: BLE001
        if stale:
            return stale
        raise SymbolListUnavailable(str(exc)) from exc
