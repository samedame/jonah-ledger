"""Extractor schema validation, ticker resolution, quote rules, and the API call shape."""
from datetime import date

from conftest import FakeClient
from jonah_ledger import extract
from jonah_ledger.symbols import normalize_ticker, parse_symbol_files
from jonah_ledger.transcript import parse_json3

TRANSCRIPT = ("so today i am buying nvidia because i think the data center business keeps growing and i also want to buy apple "
              "on a dip and honestly tesla is just a bullish story for me but i have no position there")


def call(**kw):
    c = {"ticker": "NVDA", "strength": "buying", "direction": "long", "target": None, "deadline": None,
         "quote": "i am buying nvidia because i think"}
    c.update(kw)
    return c


def validate(calls, symbols, transcript=TRANSCRIPT):
    return extract.validate_calls({"calls": calls}, symbols=symbols, video_date=date(2026, 11, 8), transcript=transcript)


def test_happy_path(symbols):
    rows, stats = validate([call(), call(ticker="AAPL", strength="wants", quote="i also want to buy apple on a dip")], symbols)
    assert [(r["ticker"], r["strength"], r["direction"]) for r in rows] == [("NVDA", "buying", "long"), ("AAPL", "wants", "long")]
    assert stats == {}


def test_unresolved_ticker_is_dropped_and_counted(symbols):
    rows, stats = validate([call(ticker="ZZZZZ"), call(ticker="in video"), call()], symbols)
    assert [r["ticker"] for r in rows] == ["NVDA"]
    assert stats["dropped_unresolved_ticker"] == 2


def test_labels_directions_and_garbage(symbols):
    rows, stats = validate([call(strength="loves"), call(ticker="AAPL", strength="short", direction="long", quote="i want to buy apple"),
                            call(ticker="TSLA", strength="mention", direction="maybe", quote="tesla is just a bullish story"), "junk", {}], symbols)
    assert [(r["ticker"], r["direction"]) for r in rows] == [("AAPL", "short")]     # short is forced to direction=short
    assert stats["dropped_invalid"] == 4


def test_bad_payload_never_raises(symbols):
    for p in (None, [], {"calls": "x"}, {"nope": 1}):
        rows, stats = extract.validate_calls(p, symbols=symbols, video_date=date(2026, 11, 8))
        assert rows == [] and stats["dropped_bad_payload"] == 1


def test_quote_truncated_to_15_words(symbols):
    long_quote = " ".join(["so", "today", "i", "am", "buying", "nvidia", "because", "i", "think", "the", "data", "center", "business", "keeps", "growing", "and", "i"])
    rows, stats = validate([call(quote=long_quote)], symbols)
    assert len(rows[0]["quote"].split()) == 15 and stats["quotes_truncated"] == 1


def test_ungrounded_quote_dropped(symbols):
    rows, stats = validate([call(quote="we are going to the moon with rockets")], symbols)
    assert rows == [] and stats["dropped_ungrounded_quote"] == 1


def test_one_row_per_ticker_strongest_wins(symbols):
    rows, stats = validate([call(strength="mention", quote="tesla is just a bullish story"), call(ticker="NVDA", strength="mention", quote="i think the data center business"),
                            call(strength="buying")], symbols)
    nv = [r for r in rows if r["ticker"] == "NVDA"]
    assert len(nv) == 1 and nv[0]["strength"] == "buying" and stats["merged_duplicate_ticker"] == 2


def test_target_and_deadline_rules(symbols):
    ok, _ = validate([call(target=250, deadline="2026-12-31")], symbols)
    assert ok[0]["target"] == 250.0 and ok[0]["deadline"] == "2026-12-31"
    past, st = validate([call(target=250, deadline="2026-01-01")], symbols)
    assert past[0]["deadline"] is None and st["deadlines_discarded"] == 1
    no_target, _ = validate([call(target=None, deadline="2026-12-31")], symbols)
    assert no_target[0]["deadline"] is None
    bad, _ = validate([call(target=-5, deadline="soon")], symbols)
    assert bad[0]["target"] is None and bad[0]["deadline"] is None


def test_extract_calls_with_fake_client(symbols):
    client = FakeClient({"calls": [call(), call(ticker="NOPE")]})
    res = extract.extract_calls(TRANSCRIPT, title="t", published_utc="2026-11-08T14:00:00Z", symbols=symbols, client=client, model="m")
    assert [c["ticker"] for c in res.calls] == ["NVDA"] and res.stats["dropped_unresolved_ticker"] == 1
    kw = client.calls[0]
    assert kw["temperature"] == 0 and kw["tool_choice"] == {"type": "tool", "name": "record_calls"} and kw["model"] == "m"
    assert "2026-11-08" in kw["messages"][0]["content"]


def test_temperature_rejection_retries_without_it(symbols):
    client = FakeClient({"calls": [call()]}, reject_temperature=True)
    res = extract.extract_calls(TRANSCRIPT, title="t", published_utc="2026-11-08T14:00:00Z", symbols=symbols, client=client)
    assert len(res.calls) == 1 and "temperature" not in client.calls[-1]


def test_forced_tool_choice_rejected_falls_back_to_auto_and_both_params_can_be_dropped(symbols):
    client = FakeClient({"calls": [call()]}, reject_tool_choice=True, reject_temperature=True)
    res = extract.extract_calls(TRANSCRIPT, title="t", published_utc="2026-11-08T14:00:00Z", symbols=symbols, client=client)
    assert [c["ticker"] for c in res.calls] == ["NVDA"]
    last = client.calls[-1]
    assert last["tool_choice"] == {"type": "auto"} and "temperature" not in last and last["tools"]


def test_text_json_fallback_and_unparseable_is_an_error_not_no_calls(symbols):
    import json

    import pytest
    ok = FakeClient(None, text_only='Here you go: {"calls": [' + json.dumps(call()) + "]}")
    res = extract.extract_calls(TRANSCRIPT, title="t", published_utc="2026-11-08T14:00:00Z", symbols=symbols, client=ok)
    assert [c["ticker"] for c in res.calls] == ["NVDA"]
    with pytest.raises(RuntimeError):
        extract.extract_calls(TRANSCRIPT, title="t", published_utc="2026-11-08T14:00:00Z", symbols=symbols,
                              client=FakeClient(None, text_only="I could not find any calls, sorry."))


def test_unrelated_api_errors_still_raise(symbols):
    import pytest

    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("rate limited")
    with pytest.raises(RuntimeError, match="rate limited"):
        extract.extract_calls(TRANSCRIPT, title="t", published_utc="2026-11-08T14:00:00Z", symbols=symbols, client=Boom)


def test_version_and_prompt_hash_are_stable():
    assert extract.prompt_hash() == extract.prompt_hash() and len(extract.prompt_hash()) == 64
    assert extract.extractor_version().startswith(extract.EXTRACTOR_SEMVER + "+")


def test_ledger_fields_carry_version_and_hash():
    f = extract.to_ledger_fields(call(), video_id="V", published_utc="2026-11-08T14:00:00Z", model="m", extracted_at="2026-11-08T15:00:00Z")
    assert f["extractor_version"] == extract.extractor_version() and f["prompt_hash"] == extract.prompt_hash()


def test_ticker_normalisation_and_symbol_parsing():
    assert normalize_ticker("$brk.b") == "BRK-B"
    nas = "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares\nNVDA|NVIDIA|Q|N|N|100|N|N\nZTST|Test|Q|Y|N|100|N|N\nFile Creation Time: x|||||||\n"
    oth = "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\nBRK.B|Berkshire|N|BRK.B|N|100|N|BRK.B\nFile Creation Time: x|||||||\n"
    assert set(parse_symbol_files(nas, oth)) == {"NVDA", "BRK-B"}


def test_parse_json3():
    blob = {"events": [{"segs": [{"utf8": "\n"}]}, {"segs": [{"utf8": "on"}, {"utf8": " Friday,"}]}, {"segs": [{"utf8": "so"}, {"utf8": " yes"}]}, {"tStartMs": 1}]}
    assert parse_json3(blob) == "on Friday, so yes"
