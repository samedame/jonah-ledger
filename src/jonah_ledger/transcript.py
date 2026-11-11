"""Fetch auto-captions with yt-dlp into a temp dir. Transcripts are never committed or kept."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import UTC


@dataclass
class TranscriptResult:
    video_id: str
    ok: bool
    text: str = ""
    published_utc: str | None = None
    reason: str = ""          # why unreadable
    live_status: str | None = None


_BLOCK_HINTS = ("sign in to confirm", "not a bot", "http error 429", "http error 403", "too many requests")


def parse_json3(blob: dict) -> str:
    """Join caption segments into plain text; drop newline-only segments."""
    parts: list[str] = []
    for ev in blob.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        s = "".join(seg.get("utf8", "") for seg in segs).replace("\n", " ").strip()
        if s:
            parts.append(s)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _ts_to_iso(ts) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_transcript(video_id: str, *, runner=None, workdir: Path | None = None) -> TranscriptResult:
    """Download auto-subs + metadata into a temp dir, parse, delete. Never raises for fetch problems."""
    runner = runner or subprocess.run
    tmp = tempfile.TemporaryDirectory(prefix="jonah-tr-") if workdir is None else None
    wd = Path(tmp.name) if tmp else Path(workdir)
    try:
        cmd = [
            sys.executable, "-m", "yt_dlp", "--skip-download", "--write-auto-subs", "--sub-langs", "en.*,en",
            "--sub-format", "json3", "--write-info-json", "--no-playlist", "-o", str(wd / "%(id)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            proc = runner(cmd, capture_output=True, text=True, timeout=240)
        except Exception as exc:  # noqa: BLE001
            return TranscriptResult(video_id, False, reason=f"yt-dlp failed to run: {exc}")
        info_path = wd / f"{video_id}.info.json"
        info = json.loads(info_path.read_text()) if info_path.exists() else {}
        published = _ts_to_iso(info.get("timestamp") or info.get("release_timestamp"))
        live = info.get("live_status")
        if live in {"is_upcoming", "is_live"}:
            return TranscriptResult(video_id, False, published_utc=published, live_status=live, reason=f"live_status={live}")
        subs = sorted(wd.glob(f"{video_id}*.json3"))
        if not subs:
            stderr = (getattr(proc, "stderr", "") or "").lower()
            if any(h in stderr for h in _BLOCK_HINTS):
                reason = "blocked"
            elif proc.returncode != 0:
                reason = "yt-dlp error"
            else:
                reason = "no auto-captions"
            return TranscriptResult(video_id, False, published_utc=published, live_status=live, reason=reason)
        subs.sort(key=lambda p: (".en-orig." in p.name, p.name))  # prefer plain 'en'
        text = parse_json3(json.loads(subs[0].read_text()))
        if len(text.split()) < 20:
            return TranscriptResult(video_id, False, published_utc=published, live_status=live, reason="caption text empty/too short")
        return TranscriptResult(video_id, True, text=text, published_utc=published, live_status=live)
    finally:
        if tmp:
            tmp.cleanup()
