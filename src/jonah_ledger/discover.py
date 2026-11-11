"""Find new videos and Shorts on the channel.

Primary source: the channel's public RSS feed. YouTube's RSS endpoint is intermittently 404 (it did
return 404 from the author's machine on 2026-11-10), so the fallback is ``yt-dlp --flat-playlist`` on
the channel's /videos and /shorts tabs. The fallback has no publish timestamp; the transcript stage
fills it in from yt-dlp's per-video metadata.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET_
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .config import BACKFILL_DAYS, CHANNEL_HANDLE, UNREADABLE_MAX_ATTEMPTS, UNREADABLE_RETRY_WINDOW_HOURS, UTC

UA = "Mozilla/5.0 (compatible; jonah-ledger research bot)"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


@dataclass
class Video:
    video_id: str
    title: str
    published_utc: str | None  # ISO Z, None when the source has no timestamp
    kind: str = "video"        # video | short


def load_seen(path: Path) -> dict:
    if Path(path).exists():
        return json.loads(Path(path).read_text())
    return {"_version": 1, "videos": {}}


def save_seen(path: Path, seen: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")


def resolve_channel_id(cache_path: Path, *, http_get=None) -> str:
    cache_path = Path(cache_path)
    if cache_path.exists():
        return json.loads(cache_path.read_text())["channel_id"]
    http_get = http_get or (lambda u: requests.get(u, headers={"User-Agent": UA}, timeout=30).text)
    html = http_get(f"https://www.youtube.com/{CHANNEL_HANDLE}")
    m = re.search(r"channel_id=(UC[A-Za-z0-9_-]{20,24})", html) or re.search(r'"channelId":"(UC[A-Za-z0-9_-]{20,24})"', html)
    if not m:
        raise RuntimeError("could not resolve channel id from channel page")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"handle": CHANNEL_HANDLE, "channel_id": m.group(1)}, indent=1) + "\n")
    return m.group(1)


def parse_rss(xml_text: str) -> list[Video]:
    root = ET_.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        title = e.findtext("a:title", namespaces=NS) or ""
        pub = e.findtext("a:published", namespaces=NS)
        if not vid:
            continue
        published = None
        if pub:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            published = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(Video(vid, title, published, "video"))
    return out


def fetch_rss(channel_id: str, *, http_get=None) -> list[Video]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    if http_get is None:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        text = resp.text
    else:
        text = http_get(url)
    return parse_rss(text)


def fetch_listing_ytdlp(channel_id: str, *, per_tab: int = 15, runner=None) -> list[Video]:
    runner = runner or subprocess.run
    out: list[Video] = []
    for tab, kind in (("videos", "video"), ("shorts", "short")):
        cmd = [
            sys.executable, "-m", "yt_dlp", "--flat-playlist", "--playlist-end", str(per_tab),
            "--print", "%(id)s\t%(title)s", f"https://www.youtube.com/channel/{channel_id}/{tab}",
        ]
        try:
            proc = runner(cmd, capture_output=True, text=True, timeout=180)
        except Exception:  # noqa: BLE001
            continue
        if proc.returncode != 0 and not proc.stdout:
            continue
        for line in proc.stdout.splitlines():
            vid, _, title = line.partition("\t")
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                out.append(Video(vid, title, None, kind))
    return out


def list_recent(channel_id: str, *, rss=fetch_rss, fallback=fetch_listing_ytdlp) -> tuple[list[Video], str]:
    """Return (videos, source). Raises RuntimeError if both sources fail."""
    try:
        vids = rss(channel_id)
        if vids:
            return vids, "rss"
    except Exception:  # noqa: BLE001
        pass
    vids = fallback(channel_id)
    if vids:
        return vids, "yt-dlp"
    raise RuntimeError("discovery failed: RSS and yt-dlp listing both returned nothing")


def select_new(videos: list[Video], seen: dict, *, now: datetime, first_run: bool) -> tuple[list[Video], list[Video]]:
    """Split listing into (to_process, to_baseline_skip) using seen_videos.json.

    Unreadable videos are retried for a while because auto-captions often appear hours after upload.
    """
    todo, skipped = [], []
    for v in videos:
        rec = seen["videos"].get(v.video_id)
        if rec is not None:
            if rec.get("status") == "unreadable" and _should_retry(rec, now):
                todo.append(v)
            continue
        if first_run and v.published_utc:
            pub = datetime.fromisoformat(v.published_utc.replace("Z", "+00:00"))
            if now - pub > timedelta(days=BACKFILL_DAYS):
                skipped.append(v)
                continue
        todo.append(v)
    return todo, skipped


def _should_retry(rec: dict, now: datetime) -> bool:
    if rec.get("attempts", 1) >= UNREADABLE_MAX_ATTEMPTS:
        return False
    pub = rec.get("published_utc")
    if pub:
        age = now - datetime.fromisoformat(pub.replace("Z", "+00:00"))
        if age > timedelta(hours=UNREADABLE_RETRY_WINDOW_HOURS):
            return False
    return True
