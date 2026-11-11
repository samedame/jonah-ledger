"""Daily bars (yfinance, cached) and the call-outcome arithmetic.

Conventions (see DECISIONS.md):
- Entry = open of the first session strictly after the video's US/Eastern calendar date.
- Horizon h is marked at the close of the h-th session, counting the entry session as session 1
  (config.ENTRY_DAY_IS_SESSION_1; flip it to count from the next session instead).
- Returns use split+dividend adjusted prices (adj_open at entry, adj_close at exit).
- Net return = direction * gross - cost, cost 10 bps (30 bps if the raw entry open is under $5).
  Excess = net return - direction * SPY return over the same dates (SPY gets no cost).
- Depth below the 252-day high, trailing 60-day return and 60-day beta are *logged only*.
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .config import (
    BENCHMARK, COST_BPS, COST_BPS_LOW_PRICE, ENTRY_DAY_IS_SESSION_1, HORIZONS, LOW_PRICE_THRESHOLD,
)
from .dates import et_date


def clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a raw yfinance/test frame to: open, high, low, close, adj_close + adj_open/adj_high/adj_factor."""
    d = df.copy()
    d.columns = [str(c).strip().lower().replace(" ", "_") for c in d.columns]
    if "adj_close" not in d.columns:
        d["adj_close"] = d["close"]
    idx = pd.DatetimeIndex(d.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    d.index = idx.normalize()
    d = d[~d.index.duplicated(keep="last")].sort_index()
    d = d.dropna(subset=["open", "close"])
    d["adj_factor"] = d["adj_close"] / d["close"]
    d["adj_open"] = d["open"] * d["adj_factor"]
    d["adj_high"] = d["high"] * d["adj_factor"]
    return d[["open", "high", "low", "close", "adj_close", "adj_factor", "adj_open", "adj_high"]]


def _yf_fetch(ticker: str) -> pd.DataFrame | None:
    import yfinance as yf

    df = yf.download(ticker, period="max", auto_adjust=False, progress=False, multi_level_index=False)
    if df is None or df.empty:
        return None
    return clean_frame(df)


class PriceStore:
    def __init__(self, cache_dir: Path, fetcher: Callable[[str], pd.DataFrame | None] | None = None, max_age_hours: float = 6.0):
        self.dir = Path(cache_dir)
        self.fetcher = fetcher or _yf_fetch
        self.max_age = max_age_hours * 3600
        self._mem: dict[str, pd.DataFrame | None] = {}

    def _path(self, t: str) -> Path:
        return self.dir / f"{t}.csv"

    def get(self, ticker: str, *, refresh: bool = False) -> pd.DataFrame | None:
        if ticker in self._mem:
            return self._mem[ticker]
        p = self._path(ticker)
        cached = None
        fresh = False
        if p.exists():
            cached = clean_frame(pd.read_csv(p, index_col=0, parse_dates=True))
            fresh = (time.time() - p.stat().st_mtime) < self.max_age
        if cached is not None and fresh and not refresh:
            self._mem[ticker] = cached
            return cached
        try:
            df = self.fetcher(ticker)
        except Exception:  # noqa: BLE001 - network trouble falls back to stale cache
            df = None
        if df is not None and len(df):
            self.dir.mkdir(parents=True, exist_ok=True)
            df.to_csv(p)
            self._mem[ticker] = df
            return df
        self._mem[ticker] = cached
        return cached


def _first_pos_after(frame: pd.DataFrame, d: date) -> int:
    return int(frame.index.searchsorted(pd.Timestamp(d), side="right"))


def _exit_offset(h: int) -> int:
    return h - 1 if ENTRY_DAY_IS_SESSION_1 else h


def compute_outcome(
    published_utc: str, stock: pd.DataFrame, spy: pd.DataFrame, *, direction: str = "long",
    horizons: tuple[int, ...] = HORIZONS, entry_is_session_1: bool | None = None,
) -> dict:
    """Pure function: outcome of one call given cleaned frames. Unresolved fields are None."""
    off = (lambda h: h - 1 if entry_is_session_1 else h) if entry_is_session_1 is not None else _exit_offset
    out: dict = {"entry_date": None, "entry_open": None, "cost_bps": None, "status": "pending_entry",
                 "depth_252": None, "trail_60": None, "beta_60": None}
    for h in horizons:
        out.update({f"ret_{h}": None, f"spy_{h}": None, f"excess_{h}": None, f"exit_date_{h}": None})
    d = et_date(published_utc)
    pos = _first_pos_after(stock, d)
    if pos >= len(stock):
        return out
    entry_ts = stock.index[pos]
    if entry_ts not in spy.index:
        out["status"] = "no_benchmark_bar"
        return out
    sign = 1.0 if direction == "long" else -1.0
    raw_open = float(stock["open"].iloc[pos])
    adj_open = float(stock["adj_open"].iloc[pos])
    cost = (COST_BPS_LOW_PRICE if raw_open < LOW_PRICE_THRESHOLD else COST_BPS) / 1e4
    spy_pos = spy.index.get_loc(entry_ts)
    spy_open = float(spy["adj_open"].iloc[spy_pos])
    out.update({"entry_date": entry_ts.date().isoformat(), "entry_open": raw_open, "cost_bps": cost * 1e4, "status": "open"})
    resolved_any = False
    for h in horizons:
        k = off(h)
        if pos + k >= len(stock):
            continue
        exit_ts = stock.index[pos + k]
        if exit_ts not in spy.index:
            continue
        gross = float(stock["adj_close"].iloc[pos + k]) / adj_open - 1.0
        spy_ret = float(spy["adj_close"].loc[exit_ts]) / spy_open - 1.0
        net = sign * gross - cost
        out[f"ret_{h}"] = net
        out[f"spy_{h}"] = spy_ret
        out[f"excess_{h}"] = net - sign * spy_ret
        out[f"exit_date_{h}"] = exit_ts.date().isoformat()
        resolved_any = True
    if resolved_any:
        out["status"] = "partial"
        if all(out[f"excess_{h}"] is not None for h in horizons):
            out["status"] = "complete"
    # --- descriptive fields (logged, never used to filter) ---
    prior = stock.iloc[max(0, pos - 252):pos]
    if len(prior) >= 20:
        out["depth_252"] = adj_open / float(prior["adj_high"].max()) - 1.0
    if pos >= 61:
        out["trail_60"] = float(stock["adj_close"].iloc[pos - 1] / stock["adj_close"].iloc[pos - 61] - 1.0)
    if pos >= 61:
        rs = stock["adj_close"].pct_change().iloc[pos - 60:pos]
        rb = spy["adj_close"].pct_change().reindex(rs.index)
        both = pd.concat([rs, rb], axis=1).dropna()
        if len(both) >= 40 and both.iloc[:, 1].var() > 0:
            out["beta_60"] = float(both.iloc[:, 0].cov(both.iloc[:, 1]) / both.iloc[:, 1].var())
    return out


def evaluate_target(
    published_utc: str, stock: pd.DataFrame | None, *, direction: str, target: float | None,
    deadline: str | None, asof: date,
) -> dict:
    """Price-target status using raw daily highs (long) or lows (short): hit / missed / open / none."""
    if target is None:
        return {"target_status": "none", "target_hit_date": None}
    if stock is None:
        return {"target_status": "open", "target_hit_date": None}
    pos = _first_pos_after(stock, et_date(published_utc))
    dl = date.fromisoformat(deadline) if deadline else None
    window = stock.iloc[pos:]
    if dl is not None:
        window = window[window.index <= pd.Timestamp(dl)]
    if len(window):
        series, hit = (window["high"], window["high"] >= target) if direction == "long" else (window["low"], window["low"] <= target)
        if hit.any():
            return {"target_status": "hit", "target_hit_date": hit[hit].index[0].date().isoformat()}
    if dl is not None and asof > dl:
        return {"target_status": "missed", "target_hit_date": None}
    return {"target_status": "open", "target_hit_date": None}


def price_vs_high(frame: pd.DataFrame | None) -> float | None:
    """Latest close relative to the trailing 252-session high (<= 0 when below the high)."""
    if frame is None or len(frame) < 20:
        return None
    win = frame.iloc[-252:]
    return float(win["close"].iloc[-1] / win["high"].max() - 1.0)


__all__ = ["PriceStore", "clean_frame", "compute_outcome", "evaluate_target", "price_vs_high", "BENCHMARK", "np"]
