"""IPO watcher parsing/outcomes, and the seed-check harness (self-consistency + real seed files when present)."""
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from conftest import make_frame
from jonah_ledger import ipo_watch, seed_check
from jonah_ledger.config import ET, IPO_NOTE, Paths
from jonah_ledger.prices import PriceStore, compute_outcome

NASDAQ = {"data": {
    "priced": {"rows": [{"dealID": "1-1", "proposedTickerSymbol": "NARA", "companyName": "Narragansett Bancorp", "proposedExchange": "NASDAQ Capital",
                         "proposedSharePrice": "10.00", "sharesOffered": "9,098,800", "pricedDate": "11/02/2026"}]},
    "upcoming": {"upcomingTable": {"rows": [{"dealID": "2-2", "proposedTickerSymbol": "WWB", "companyName": "WINDWARD BIO", "proposedExchange": "NASDAQ Global",
                                             "proposedSharePrice": "16.00-18.00", "sharesOffered": "10,294,117", "expectedPriceDate": "11/13/2026"}]}}}}


def test_parse_nasdaq_keeps_everything_unranked():
    d = ipo_watch.parse_nasdaq(NASDAQ)
    assert {(x["symbol"], x["kind"], x["date"]) for x in d} == {("NARA", "priced", "2026-11-02"), ("WWB", "upcoming", "2026-11-13")}
    emb = ipo_watch.deal_embeds(d, "Upcoming IPOs")
    assert IPO_NOTE in str(emb) and "16.00-18.00" in str(emb)


def test_fallback_to_edgar_then_skip(tmp_path):
    edgar = {"hits": {"hits": [{"_id": "x:y", "_source": {"display_names": ["reAlpha Tech Corp.  (AIRE)  (CIK 0001859199)"], "file_date": "2026-11-05"}}]}}

    def get_json(url, headers):
        if "nasdaq" in url:
            raise OSError("blocked")
        return edgar
    deals, src = ipo_watch.fetch_deals(date(2026, 11, 10), get_json=get_json)
    assert src == "edgar" and deals[0]["symbol"] == "AIRE"
    p = Paths(tmp_path)
    out = ipo_watch.run_ipo_watch(p, today=date(2026, 11, 10), get_json=lambda u, h: (_ for _ in ()).throw(OSError("down")), send_alerts=False)
    assert out["skipped"] is True


def test_outcomes_logged_once_with_offer_open_close(tmp_path, monkeypatch):
    monkeypatch.setenv("JONAH_ALERTS", "off")
    p = Paths(tmp_path)
    frame = make_frame(start="2026-11-02", periods=6, base=12.0, step=0.0)   # open 12, close 12.5
    store = PriceStore(p.prices_cache, fetcher=lambda t: frame)
    r = ipo_watch.run_ipo_watch(p, today=date(2026, 11, 10), get_json=lambda u, h: NASDAQ, store=store, send_alerts=False)
    assert r["new_priced"] == 1 and r["new_upcoming"] == 1 and r["outcomes_logged"] == 1
    row = json.loads(p.ipo_outcomes.read_text().splitlines()[0])
    assert row["offer_price"] == 10.0 and row["day1_open"] == 12.0 and row["day1_close"] == 12.5
    assert row["offer_to_close"] == pytest.approx(0.25) and row["offer_to_open"] == pytest.approx(0.2)
    r2 = ipo_watch.run_ipo_watch(p, today=date(2026, 11, 11), get_json=lambda u, h: NASDAQ, store=store, send_alerts=False)
    assert r2["outcomes_logged"] == 0 and r2["new_priced"] == 0 and len(p.ipo_outcomes.read_text().splitlines()) == 1


# ---------- seed harness -------------------------------------------------------------------------
def _synthetic_seed(store):
    spy, stock = store.get("SPY"), store.get("AAA")
    rows = []
    for d in ["2026-02-02", "2026-02-13", "2026-03-06", "2026-04-10"]:
        pub = datetime.combine(date.fromisoformat(d), datetime.min.time().replace(hour=12), tzinfo=ET).astimezone(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        o = compute_outcome(pub, stock, spy)
        rows.append({"date": d, "ticker": "AAA", "direction": "long", "strength": "buying", "entry_date": o["entry_date"],
                     "entry_open": o["entry_open"], "excess_spy_60": o["excess_60"], "excess_qqq_60": 0.0})
    return pd.DataFrame(rows)


def _store(tmp_path):
    return PriceStore(tmp_path, fetcher=lambda t: make_frame(base=400, step=2.0) if t == "SPY" else make_frame(base=100, step=1.0))


def test_rescore_harness_matches_itself_and_reports_both_conventions(tmp_path):
    store = _store(tmp_path)
    res = seed_check.rescore_calls(_synthetic_seed(store), store)
    assert res["columns"]["excess_60"] == "excess_spy_60"           # SPY column chosen, not QQQ
    assert res["entry_date_match_rate"] == 1.0 and res["entry_open_match_rate"] == 1.0
    assert res["seed_mean_excess_60"] == pytest.approx(res["entry_day_is_session_1"]["mean_excess_60"])
    assert res["entry_day_is_session_1"]["n"] == 4 and res["checks"]["n_equals_151"] is False and res["passed"] is False


def test_forward_ledger_status_reproduction(tmp_path):
    store = _store(tmp_path)
    f = store.get("AAA")
    hit_target = float(f["high"].loc["2026-03-12"])
    df = pd.DataFrame([
        {"date": "2026-03-06", "ticker": "AAA", "target": hit_target, "deadline": "2026-03-31", "status": "Hit"},
        {"date": "2026-03-06", "ticker": "AAA", "target": 99999, "deadline": "2026-03-31", "status": "Missed"},
        {"date": "2026-03-06", "ticker": "AAA", "target": 99999, "deadline": "2027-06-30", "status": "Open"}])
    res = seed_check.check_forward(df, store, date(2026, 12, 1), expect=(1, 1, 1))
    assert res["got_hit_missed_open"] == (1, 1, 1) and res["passed"] and res["row_agreement"] == 1.0


SEED = Path(__file__).resolve().parents[1] / "data" / "seed"


@pytest.mark.skipif(not (SEED / "calls_scored.csv").exists(), reason="data/seed/calls_scored.csv not provided: NOT VERIFIED")
def test_real_calls_seed_rescore():
    res = seed_check.rescore_calls(seed_check.load_csv(SEED / "calls_scored.csv"), PriceStore(SEED.parent / "prices_cache"))
    assert res["passed"], json.dumps(res, indent=1, default=str)


@pytest.mark.skipif(not (SEED / "forward_ledger.csv").exists(), reason="data/seed/forward_ledger.csv not provided: NOT VERIFIED")
def test_real_forward_ledger_statuses():
    res = seed_check.check_forward(seed_check.load_csv(SEED / "forward_ledger.csv"), PriceStore(SEED.parent / "prices_cache"), date.today())
    assert res["passed"], json.dumps(res, indent=1, default=str)
