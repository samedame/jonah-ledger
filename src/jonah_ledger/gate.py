"""Forward gate. Message only: this module never trades and nothing downstream places orders."""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from .config import GATE_DROP_BEST, GATE_HORIZON, GATE_MIN_RESOLVED


@dataclass
class GateResult:
    status: str                       # COLLECTING | PASS | FAIL
    n_resolved: int
    mean_excess: float | None = None
    mean_drop_best: float | None = None
    month_equal_mean: float | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    message: str = ""


def month_equal_mean(pairs: list[tuple[str, float]]) -> float | None:
    """Mean of per-calendar-month means: each month weighs the same however many calls it holds."""
    by: dict[str, list[float]] = {}
    for month, x in pairs:
        by.setdefault(month, []).append(x)
    return mean(mean(v) for v in by.values()) if by else None


def mean_drop_best(values: list[float], k: int) -> float | None:
    if len(values) <= k:
        return None
    return mean(sorted(values, reverse=True)[k:])


def evaluate_gate(
    pairs: list[tuple[str, float]], *, min_n: int = GATE_MIN_RESOLVED, drop: int = GATE_DROP_BEST, horizon: int = GATE_HORIZON,
) -> GateResult:
    """``pairs`` = (calendar month 'YYYY-MM', excess return vs SPY) for long calls that finished ``horizon`` sessions."""
    vals = [x for _, x in pairs]
    n = len(vals)
    m = mean(vals) if n else None
    md = mean_drop_best(vals, drop)
    me = month_equal_mean(pairs)
    if n < min_n:
        return GateResult("COLLECTING", n, m, md, me, {}, f"COLLECTING: {n}/{min_n} long calls have finished {horizon} sessions. PAPER ONLY, not a signal.")
    checks = {
        "mean_excess_positive": m is not None and m > 0,
        f"positive_after_dropping_best_{drop}": md is not None and md > 0,
        "month_equal_weighted_positive": me is not None and me > 0,
    }
    ok = all(checks.values())
    def f(x):
        return "n/a" if x is None else f"{x:+.2%}"

    stats = f"n={n}, mean excess {f(m)}, without best {drop} {f(md)}, month-equal {f(me)}"
    if ok:
        msg = (
            f"PASS: passed these checks on this data ({stats}). PAPER ONLY, not a signal, and not a prediction.\n"
            "If you ever acted on it, the only sizing this ledger suggests: 1% of equity per call, cash not margin, "
            "total exposure capped (calls cluster; an earlier simulation at 2% per call became fully invested)."
        )
    else:
        failed = ", ".join(k for k, v in checks.items() if not v)
        msg = f"FAIL: did not pass these checks on this data ({stats}). Failed: {failed}. PAPER ONLY, not a signal."
    return GateResult("PASS" if ok else "FAIL", n, m, md, me, checks, msg)
