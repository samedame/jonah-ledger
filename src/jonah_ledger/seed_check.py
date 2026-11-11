"""Acceptance checks against the two seed files from the backtest delivery.

Neither file's header was available when this was written, so columns are located by name patterns and every
guess is printed; override any of them with ``--map key=column``. Nothing here tunes conventions to force a match:
the report shows the result under the default convention and under the alternative one so the difference can be
documented in DECISIONS.md.
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from .config import BENCHMARK, ET, LONG_STRENGTHS, UTC
from .prices import PriceStore, compute_outcome, evaluate_target

EXPECT_MEAN_60, EXPECT_N, MEAN_TOL = 0.101, 151, 0.005
EXPECT_FORWARD = (5, 3, 11)  # hit, missed, open


def find_col(cols, includes, excludes=()) -> str | None:
    for c in cols:
        lc = c.lower()
        if all(i in lc for i in includes) and not any(e in lc for e in excludes):
            return c
    return None


def guess_calls_map(cols: list[str]) -> dict[str, str | None]:
    return {
        "date": find_col(cols, ["video", "date"]) or find_col(cols, ["call", "date"], ["entry"]) or find_col(cols, ["date"], ["entry", "exit"]),
        "ticker": find_col(cols, ["ticker"]) or find_col(cols, ["symbol"]),
        "direction": find_col(cols, ["direction"]) or find_col(cols, ["side"]),
        "strength": find_col(cols, ["strength"]) or find_col(cols, ["label"]) or find_col(cols, ["class"]),
        "entry_date": find_col(cols, ["entry", "date"]),
        "entry_open": find_col(cols, ["entry", "open"]) or find_col(cols, ["open"]),
        "excess_60": find_col(cols, ["spy", "60", "exc"], ["qqq"]) or find_col(cols, ["exc", "60"], ["qqq", "qqq"]),
    }


def guess_forward_map(cols: list[str]) -> dict[str, str | None]:
    return {
        "date": find_col(cols, ["date"], ["deadline", "target", "hit"]) or find_col(cols, ["call"]),
        "ticker": find_col(cols, ["ticker"]) or find_col(cols, ["symbol"]),
        "target": find_col(cols, ["target"], ["status", "date", "hit"]),
        "deadline": find_col(cols, ["deadline"]) or find_col(cols, ["due"]) or find_col(cols, ["expiry"]),
        "status": find_col(cols, ["status"]) or find_col(cols, ["result"]),
        "direction": find_col(cols, ["direction"]),
    }


def _published_utc(d) -> str:
    d = pd.Timestamp(d).date()
    return datetime.combine(d, time(12, 0), tzinfo=ET).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x):
    try:
        return float(str(x).replace("$", "").replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _is_long(row, m) -> bool:
    if m.get("strength") and str(row[m["strength"]]).strip().lower() in LONG_STRENGTHS:
        return True
    if m.get("direction") and str(row[m["direction"]]).strip().lower().startswith("long"):
        return not m.get("strength")
    return False


def rescore_calls(df: pd.DataFrame, store: PriceStore, *, colmap: dict | None = None) -> dict:
    m = {**guess_calls_map(list(df.columns)), **(colmap or {})}
    if not m.get("date") or not m.get("ticker"):
        raise ValueError(f"cannot find date/ticker columns in {list(df.columns)}; pass --map date=COL ticker=COL")
    spy = store.get(BENCHMARK)
    res = {"columns": m, "rows": len(df), "date_match": 0, "date_total": 0, "open_match": 0, "open_total": 0}
    mine = {True: [], False: []}  # keyed by entry_is_session_1
    seed_vals = []
    for _, r in df.iterrows():
        stock = store.get(str(r[m["ticker"]]).strip().upper().replace(".", "-"))
        if stock is None or spy is None:
            continue
        pub = _published_utc(r[m["date"]])
        direction = "short" if str(r.get(m["direction"], "long")).lower().startswith("short") else "long"
        o = compute_outcome(pub, stock, spy, direction=direction)
        if m.get("entry_date") and pd.notna(r[m["entry_date"]]):
            res["date_total"] += 1
            res["date_match"] += o["entry_date"] == pd.Timestamp(r[m["entry_date"]]).date().isoformat()
        if m.get("entry_open") and o["entry_open"] is not None and _num(r[m["entry_open"]]) is not None:
            res["open_total"] += 1
            seed_open = _num(r[m["entry_open"]])
            adj = float(stock.loc[pd.Timestamp(o["entry_date"]), "adj_open"])
            res["open_match"] += any(abs(seed_open - ref) <= max(0.01, 0.005 * ref) for ref in (o["entry_open"], adj))
        if _is_long(r, m):
            for flag in (True, False):
                oo = compute_outcome(pub, stock, spy, direction="long", entry_is_session_1=flag)
                if oo["excess_60"] is not None:
                    mine[flag].append(oo["excess_60"])
            if m.get("excess_60") and _num(r[m["excess_60"]]) is not None and o["excess_60"] is not None:
                v = _num(r[m["excess_60"]])
                seed_vals.append(v / 100.0 if abs(v) > 2 else v)
    res["entry_date_match_rate"] = res["date_match"] / res["date_total"] if res["date_total"] else None
    res["entry_open_match_rate"] = res["open_match"] / res["open_total"] if res["open_total"] else None
    for flag, name in ((True, "entry_day_is_session_1"), (False, "entry_day_is_session_0")):
        v = mine[flag]
        res[name] = {"n": len(v), "mean_excess_60": sum(v) / len(v) if v else None}
    res["seed_mean_excess_60"] = sum(seed_vals) / len(seed_vals) if seed_vals else None
    res["seed_n_with_excess_60"] = len(seed_vals)
    default = res["entry_day_is_session_1"]
    rate_ok = all(x is None or x >= 0.98 for x in (res["entry_date_match_rate"], res["entry_open_match_rate"]))
    mean_ok = default["mean_excess_60"] is not None and abs(default["mean_excess_60"] - EXPECT_MEAN_60) <= MEAN_TOL
    res["checks"] = {"entry_match_ge_98pct": rate_ok, f"mean_60_within_0.5pp_of_{EXPECT_MEAN_60:.1%}": mean_ok,
                     f"n_equals_{EXPECT_N}": default["n"] == EXPECT_N}
    res["passed"] = all(res["checks"].values())
    return res


def check_forward(df: pd.DataFrame, store: PriceStore, asof: date, *, colmap: dict | None = None, expect=EXPECT_FORWARD) -> dict:
    m = {**guess_forward_map(list(df.columns)), **(colmap or {})}
    for need in ("date", "ticker", "target"):
        if not m.get(need):
            raise ValueError(f"cannot find '{need}' column in {list(df.columns)}; pass --map {need}=COL")
    counts = {"hit": 0, "missed": 0, "open": 0, "none": 0}
    agree = total = 0
    for _, r in df.iterrows():
        stock = store.get(str(r[m["ticker"]]).strip().upper().replace(".", "-"))
        direction = "short" if m.get("direction") and str(r[m["direction"]]).lower().startswith("short") else "long"
        dl = r[m["deadline"]] if m.get("deadline") and pd.notna(r[m["deadline"]]) else None
        ev = evaluate_target(_published_utc(r[m["date"]]), stock, direction=direction, target=_num(r[m["target"]]),
                             deadline=pd.Timestamp(dl).date().isoformat() if dl is not None else None, asof=asof)
        counts[ev["target_status"]] += 1
        if m.get("status") and pd.notna(r[m["status"]]):
            total += 1
            agree += str(r[m["status"]]).strip().lower()[:3] == ev["target_status"][:3]
    got = (counts["hit"], counts["missed"], counts["open"])
    return {"columns": m, "asof": asof.isoformat(), "counts": counts, "expected_hit_missed_open": expect, "got_hit_missed_open": got,
            "row_agreement": (agree / total) if total else None, "passed": got == tuple(expect)}


def load_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path)
