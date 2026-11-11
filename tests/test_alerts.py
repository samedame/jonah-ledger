"""Discord formatting limits, the kill switch, retries and the stale-bot heartbeat."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from jonah_ledger import alerts

ROW = {"ticker": "NVDA", "strength": "buying", "direction": "long", "quote": "i'm buying this now", "video_id": "XbtQQDWImI8",
       "target": 250.0, "deadline": "2026-12-31"}


class Sess:
    def __init__(self, codes=(204,)):
        self.codes, self.posts = list(codes), []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        code = self.codes.pop(0) if self.codes else 204
        return SimpleNamespace(status_code=code, json=lambda: {"retry_after": 0})


def test_clip_and_limits():
    e = alerts.make_embed("T" * 1000, "D" * 9000, fields=[("N" * 500, "V" * 3000, True)] * 40)
    assert len(e["title"]) <= 256 and len(e["description"]) <= 4096 and len(e["fields"]) <= 25
    assert all(len(f["name"]) <= 256 and len(f["value"]) <= 1024 for f in e["fields"])
    assert len(e["footer"]["text"]) <= 2048 and alerts.embed_size(e) <= 6000


def test_total_size_enforced_by_shrinking():
    e = alerts.make_embed("t", "D" * 4000, fields=[("n", "V" * 1000, False)] * 25)
    assert alerts.embed_size(e) <= 6000


def test_chunking_respects_10_embeds_and_6000_chars():
    embeds = [alerts.make_embed("x", "y" * 1500) for _ in range(23)]
    msgs = alerts.chunk_embeds(embeds)
    assert sum(len(m) for m in msgs) == 23
    assert all(len(m) <= 10 and sum(alerts.embed_size(e) for e in m) <= 6000 for m in msgs)


def test_new_call_embed_content():
    e = alerts.new_call_embed(ROW, vs_high=-0.234)
    text = str(e)
    assert "PAPER ONLY, not a signal" in text and "Next open" in text and "-23.4%" in text and "youtu.be/XbtQQDWImI8" in text
    assert "$250" in text and "2026-12-31" in text
    assert "n/a" in str(alerts.new_call_embed(ROW, vs_high=None))
    assert alerts.new_call_embed({**ROW, "quote": "q " * 3000}, vs_high=None) and \
        alerts.embed_size(alerts.new_call_embed({**ROW, "quote": "q " * 3000}, vs_high=None)) <= 6000


def test_kill_switch_silences_but_does_not_raise(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("JONAH_ALERTS", "off")
    s = Sess()
    assert alerts.send_embeds([alerts.make_embed("a")], session=s) == 0 and s.posts == []
    monkeypatch.setenv("JONAH_ALERTS", "on")
    assert alerts.send_embeds([alerts.make_embed("a")], session=s) == 1 and s.posts[0]["allowed_mentions"] == {"parse": []}


def test_no_webhook_means_no_post(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("JONAH_ALERTS", "on")
    s = Sess()
    assert alerts.send_embeds([alerts.make_embed("a")], session=s) == 0 and s.posts == []


def test_rate_limit_retry(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv("JONAH_ALERTS", "on")
    s = Sess(codes=[429, 204])
    assert alerts.send_embeds([alerts.make_embed("a")], session=s, sleep=lambda x: None) == 1 and len(s.posts) == 2


def test_alert_state_dedupes(tmp_path):
    st = alerts.AlertState(tmp_path / "s.json")
    assert not st.seen("k")
    st.mark("k"); st.save()
    assert alerts.AlertState(tmp_path / "s.json").seen("k")


def test_heartbeat_stale_after_48h_and_warns_once_per_day(tmp_path):
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    hb, sent = tmp_path / "hb.json", []
    st = alerts.AlertState(tmp_path / "s.json")
    alerts.write_heartbeat(hb, now - timedelta(hours=47), "rss")
    assert not alerts.maybe_stale_alert(hb, st, now, send=sent.append) and sent == []
    alerts.write_heartbeat(hb, now - timedelta(hours=49), "rss")
    assert alerts.maybe_stale_alert(hb, st, now, send=sent.append) and len(sent) == 1
    assert not alerts.maybe_stale_alert(hb, st, now + timedelta(hours=3), send=sent.append) and len(sent) == 1
    assert alerts.maybe_stale_alert(hb, st, now + timedelta(hours=25), send=sent.append) and len(sent) == 2
    # no heartbeat file at all counts as stale
    assert alerts.maybe_stale_alert(tmp_path / "none.json", alerts.AlertState(tmp_path / "s2.json"), now, send=sent.append)
