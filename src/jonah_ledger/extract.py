"""Claude-based call extraction with strict validation.

The model only *proposes*. Everything is re-validated in code: tickers must resolve against the
Nasdaq Trader list (otherwise dropped and counted), quotes are cut to 15 words and must be grounded
in the transcript, there is one row per ticker per video, and labels must be one of four values.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .config import STRENGTHS, model_name
from .symbols import looks_like_ticker, normalize_ticker

EXTRACTOR_SEMVER = "1.0.0"
MAX_QUOTE_WORDS = 15
CHUNK_CHARS = 120_000
GROUNDING_MIN_FRACTION = 0.6
# When one ticker appears with several labels in the same video, keep the strongest.
PRECEDENCE = {"buying": 0, "wants": 1, "short": 2, "mention": 3}

SYSTEM_PROMPT = """You extract dated stock calls from the auto-generated transcript of ONE YouTube video by a retail investor.
Return them by calling the record_calls tool. Be conservative: a missing call is better than an invented one.

A call is a statement made in this video that (a) names a specific US-listed ticker or a company you can map to its ticker with certainty, and (b) is exactly one of:
- "buying": he says he is buying, bought, or just added the stock (a position he actually took).
- "wants": he says he wants to buy, would add, is adding on a dip, or plans to buy.
- "short": he says he is shorting it or betting it falls.
- "mention": bullish or bearish talk about the stock with no position stated.
Direction is "long" for bullish content and "short" for bearish. buying and wants are always long; short is always short; a mention takes the direction of what he says.

NOT calls (omit them): options fills, win-rate claims, profit-and-loss brags, past trades recounted as history without a present stance, index or ETF talk unless he names a specific ticker as a position, private companies, crypto, and tickers you cannot resolve. Auto-captions garble tickers (e.g. "in video" for NVDA): resolve from context only when certain, otherwise omit.

Rules:
- One entry per ticker per video. If he says several things about a ticker, report the strongest: buying > wants > short > mention.
- "quote": a verbatim excerpt from the transcript of at most 15 words that supports the label. Do not paraphrase.
- "target": a price target in dollars only if he states one for that ticker, else null.
- "deadline": an ISO date YYYY-MM-DD only if he states a deadline or date for the target, resolved against the video date given in the user message, else null.
- Do not infer anything he did not say. If the transcript has no calls, return an empty list.
- Always answer by calling record_calls exactly once, with no other text.
"""

TOOL: dict[str, Any] = {
    "name": "record_calls",
    "description": "Record every qualifying stock call found in the transcript (possibly none).",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["calls"],
        "properties": {
            "calls": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["ticker", "strength", "direction", "target", "deadline", "quote"],
                    "properties": {
                        "ticker": {"type": "string", "description": "US ticker symbol, e.g. NVDA"},
                        "strength": {"type": "string", "enum": list(STRENGTHS)},
                        "direction": {"type": "string", "enum": ["long", "short"]},
                        "target": {"type": ["number", "null"]},
                        "deadline": {"type": ["string", "null"]},
                        "quote": {"type": "string"},
                    },
                },
            }
        },
    },
}


def prompt_hash() -> str:
    return hashlib.sha256((SYSTEM_PROMPT + json.dumps(TOOL, sort_keys=True)).encode()).hexdigest()


def extractor_version() -> str:
    return f"{EXTRACTOR_SEMVER}+{prompt_hash()[:12]}"


@dataclass
class ExtractionResult:
    calls: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    model: str = ""


def _bump(stats: dict[str, int], key: str, n: int = 1) -> None:
    stats[key] = stats.get(key, 0) + n


def _norm_words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower())


def _grounded(quote: str, transcript_words: set[str]) -> bool:
    qw = _norm_words(quote)
    if not qw:
        return False
    return sum(1 for w in qw if w in transcript_words) / len(qw) >= GROUNDING_MIN_FRACTION


def validate_calls(
    payload: Any, *, symbols: dict[str, dict], video_date: date, transcript: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate the tool payload. Returns (clean calls, stats). Never raises on bad model output."""
    stats: dict[str, int] = {}
    if not isinstance(payload, dict) or not isinstance(payload.get("calls"), list):
        _bump(stats, "dropped_bad_payload")
        return [], stats
    twords = set(_norm_words(transcript)) if transcript else None
    best: dict[str, dict[str, Any]] = {}
    for raw in payload["calls"]:
        if not isinstance(raw, dict):
            _bump(stats, "dropped_invalid")
            continue
        strength = raw.get("strength")
        tick = raw.get("ticker")
        quote = raw.get("quote")
        if strength not in STRENGTHS or not isinstance(tick, str) or not tick.strip() \
                or not isinstance(quote, str) or not quote.strip():
            _bump(stats, "dropped_invalid")
            continue
        tick = normalize_ticker(tick)
        if not looks_like_ticker(tick) or tick not in symbols:
            _bump(stats, "dropped_unresolved_ticker")
            continue
        direction = raw.get("direction")
        if strength in ("buying", "wants"):
            direction = "long"
        elif strength == "short":
            direction = "short"
        elif direction not in ("long", "short"):
            _bump(stats, "dropped_invalid")
            continue
        if twords is not None and not _grounded(quote, twords):
            _bump(stats, "dropped_ungrounded_quote")
            continue
        words = quote.split()
        if len(words) > MAX_QUOTE_WORDS:
            quote = " ".join(words[:MAX_QUOTE_WORDS])
            _bump(stats, "quotes_truncated")
        target = raw.get("target")
        if isinstance(target, bool) or not isinstance(target, (int, float)) or target <= 0:
            target = None
        else:
            target = float(target)
        deadline = raw.get("deadline")
        if deadline is not None:
            try:
                d = date.fromisoformat(deadline)
                deadline = d.isoformat() if d >= video_date else None
            except (TypeError, ValueError):
                deadline = None
            if deadline is None:
                _bump(stats, "deadlines_discarded")
        if target is None and deadline is not None:
            deadline = None  # a deadline with no target has nothing to attach to
        row = {"ticker": tick, "strength": strength, "direction": direction,
               "target": target, "deadline": deadline, "quote": quote.strip()}
        prev = best.get(tick)
        if prev is None:
            best[tick] = row
        else:
            _bump(stats, "merged_duplicate_ticker")
            if PRECEDENCE[strength] < PRECEDENCE[prev["strength"]]:
                best[tick] = row
    return list(best.values()), stats


def _chunks(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, cur, size = [], [], 0
    for w in text.split():
        cur.append(w)
        size += len(w) + 1
        if size >= CHUNK_CHARS:
            out.append(" ".join(cur))
            cur, size = [], 0
    if cur:
        out.append(" ".join(cur))
    return out


def _call_model(client, transcript: str, title: str, video_date: date, model: str) -> dict[str, Any]:
    user = (
        f"Video title: {title}\nVideo date (US/Eastern calendar date): {video_date.isoformat()}\n\n"
        f"Transcript (auto-generated, may be garbled):\n{transcript}"
    )
    kwargs: dict[str, Any] = dict(
        model=model, max_tokens=4096, system=SYSTEM_PROMPT, tools=[TOOL], temperature=0,
        tool_choice={"type": "tool", "name": "record_calls"},
        messages=[{"role": "user", "content": user}],
    )
    # Some models reject temperature or a forced tool_choice. Drop only the parameter the API names and retry;
    # the system prompt asks for the tool call either way and validate_calls re-checks everything.
    for _ in range(3):
        try:
            resp = client.messages.create(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "tool_choice" in msg and kwargs.get("tool_choice", {}).get("type") != "auto":
                kwargs["tool_choice"] = {"type": "auto"}
            elif "temperature" in msg and "temperature" in kwargs:
                del kwargs["temperature"]
            else:
                raise
    else:
        raise RuntimeError("model call failed after dropping unsupported parameters")
    texts = []
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
        if getattr(block, "type", None) == "text":
            texts.append(getattr(block, "text", ""))
    # No tool call: accept a bare JSON object in the text, otherwise flag it (never silently read as "no calls").
    m = re.search(r"\{.*\}", "\n".join(texts), re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"_unparseable": True}


def extract_calls(
    transcript: str, *, title: str, published_utc: str, symbols: dict[str, dict], client=None, model: str | None = None,
) -> ExtractionResult:
    from .dates import et_date

    model = model or model_name()
    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    vdate = et_date(published_utc)
    merged: dict[str, dict[str, Any]] = {}
    stats: dict[str, int] = {}
    for chunk in _chunks(transcript):
        payload = _call_model(client, chunk, title, vdate, model)
        if isinstance(payload, dict) and payload.get("_unparseable"):
            raise RuntimeError("model returned neither a tool call nor parseable JSON")   # video stays unseen, retried next poll
        calls, st = validate_calls(payload, symbols=symbols, video_date=vdate, transcript=chunk)
        for k, v in st.items():
            _bump(stats, k, v)
        for c in calls:
            prev = merged.get(c["ticker"])
            if prev is None or PRECEDENCE[c["strength"]] < PRECEDENCE[prev["strength"]]:
                merged[c["ticker"]] = c
    return ExtractionResult(list(merged.values()), stats, model)


def to_ledger_fields(call: dict[str, Any], *, video_id: str, published_utc: str, model: str, extracted_at: str) -> dict[str, Any]:
    return {
        "video_id": video_id, "published_utc": published_utc, "ticker": call["ticker"],
        "direction": call["direction"], "strength": call["strength"], "target": call["target"],
        "deadline": call["deadline"], "quote": call["quote"],
        "extractor_version": extractor_version(), "prompt_hash": prompt_hash(), "model": model,
        "extracted_at": extracted_at,
    }
