"""Gate logic (including drop-best-two and month-equal weighting) and scorecard statistics."""
from datetime import date

from jonah_ledger.gate import evaluate_gate, month_equal_mean
from jonah_ledger.score import render_scorecard, summarize


def pairs(vals, month="2026-01"):
    return [(month, v) for v in vals]


def test_collecting_until_30_resolved():
    g = evaluate_gate(pairs([0.1] * 29))
    assert g.status == "COLLECTING" and g.n_resolved == 29 and "29/30" in g.message


def test_pass_when_all_three_checks_positive():
    g = evaluate_gate([(f"2026-{m:02d}", 0.05) for m in range(1, 11) for _ in range(3)])
    assert g.status == "PASS" and all(g.checks.values())
    assert "1% of equity per call" in g.message and "cash not margin" in g.message and "PAPER ONLY" in g.message
    assert "will make money" not in g.message and "passed these checks on this data" in g.message


def test_fail_when_positive_mean_depends_on_best_two():
    vals = [1.0, 1.0] + [-0.02] * 28
    g = evaluate_gate(pairs(vals))
    assert g.mean_excess > 0 and g.mean_drop_best < 0
    assert g.status == "FAIL" and g.checks["positive_after_dropping_best_2"] is False


def test_fail_when_month_equal_weighted_mean_is_negative():
    p = [("2026-01", 0.10)] * 20 + [(f"2026-{m:02d}", -0.05) for m in range(2, 12)]
    g = evaluate_gate(p)
    assert g.mean_excess > 0 and g.mean_drop_best > 0 and g.month_equal_mean < 0
    assert g.status == "FAIL" and g.checks["month_equal_weighted_positive"] is False


def test_fail_when_mean_negative():
    assert evaluate_gate(pairs([-0.01] * 30)).status == "FAIL"


def test_gate_boundary_30_is_enough_and_zero_is_not_positive():
    assert evaluate_gate(pairs([0.0] * 30)).status == "FAIL"
    assert evaluate_gate(pairs([0.01] * 30)).status == "PASS"


def test_month_equal_mean():
    assert month_equal_mean([("a", 1.0), ("a", 1.0), ("a", 1.0), ("b", -1.0)]) == 0.0


def outcome(i, ex60, strength="buying", month="2026-01", ticker=None):
    o = {"call_id": f"c{i}", "ticker": ticker or f"T{i}", "strength": strength, "call_month": month, "target": None,
         "target_status": "none", "late_extraction": False}
    for h in (1, 5, 20, 60, 120, 250):
        o[f"excess_{h}"] = ex60 if h in (1, 5, 20, 60) else None
    return o


def test_summarize_drop_best_and_concentration():
    outs = [outcome(i, v, ticker="NVDA" if i < 3 else None) for i, v in enumerate([0.5, 0.3, 0.1, 0.0, -0.1, -0.2])]
    outs.append(outcome(99, 0.9, strength="mention"))
    s = summarize(outs, unreadable=2, late=1)
    assert s["horizons"][60]["n"] == 6                       # mention excluded from long stats
    assert round(s["horizons"][60]["mean"], 6) == round((0.5 + 0.3 + 0.1 + 0 - 0.1 - 0.2) / 6, 6)
    assert round(s["drop_best"][1], 6) == round((0.3 + 0.1 + 0 - 0.1 - 0.2) / 5, 6)
    assert round(s["drop_best"][2], 6) == round((0.1 + 0 - 0.1 - 0.2) / 4, 6)
    assert s["drop_best"][5] == -0.2 or s["drop_best"][5] is not None
    assert s["top_tickers"][0] == ("NVDA", 3) and s["unique_long_tickers"] == 4
    assert s["by_strength"]["mention"]["resolved_60"] == 1 and s["unreadable"] == 2


def test_month_equal_in_summary_and_scorecard_text():
    outs = [outcome(i, 0.10, month="2026-01") for i in range(3)] + [outcome(10, -0.30, month="2026-02")]
    s = summarize(outs, unreadable=0, late=0)
    assert round(s["month_equal_60"], 6) == round((0.10 - 0.30) / 2, 6)
    md = render_scorecard(s, asof=date(2026, 11, 14), head_hash="ab" * 32, verify_ok=True)
    for needle in ("PAPER ONLY", "COLLECTING", "Unreadable videos", "Price-target ledger", "Month-equal-weighted", "Hit rate"):
        assert needle in md
