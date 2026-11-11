"""Outcome arithmetic: entry, horizons, costs, benchmark, direction, targets, cache."""
from datetime import date

import pandas as pd
import pytest

from conftest import make_frame
from jonah_ledger.prices import PriceStore, clean_frame, compute_outcome, evaluate_target, price_vs_high

PUB = "2026-03-06T20:00:00Z"   # Friday 3pm ET -> entry Monday 2026-03-09


def test_entry_horizons_and_excess_session1_convention():
    stock = make_frame(base=100, step=1.0)
    spy = make_frame(base=400, step=2.0)
    o = compute_outcome(PUB, stock, spy)
    assert o["entry_date"] == "2026-03-09"
    pos = stock.index.get_loc(pd.Timestamp("2026-03-09"))
    entry = stock["open"].iloc[pos]
    # 1st session close is the entry-day close
    assert o["ret_1"] == pytest.approx(stock["close"].iloc[pos] / entry - 1 - 0.0010)
    assert o["exit_date_1"] == "2026-03-09"
    assert o["exit_date_5"] == stock.index[pos + 4].date().isoformat()
    spy_entry = spy["open"].iloc[pos]
    assert o["spy_60"] == pytest.approx(spy["close"].iloc[pos + 59] / spy_entry - 1)
    assert o["excess_60"] == pytest.approx(o["ret_60"] - o["spy_60"])


def test_alternate_convention_counts_from_next_session():
    stock, spy = make_frame(), make_frame(base=400, step=2.0)
    a = compute_outcome(PUB, stock, spy, entry_is_session_1=True)
    b = compute_outcome(PUB, stock, spy, entry_is_session_1=False)
    assert b["exit_date_1"] == "2026-03-10" and a["exit_date_1"] == "2026-03-09"


def test_cost_is_10bps_or_30bps_under_5_dollars():
    spy = make_frame(base=400)
    normal = compute_outcome(PUB, make_frame(base=50, step=0), spy)
    penny = compute_outcome(PUB, make_frame(base=3, step=0), spy)
    assert normal["cost_bps"] == 10 and penny["cost_bps"] == 30
    # flat prices: only the intraday +0.5 close and the cost matter
    assert penny["ret_1"] == pytest.approx(0.5 / 3 - 0.003)


def test_unresolved_horizons_are_none_and_status_partial():
    stock = make_frame(periods=80)             # ends before 250 sessions after entry
    o = compute_outcome(PUB, stock, make_frame(periods=80, base=400))
    assert o["excess_20"] is not None and o["excess_250"] is None and o["status"] == "partial"


def test_entry_in_the_future_is_pending():
    stock = make_frame(periods=40)             # data ends 2026-02-26
    o = compute_outcome("2026-04-01T12:00:00Z", stock, stock)
    assert o["entry_date"] is None and o["status"] == "pending_entry"


def test_short_direction_flips_sign():
    stock, spy = make_frame(base=100, step=1.0), make_frame(base=400, step=2.0)
    lo, sh = compute_outcome(PUB, stock, spy), compute_outcome(PUB, stock, spy, direction="short")
    assert sh["ret_20"] == pytest.approx(-(lo["ret_20"] + 0.001) - 0.001)
    assert sh["excess_20"] == pytest.approx(sh["ret_20"] + lo["spy_20"])


def test_descriptive_fields_logged():
    stock, spy = make_frame(), make_frame(base=400, step=2.0)
    o = compute_outcome("2026-10-02T20:00:00Z", stock, spy)
    assert o["depth_252"] is not None and o["depth_252"] <= 0.001
    assert o["trail_60"] is not None and o["trail_60"] > 0
    assert o["beta_60"] is not None


def test_price_target_statuses():
    f = make_frame(base=100, step=1.0)         # high on 2026-03-09 is ~ 100+45+1.5
    hi = float(f["high"].loc["2026-03-12"])
    common = dict(direction="long", asof=date(2026, 12, 1))
    assert evaluate_target(PUB, f, target=hi, deadline="2026-03-31", **common)["target_status"] == "hit"
    assert evaluate_target(PUB, f, target=10_000, deadline="2026-03-31", **common)["target_status"] == "missed"
    assert evaluate_target(PUB, f, target=10_000, deadline=None, **common)["target_status"] == "open"
    assert evaluate_target(PUB, f, target=10_000, deadline="2027-01-31", **common)["target_status"] == "open"
    assert evaluate_target(PUB, f, target=None, deadline=None, **common)["target_status"] == "none"
    assert evaluate_target(PUB, f, direction="short", target=150, deadline=None, asof=date(2026, 12, 1))["target_status"] == "hit"


def test_target_hit_only_counts_inside_window():
    f = make_frame(base=100, step=1.0)
    late_hit = float(f["high"].iloc[-1])       # only reached at the very end
    r = evaluate_target(PUB, f, direction="long", target=late_hit, deadline="2026-03-20", asof=date(2026, 12, 1))
    assert r["target_status"] == "missed"


def test_store_caches_and_survives_fetch_failure(tmp_path):
    calls = []

    def fetch(t):
        calls.append(t)
        return make_frame(periods=30)

    s = PriceStore(tmp_path, fetcher=fetch)
    assert len(s.get("AAA")) == 30 and (tmp_path / "AAA.csv").exists()
    s2 = PriceStore(tmp_path, fetcher=fetch)
    s2.get("AAA")
    assert calls == ["AAA"]                    # second store read the cache
    s3 = PriceStore(tmp_path, fetcher=lambda t: (_ for _ in ()).throw(RuntimeError("down")), max_age_hours=0)
    assert len(s3.get("AAA")) == 30            # stale cache beats an outage
    assert PriceStore(tmp_path, fetcher=lambda t: None).get("ZZZ") is None


def test_price_vs_high():
    f = make_frame(periods=100, base=100, step=-0.5)
    assert price_vs_high(f) < 0
    assert price_vs_high(make_frame(periods=5)) is None
    assert clean_frame(f).shape[1] == 8
