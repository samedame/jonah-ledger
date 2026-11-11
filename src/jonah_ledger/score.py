"""Daily scoring, summary statistics and the weekly markdown scorecard."""
from __future__ import annotations

import csv
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median

from . import alerts
from .config import BENCHMARK, GATE_HORIZON, HORIZONS, LONG_STRENGTHS, STRENGTHS, UTC, Paths
from .dates import et_date
from .gate import GateResult, evaluate_gate
from .ledger import Ledger
from .prices import PriceStore, compute_outcome, evaluate_target

OUT_COLS = (
    ["call_id", "ticker", "strength", "direction", "published_utc", "call_month", "entry_date", "entry_open",
     "cost_bps", "status", "depth_252", "trail_60", "beta_60", "target", "deadline", "target_status", "target_hit_date", "late_extraction"]
    + [f"{k}_{h}" for h in HORIZONS for k in ("ret", "spy", "excess", "exit_date")]
)


def build_outcomes(rows: list[dict], store: PriceStore, asof: date) -> list[dict]:
    spy = store.get(BENCHMARK)
    out = []
    for r in rows:
        o = {"call_id": r["call_id"], "ticker": r["ticker"], "strength": r["strength"], "direction": r["direction"],
             "published_utc": r["published_utc"], "call_month": et_date(r["published_utc"]).strftime("%Y-%m"),
             "target": r.get("target"), "deadline": r.get("deadline"), "late_extraction": r.get("late_extraction", False)}
        stock = store.get(r["ticker"])
        if stock is None or spy is None:
            o.update({"status": "no_price_data", "target_status": "open" if r.get("target") else "none"})
            out.append(o)
            continue
        o.update(compute_outcome(r["published_utc"], stock, spy, direction=r["direction"]))
        if (spy.index[-1] - stock.index[-1]).days > 10:
            o["status"] = "stale_data"
        o.update(evaluate_target(r["published_utc"], stock, direction=r["direction"], target=r.get("target"),
                                 deadline=r.get("deadline"), asof=asof))
        out.append(o)
    return out


def write_outcomes(path: Path, outcomes: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS, extrasaction="ignore")
        w.writeheader()
        for o in outcomes:
            w.writerow({c: ("" if o.get(c) is None else o.get(c)) for c in OUT_COLS})


def _vals(outs: list[dict], h: int) -> list[float]:
    return [o[f"excess_{h}"] for o in outs if o.get(f"excess_{h}") is not None]


def _drop_best_mean(vals: list[float], k: int) -> float | None:
    return mean(sorted(vals, reverse=True)[k:]) if len(vals) > k else None


def gate_pairs(outcomes: list[dict]) -> list[tuple[str, float]]:
    return [(o["call_month"], o[f"excess_{GATE_HORIZON}"]) for o in outcomes
            if o["strength"] in LONG_STRENGTHS and o.get(f"excess_{GATE_HORIZON}") is not None]


def summarize(outcomes: list[dict], *, unreadable: int, late: int) -> dict:
    longs = [o for o in outcomes if o["strength"] in LONG_STRENGTHS]
    s: dict = {"counts": {k: sum(1 for o in outcomes if o["strength"] == k) for k in STRENGTHS}, "total": len(outcomes),
               "unreadable": unreadable, "late": late, "horizons": {}, "by_strength": {}}
    for h in HORIZONS:
        v = _vals(longs, h)
        s["horizons"][h] = {"n": len(v), "mean": mean(v) if v else None, "median": median(v) if v else None,
                            "hit_rate": (sum(1 for x in v if x > 0) / len(v)) if v else None}
    for k in STRENGTHS:
        grp = [o for o in outcomes if o["strength"] == k]
        v = _vals(grp, GATE_HORIZON)
        s["by_strength"][k] = {"calls": len(grp), "resolved_60": len(v), "mean_60": mean(v) if v else None}
    v60 = _vals(longs, GATE_HORIZON)
    s["drop_best"] = {k: _drop_best_mean(v60, k) for k in (1, 2, 5)}
    pairs = gate_pairs(outcomes)
    s["month_equal_60"] = evaluate_gate(pairs, min_n=1).month_equal_mean if pairs else None
    cnt = Counter(o["ticker"] for o in longs)
    s["unique_long_tickers"] = len(cnt)
    s["top_tickers"] = cnt.most_common(5)
    s["top5_share"] = (sum(c for _, c in cnt.most_common(5)) / len(longs)) if longs else None
    tg = [o for o in outcomes if o.get("target")]
    s["targets"] = {"hit": sum(o["target_status"] == "hit" for o in tg), "missed": sum(o["target_status"] == "missed" for o in tg),
                    "open": sum(o["target_status"] == "open" for o in tg), "rows": tg}
    s["gate"] = evaluate_gate(pairs)
    return s


def _p(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def render_scorecard(s: dict, *, asof: date, head_hash: str, verify_ok: bool) -> str:
    g: GateResult = s["gate"]
    L = [f"# Jonah-Ledger weekly scorecard, week ending {asof.isoformat()}", "",
         "**PAPER ONLY, not a signal.** Research tool, not financial advice. Excess = net return after costs minus SPY over the same sessions.", "",
         f"- Gate: **{g.status}**. {g.message.splitlines()[0]}",
         f"- Ledger: {'verified' if verify_ok else '**VERIFY FAILED**'}, head `{head_hash}`",
         f"- Calls logged: {s['total']} ({', '.join(f'{k} {v}' for k, v in s['counts'].items())})",
         f"- Unreadable videos (no captions / blocked, not 'no calls'): {s['unreadable']}",
         f"- Calls extracted after their planned entry open (flagged late): {s['late']}", "",
         "## Long calls (buying + wants) vs SPY", "", "| Horizon | Resolved n | Mean excess | Median excess | Hit rate (excess > 0) |", "|---|---|---|---|---|"]
    for h, d in s["horizons"].items():
        hr = "n/a" if d["hit_rate"] is None else f"{d['hit_rate']:.0%}"
        L.append(f"| {h} sessions | {d['n']} | {_p(d['mean'])} | {_p(d['median'])} | {hr} |")
    L += ["", f"## Robustness at {GATE_HORIZON} sessions (long calls)", "",
          f"- Mean after dropping best 1 / 2 / 5 calls: {_p(s['drop_best'][1])} / {_p(s['drop_best'][2])} / {_p(s['drop_best'][5])}",
          f"- Month-equal-weighted mean: {_p(s['month_equal_60'])}",
          f"- Unique tickers: {s['unique_long_tickers']}; top 5 = {', '.join(f'{t} ({c})' for t, c in s['top_tickers']) or 'n/a'}"
          + ("" if s["top5_share"] is None else f" ({s['top5_share']:.0%} of long calls)"), "",
          f"## By strength label ({GATE_HORIZON} sessions)", "", "| Label | Calls | Resolved | Mean excess |", "|---|---|---|---|"]
    for k, d in s["by_strength"].items():
        L.append(f"| {k} | {d['calls']} | {d['resolved_60']} | {_p(d['mean_60'])} |")
    t = s["targets"]
    L += ["", "## Price-target ledger", "", f"Hit {t['hit']}, missed {t['missed']}, open {t['open']}", ""]
    if t["rows"]:
        L += ["| Ticker | Target | Deadline | Status |", "|---|---|---|---|"]
        L += [f"| {o['ticker']} | ${o['target']:g} | {o.get('deadline') or '-'} | {o['target_status']} |" for o in t["rows"]]
    L += ["", "## Gate detail", "", g.message, ""]
    return "\n".join(L)


def run_score(paths: Paths, *, store: PriceStore | None = None, asof: date | None = None, send_alerts: bool = True) -> list[dict]:
    asof = asof or datetime.now(UTC).date()
    store = store or PriceStore(paths.prices_cache)
    rows = Ledger(paths.ledger).effective_rows()
    store.get(BENCHMARK, refresh=True)
    outcomes = build_outcomes(rows, store, asof)
    write_outcomes(paths.outcomes, outcomes)
    if send_alerts:
        state = alerts.AlertState(paths.alerts_state)
        by_id = {r["call_id"]: r for r in rows}
        embeds, keys = [], []
        for o in outcomes:
            if o.get("target") and o["target_status"] in ("hit", "missed"):
                key = f"target:{o['call_id']}:{o['target_status']}"
                if not state.seen(key):
                    embeds.append(alerts.target_embed(by_id[o["call_id"]], o["target_status"], o.get("target_hit_date")))
                    keys.append(key)
        if embeds:
            alerts.send_embeds(embeds)
            for k in keys:
                state.mark(k)
            state.save()
    return outcomes


def unreadable_count(seen_path: Path) -> int:
    import json

    if not Path(seen_path).exists():
        return 0
    return sum(1 for v in json.loads(Path(seen_path).read_text())["videos"].values() if v.get("status") == "unreadable")


def run_weekly(paths: Paths, *, store: PriceStore | None = None, asof: date | None = None, send_alerts: bool = True) -> Path:
    asof = asof or datetime.now(UTC).date()
    outcomes = run_score(paths, store=store, asof=asof, send_alerts=send_alerts)
    led = Ledger(paths.ledger)
    ver = led.verify()
    rows = led.effective_rows()
    s = summarize(outcomes, unreadable=unreadable_count(paths.seen), late=sum(1 for r in rows if r.get("late_extraction")))
    md = render_scorecard(s, asof=asof, head_hash=ver.head, verify_ok=ver.ok)
    iso = asof.isocalendar()
    out = paths.weekly / f"{iso.year}-W{iso.week:02d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    if send_alerts:
        short = (f"Calls {s['total']} · long resolved@{GATE_HORIZON}: {s['horizons'][GATE_HORIZON]['n']} · mean excess "
                 f"{_p(s['horizons'][GATE_HORIZON]['mean'])} · unreadable videos {s['unreadable']} · targets hit/missed/open "
                 f"{s['targets']['hit']}/{s['targets']['missed']}/{s['targets']['open']}")
        alerts.send_embeds([alerts.digest_embed(short, s["gate"].message, ver.head, ver.ok)])
    return out
