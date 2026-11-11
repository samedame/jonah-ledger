"""Discord webhook alerts. Embeds are clipped to Discord's limits; JONAH_ALERTS=off silences everything."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .config import HEARTBEAT_STALE_HOURS, PAPER_BANNER, UTC, alerts_enabled, webhook_url

# Discord limits
L_TITLE, L_DESC, L_FIELD_NAME, L_FIELD_VALUE, L_FIELDS, L_FOOTER = 256, 4096, 256, 1024, 25, 2048
L_EMBED_TOTAL, L_EMBEDS_PER_MSG = 6000, 10

GREEN, RED, AMBER, BLUE, GREY = 0x2ECC71, 0xE74C3C, 0xF1C40F, 0x3498DB, 0x95A5A6


def clip(s: Any, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def make_embed(title: str, description: str = "", *, fields: list[tuple[str, str, bool]] | None = None,
               color: int = BLUE, footer: str = PAPER_BANNER, url: str | None = None) -> dict:
    e: dict[str, Any] = {"title": clip(title, L_TITLE), "color": color, "footer": {"text": clip(footer, L_FOOTER)}}
    if description:
        e["description"] = clip(description, L_DESC)
    if url:
        e["url"] = url
    if fields:
        e["fields"] = [{"name": clip(n or "​", L_FIELD_NAME), "value": clip(v or "​", L_FIELD_VALUE), "inline": bool(i)}
                       for n, v, i in fields[:L_FIELDS]]
    return fit_total(e)


def embed_size(e: dict) -> int:
    n = len(e.get("title", "")) + len(e.get("description", "")) + len(e.get("footer", {}).get("text", ""))
    for f in e.get("fields", []):
        n += len(f["name"]) + len(f["value"])
    return n


def fit_total(e: dict) -> dict:
    """Shrink the description, then drop trailing fields, until the embed fits the 6000-char total."""
    while embed_size(e) > L_EMBED_TOTAL and e.get("description") and len(e["description"]) > 200:
        e["description"] = clip(e["description"], max(200, len(e["description"]) - (embed_size(e) - L_EMBED_TOTAL) - 1))
    while embed_size(e) > L_EMBED_TOTAL and e.get("fields"):
        e["fields"].pop()
    return e


def chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Group embeds into messages of <=10 embeds and <=6000 total characters."""
    msgs: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for e in embeds:
        s = embed_size(e)
        if cur and (len(cur) >= L_EMBEDS_PER_MSG or size + s > L_EMBED_TOTAL):
            msgs.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += s
    if cur:
        msgs.append(cur)
    return msgs


def send_embeds(embeds: list[dict], *, session=requests, sleep=time.sleep) -> int:
    """Post embeds. Returns number of messages sent. Silent (but logged) when alerts are off/unconfigured."""
    if not embeds:
        return 0
    if not alerts_enabled():
        print(f"[alerts] JONAH_ALERTS=off: suppressed {len(embeds)} embed(s)")
        return 0
    url = webhook_url()
    if not url:
        print(f"[alerts] DISCORD_WEBHOOK_URL not set: suppressed {len(embeds)} embed(s)")
        return 0
    sent = 0
    for msg in chunk_embeds(embeds):
        body = {"username": "jonah-ledger", "embeds": msg, "allowed_mentions": {"parse": []}}
        for attempt in range(3):
            resp = session.post(url, json=body, timeout=20)
            if resp.status_code == 429:
                sleep(min(float(resp.json().get("retry_after", 2)), 30))
                continue
            if resp.status_code >= 300:
                print(f"[alerts] Discord returned {resp.status_code}")
            else:
                sent += 1
            break
    return sent


# ---- state (dedupe + heartbeat) -------------------------------------------------------------
class AlertState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"sent": [], "last_stale_warning_utc": None}

    def seen(self, key: str) -> bool:
        return key in self.data["sent"]

    def mark(self, key: str) -> None:
        if key not in self.data["sent"]:
            self.data["sent"].append(key)
            self.data["sent"] = self.data["sent"][-5000:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1) + "\n")


def write_heartbeat(path: Path, now: datetime, source: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({"last_success_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": source}, indent=1) + "\n")


def heartbeat_age_hours(path: Path, now: datetime) -> float | None:
    p = Path(path)
    if not p.exists():
        return None
    last = datetime.fromisoformat(json.loads(p.read_text())["last_success_utc"].replace("Z", "+00:00"))
    return (now - last).total_seconds() / 3600


# ---- builders ---------------------------------------------------------------------------------
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1%}"


def new_call_embed(row: dict, *, vs_high: float | None, late: bool = False) -> dict:
    fields = [
        ("Strength", row["strength"], True),
        ("Direction", row["direction"], True),
        ("Planned entry", "Next open: first session open after the video's US/Eastern date", False),
        ("Price vs 252-day high", "n/a" if vs_high is None else f"{vs_high:+.1%} (latest close)", True),
    ]
    if row.get("target"):
        fields.append(("Stated target", f"${row['target']:g}" + (f" by {row['deadline']}" if row.get("deadline") else ""), True))
    if late:
        fields.append(("Note", "Extracted after the planned entry open; flagged late in the ledger.", False))
    fields.append(("Video", f"https://youtu.be/{row['video_id']}", False))
    color = RED if row["direction"] == "short" else GREEN if row["strength"] in ("buying", "wants") else GREY
    return make_embed(f"New call: {row['ticker']}", f"“{row['quote']}”\n\n**{PAPER_BANNER}**", fields=fields, color=color)


def target_embed(row: dict, status: str, hit_date: str | None) -> dict:
    ok = status == "hit"
    desc = (f"Target ${row['target']:g} was hit" + (f" on {hit_date}" if hit_date else "")) if ok else \
        f"Deadline {row['deadline']} passed without reaching target ${row['target']:g}."
    return make_embed(f"Price target {status}: {row['ticker']}", desc, color=GREEN if ok else AMBER,
                      fields=[("Call", f"{row['strength']} · video https://youtu.be/{row['video_id']}", False)])


def stale_embed(age_hours: float | None) -> dict:
    when = "never" if age_hours is None else f"{age_hours:.0f} hours ago"
    return make_embed("Stale bot warning", f"No successful discovery run in over {HEARTBEAT_STALE_HOURS} hours (last: {when}). "
                      "Check GitHub Actions, or run scripts/local_run.sh if YouTube is blocking runners.", color=RED)


def digest_embed(summary_md: str, gate_message: str, head_hash: str, verify_ok: bool) -> dict:
    return make_embed(
        "Weekly digest", summary_md, color=BLUE,
        fields=[("Gate", gate_message, False), ("Ledger", f"{'verified' if verify_ok else 'VERIFY FAILED'} · head {head_hash[:16]}", False)],
    )


def maybe_stale_alert(heartbeat_path: Path, state: AlertState, now: datetime, *, send=send_embeds) -> bool:
    age = heartbeat_age_hours(heartbeat_path, now)
    if age is not None and age <= HEARTBEAT_STALE_HOURS:
        return False
    last = state.data.get("last_stale_warning_utc")
    if last and (now - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() < 24 * 3600:
        return False
    send([stale_embed(age)])
    state.data["last_stale_warning_utc"] = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.save()
    return True
