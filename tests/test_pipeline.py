"""End-to-end poll with fakes (no network, no API key): idempotency, unreadable handling, late flag, discovery."""
import json
from datetime import datetime, timezone

import pytest

from conftest import FakeClient, make_frame
from jonah_ledger import discover, pipeline
from jonah_ledger.config import Paths
from jonah_ledger.ledger import Ledger
from jonah_ledger.prices import PriceStore
from jonah_ledger.transcript import TranscriptResult

TEXT = "today i am buying nvidia because i believe the data center business keeps growing " * 3
NOW = datetime(2026, 11, 8, 16, 0, tzinfo=timezone.utc)   # Sunday


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("JONAH_ALERTS", "off")


@pytest.fixture
def paths(tmp_path):
    p = Paths(tmp_path)
    p.channel.parent.mkdir(parents=True)
    p.channel.write_text(json.dumps({"channel_id": "UCtest"}))
    return p


def run(paths, videos, tr, client, symbols, now=NOW):
    store = PriceStore(paths.prices_cache, fetcher=lambda t: make_frame())
    return pipeline.run_poll(paths, now=now, client=client, symbols=symbols, store=store, send_alerts=False,
                             transcript_fn=lambda vid: tr, list_fn=lambda cid: (videos, "rss"))


def test_poll_logs_calls_once_and_is_idempotent(paths, symbols):
    v = discover.Video("AAAAAAAAAAA", "Buying NVDA", "2026-11-08T14:00:00Z")
    client = FakeClient({"calls": [{"ticker": "NVDA", "strength": "buying", "direction": "long", "target": None, "deadline": None,
                                    "quote": "i am buying nvidia because i believe"}]})
    s = run(paths, [v], TranscriptResult(v.video_id, True, TEXT), client, symbols)
    assert s["new_calls"] == 1 and s["extracted"] == 1
    rows = Ledger(paths.ledger).rows()
    assert rows[0]["extractor_version"] and rows[0]["prompt_hash"] and rows[0]["late_extraction"] is False
    assert Ledger(paths.ledger).verify().ok
    assert json.loads(paths.seen.read_text())["videos"][v.video_id]["status"] == "extracted"
    assert json.loads(paths.heartbeat.read_text())["source"] == "rss"
    s2 = run(paths, [v], TranscriptResult(v.video_id, True, TEXT), client, symbols)
    assert s2["to_process"] == 0 and len(Ledger(paths.ledger).rows()) == 1 and len(client.calls) == 1


def test_no_calls_is_recorded_distinctly_from_unreadable(paths, symbols):
    a = discover.Video("AAAAAAAAAAA", "chat", "2026-11-08T14:00:00Z")
    b = discover.Video("BBBBBBBBBBB", "silent", "2026-11-08T15:00:00Z")
    client = FakeClient({"calls": []})
    store = PriceStore(paths.prices_cache, fetcher=lambda t: make_frame())
    trs = {a.video_id: TranscriptResult(a.video_id, True, TEXT), b.video_id: TranscriptResult(b.video_id, False, reason="no auto-captions")}
    s = pipeline.run_poll(paths, now=NOW, client=client, symbols=symbols, store=store, send_alerts=False,
                          transcript_fn=lambda vid: trs[vid], list_fn=lambda c: ([a, b], "rss"))
    seen = json.loads(paths.seen.read_text())["videos"]
    assert seen[a.video_id]["status"] == "no_calls" and seen[b.video_id]["status"] == "unreadable"
    assert seen[b.video_id]["attempts"] == 1 and s["unreadable"] == 1 and s["no_calls"] == 1
    assert Ledger(paths.ledger).rows() == []


def test_unreadable_videos_are_retried_then_succeed(paths, symbols):
    v = discover.Video("AAAAAAAAAAA", "late captions", "2026-11-08T14:00:00Z")
    client = FakeClient({"calls": [{"ticker": "NVDA", "strength": "wants", "direction": "long", "target": None, "deadline": None,
                                    "quote": "i am buying nvidia because i believe"}]})
    run(paths, [v], TranscriptResult(v.video_id, False, reason="no auto-captions"), client, symbols)
    s = run(paths, [v], TranscriptResult(v.video_id, True, TEXT), client, symbols, now=NOW.replace(hour=19))
    assert s["new_calls"] == 1 and json.loads(paths.seen.read_text())["videos"][v.video_id]["status"] == "extracted"


def test_late_extraction_is_flagged(paths, symbols):
    v = discover.Video("AAAAAAAAAAA", "old", "2026-11-05T14:00:00Z")           # Thu; entry Fri open long passed by Sun
    discover.save_seen(paths.seen, {"_version": 1, "videos": {"ZZZZZZZZZZZ": {"status": "no_calls"}}})   # not a first run
    client = FakeClient({"calls": [{"ticker": "NVDA", "strength": "buying", "direction": "long", "target": None, "deadline": None,
                                    "quote": "i am buying nvidia because i believe"}]})
    run(paths, [v], TranscriptResult(v.video_id, True, TEXT), client, symbols)
    assert Ledger(paths.ledger).rows()[0]["late_extraction"] is True


def test_extraction_api_error_leaves_video_unseen_for_retry(paths, symbols):
    class Boom:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("api down")
    v = discover.Video("AAAAAAAAAAA", "x", "2026-11-08T14:00:00Z")
    s = run(paths, [v], TranscriptResult(v.video_id, True, TEXT), Boom, symbols)
    assert s["errors"] == 1 and v.video_id not in json.loads(paths.seen.read_text())["videos"]


def test_first_run_skips_old_backlog(paths, symbols):
    old = discover.Video("OOOOOOOOOOO", "old", "2026-10-01T14:00:00Z")
    new = discover.Video("NNNNNNNNNNN", "new", "2026-11-08T14:00:00Z")
    client = FakeClient({"calls": []})
    s = run(paths, [old, new], TranscriptResult(new.video_id, True, TEXT), client, symbols)
    seen = json.loads(paths.seen.read_text())["videos"]
    assert seen[old.video_id]["status"] == "baseline_skipped" and s["to_process"] == 1


def test_discovery_failure_does_not_touch_heartbeat(paths, symbols):
    def boom(cid):
        raise RuntimeError("both sources failed")
    with pytest.raises(RuntimeError):
        pipeline.run_poll(paths, now=NOW, client=FakeClient({"calls": []}), symbols=symbols, send_alerts=False, list_fn=boom)
    assert not paths.heartbeat.exists()


RSS = """<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">
<entry><yt:videoId>XbtQQDWImI8</yt:videoId><title>I'm buying this tiny Energy stock NOW!!</title><published>2026-11-08T14:06:14+00:00</published></entry>
<entry><yt:videoId>zpeCW1S_JAA</yt:videoId><title>Adding Nvidia</title><published>2026-11-09T13:00:00-05:00</published></entry></feed>"""


def test_rss_parse_and_fallback_order():
    v = discover.parse_rss(RSS)
    assert [(x.video_id, x.published_utc) for x in v] == [("XbtQQDWImI8", "2026-11-08T14:06:14Z"), ("zpeCW1S_JAA", "2026-11-09T18:00:00Z")]
    got, src = discover.list_recent("UC", rss=lambda c: (_ for _ in ()).throw(RuntimeError("404")), fallback=lambda c: [discover.Video("AAAAAAAAAAA", "t", None)])
    assert src == "yt-dlp" and got[0].published_utc is None
    with pytest.raises(RuntimeError):
        discover.list_recent("UC", rss=lambda c: [], fallback=lambda c: [])


def test_channel_id_resolution_is_cached(tmp_path):
    html = '<link rel="alternate" type="application/rss+xml" href="https://www.youtube.com/feeds/videos.xml?channel_id=UCKZDPyNulHpz20PpGSMbW5g">'
    p = tmp_path / "channel.json"
    assert discover.resolve_channel_id(p, http_get=lambda u: html) == "UCKZDPyNulHpz20PpGSMbW5g"
    assert discover.resolve_channel_id(p, http_get=lambda u: (_ for _ in ()).throw(AssertionError("should be cached"))) == "UCKZDPyNulHpz20PpGSMbW5g"
