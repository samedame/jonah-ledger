"""Constants, paths and environment switches. No broker code, no credentials for brokers, ever."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

CHANNEL_HANDLE = "@carsandinvesting"

# Backtest conventions (encoded, not re-argued). See DECISIONS.md for the ambiguous parts.
HORIZONS = (1, 5, 20, 60, 120, 250)
BENCHMARK = "SPY"
COST_BPS = 10.0            # per trade (one call = one trade, see DECISIONS.md)
COST_BPS_LOW_PRICE = 30.0  # if the entry price is under LOW_PRICE_THRESHOLD
LOW_PRICE_THRESHOLD = 5.0
# True: the entry session is session 1, so the "1st session close" is the entry-day close.
ENTRY_DAY_IS_SESSION_1 = True

STRENGTHS = ("buying", "wants", "short", "mention")
LONG_STRENGTHS = ("buying", "wants")
RETRACTED = "retracted"  # only valid on a correction row that carries `supersedes`

# Gate
GATE_MIN_RESOLVED = 30
GATE_HORIZON = 60
GATE_DROP_BEST = 2

# Discovery / transcript behaviour
BACKFILL_DAYS = 3          # on the very first run only videos newer than this are processed
UNREADABLE_MAX_ATTEMPTS = 8
UNREADABLE_RETRY_WINDOW_HOURS = 72
HEARTBEAT_STALE_HOURS = 48

PAPER_BANNER = "PAPER ONLY, not a signal"
IPO_NOTE = (
    "Request IPO Access shares manually. Selling within 30 days counts as flipping "
    "and may block IPO Access for 60 days."
)

DEFAULT_MODEL = "claude-sonnet-5-5"


def model_name() -> str:
    return os.environ.get("JONAH_MODEL") or DEFAULT_MODEL  # empty string (unset Actions var) -> default


def alerts_enabled() -> bool:
    return os.environ.get("JONAH_ALERTS", "on").strip().lower() not in {"off", "0", "false", "no"}


def webhook_url() -> str | None:
    return os.environ.get("DISCORD_WEBHOOK_URL") or None


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def ledger(self) -> Path:
        return self.data / "calls.forward.jsonl"

    @property
    def seen(self) -> Path:
        return self.data / "seen_videos.json"

    @property
    def channel(self) -> Path:
        return self.data / "channel.json"

    @property
    def heartbeat(self) -> Path:
        return self.data / "heartbeat.json"

    @property
    def alerts_state(self) -> Path:
        return self.data / "alerts_state.json"

    @property
    def outcomes(self) -> Path:
        return self.data / "outcomes.csv"

    @property
    def prices_cache(self) -> Path:
        return self.data / "prices_cache"

    @property
    def seed(self) -> Path:
        return self.data / "seed"

    @property
    def ipo_state(self) -> Path:
        return self.data / "ipo_state.json"

    @property
    def ipo_outcomes(self) -> Path:
        return self.data / "ipo_outcomes.jsonl"

    @property
    def weekly(self) -> Path:
        return self.root / "reports" / "weekly"


def default_root() -> Path:
    env = os.environ.get("JONAH_LEDGER_ROOT")
    if env:
        return Path(env).resolve()
    # src/jonah_ledger/config.py -> repo root (editable install)
    return Path(__file__).resolve().parents[2]


def get_paths() -> Paths:
    return Paths(default_root())
