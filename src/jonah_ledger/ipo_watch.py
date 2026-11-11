"""Phase 2: alert-only IPO calendar watcher. No ranking, no filtering, no trading.

Source choice (DECISIONS.md): Nasdaq's public IPO calendar JSON first; if it is unreachable (cloud IPs are
sometimes blocked) fall back to EDGAR full-text search for 424B4 prospectuses. If neither works the run
logs a skip and exits cleanly. After a deal lists, offer price / day-one open / day-one close are appended to
data/ipo_outcomes.jsonl to extend the offer-to-close sample.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from . import alerts
from .config import IPO_NOTE, PAPER_BANNER, UTC, Paths
from .prices import PriceStore

NASDAQ_URL = "https://api.nasdaq.com/api/ipo/calendar?date={ym}"
EDGAR_URL = "https://efts.sec.gov/LATEST/search-index?q=%22424B4%22&forms=424B4&dateRange=custom&startdt={start}&enddt={end}"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DAY1_WINDOW_DAYS = 4        # first bar must appear within this many days of the pricing date
GIVE_UP_AFTER_DAYS = 12     # stop waiting for first-day bars


def _get_json(url: str, headers: dict) -> dict:
    r = requests.get(url, headers=headers, timeout=25)
    r.raise_for_status()
    return r.json()


def _mdY(s: str | None) -> str | None:
    try:
        return datetime.strptime(s, "%m/%d/%Y").date().isoformat() if s else None
    except ValueError:
        return None


def parse_nasdaq(blob: dict) -> list[dict]:
    """Normalise the Nasdaq calendar JSON into upcoming + priced rows (calendar order, nothing dropped)."""
    out = []
    data = (blob or {}).get("data") or {}
    for kind, table_key, date_key in (("upcoming", "upcomingTable", "expectedPriceDate"), ("priced", "priced", "pricedDate")):
        block = data.get(kind) or {}
        rows = (block.get("rows") if table_key == "priced" else (block.get(table_key) or {}).get("rows")) or []
        for r in rows:
            out.append({
                "key": r.get("dealID") or f"{r.get('proposedTickerSymbol')}:{r.get(date_key)}", "kind": kind,
                "symbol": (r.get("proposedTickerSymbol") or "").strip(), "company": r.get("companyName") or "",
                "exchange": r.get("proposedExchange") or "", "price_text": r.get("proposedSharePrice") or "",
                "shares": r.get("sharesOffered") or "", "date": _mdY(r.get(date_key)), "source": "nasdaq",
            })
    return out


def parse_edgar(blob: dict) -> list[dict]:
    out = []
    for h in ((blob or {}).get("hits") or {}).get("hits", []):
        s = h.get("_source", {})
        name = (s.get("display_names") or [""])[0]
        m = re.search(r"\(([A-Z][A-Z.\-]{0,5})\)\s+\(CIK", name)
        out.append({"key": h.get("_id", name), "kind": "priced", "symbol": m.group(1) if m else "",
                    "company": re.sub(r"\s+\(.*", "", name).strip(), "exchange": "", "price_text": "",
                    "shares": "", "date": s.get("file_date"), "source": "edgar-424B4"})
    return out


def fetch_deals(today: date, *, get_json=_get_json) -> tuple[list[dict], str]:
    """Return (deals, source). Raises RuntimeError when neither source is reachable."""
    deals: list[dict] = []
    try:
        for ym in sorted({today.strftime("%Y-%m"), (today + timedelta(days=20)).strftime("%Y-%m")}):
            deals += parse_nasdaq(get_json(NASDAQ_URL.format(ym=ym), {"User-Agent": BROWSER_UA, "Accept": "application/json"}))
        return deals, "nasdaq"
    except Exception as exc:  # noqa: BLE001
        print(f"[ipo] Nasdaq calendar unavailable ({type(exc).__name__}); trying EDGAR 424B4")
    try:
        ua = os.environ.get("SEC_USER_AGENT") or "jonah-ledger research bot"
        blob = get_json(EDGAR_URL.format(start=(today - timedelta(days=14)).isoformat(), end=today.isoformat()), {"User-Agent": ua})
        return parse_edgar(blob), "edgar"
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"no IPO source reachable: {type(exc).__name__}") from exc


def _offer_price(price_text: str) -> float | None:
    nums = re.findall(r"\d+(?:\.\d+)?", price_text or "")
    return float(nums[0]) if len(nums) == 1 else None   # a range is not an offer price


def deal_line(d: dict) -> str:
    return f"**{d['symbol'] or '?'}** {d['company']} · {d['exchange'] or d['source']} · {d['price_text'] or 'price n/a'} · {d['date'] or 'date n/a'}"


def deal_embeds(deals: list[dict], title: str) -> list[dict]:
    if not deals:
        return []
    out = []
    for i in range(0, len(deals), 15):
        lines = [deal_line(d) for d in deals[i:i + 15]]
        out.append(alerts.make_embed(title, "\n".join(lines) + f"\n\n{IPO_NOTE}", color=alerts.BLUE,
                                     footer=f"{PAPER_BANNER} · listed in calendar order, not ranked"))
    return out


def log_outcomes(paths: Paths, state: dict, today: date, store: PriceStore) -> int:
    """Append offer/day-one-open/day-one-close for deals that have started trading. Returns rows appended."""
    done = set(state.setdefault("outcome_done", []))
    n = 0
    for key, d in list(state.get("priced", {}).items()):
        if key in done or not d.get("date") or not d.get("symbol"):
            continue
        priced = date.fromisoformat(d["date"])
        if today <= priced:
            continue
        offer = _offer_price(d.get("price_text", ""))
        if offer is None:
            done.add(key)   # EDGAR-only or ranged: no clean offer price to measure against
            continue
        frame = store.get(d["symbol"], refresh=True)
        bar = None
        if frame is not None:
            after = frame[frame.index >= str(priced)]
            if len(after) and (after.index[0].date() - priced).days <= DAY1_WINDOW_DAYS:
                bar = after.iloc[0]
        if bar is None:
            if (today - priced).days > GIVE_UP_AFTER_DAYS:
                done.add(key)
            continue
        row = {"key": key, "symbol": d["symbol"], "company": d["company"], "priced_date": d["date"],
               "day1_date": after.index[0].date().isoformat(), "offer_price": offer,
               "day1_open": round(float(bar["open"]), 4), "day1_close": round(float(bar["close"]), 4),
               "offer_to_open": round(float(bar["open"]) / offer - 1, 6), "offer_to_close": round(float(bar["close"]) / offer - 1, 6),
               "logged_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        paths.ipo_outcomes.parent.mkdir(parents=True, exist_ok=True)
        with paths.ipo_outcomes.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        done.add(key)
        n += 1
    state["outcome_done"] = sorted(done)
    return n


def run_ipo_watch(paths: Paths, *, today: date | None = None, get_json=_get_json, store: PriceStore | None = None,
                  send_alerts: bool = True) -> dict:
    today = today or datetime.now(UTC).date()
    try:
        deals, source = fetch_deals(today, get_json=get_json)
    except RuntimeError as exc:
        print(f"[ipo] skipped: {exc}")
        return {"skipped": True, "reason": str(exc)}
    state = json.loads(paths.ipo_state.read_text()) if paths.ipo_state.exists() else {"upcoming": {}, "priced": {}, "alerted": []}
    alerted = set(state.setdefault("alerted", []))
    new_up = [d for d in deals if d["kind"] == "upcoming" and f"up:{d['key']}" not in alerted]
    new_pr = [d for d in deals if d["kind"] == "priced" and f"pr:{d['key']}" not in alerted]
    for d in deals:
        state.setdefault(d["kind"], {})[d["key"]] = d
    if send_alerts:
        alerts.send_embeds(deal_embeds(new_up, "Upcoming IPOs") + deal_embeds(new_pr, "Priced IPOs"))
    alerted |= {f"up:{d['key']}" for d in new_up} | {f"pr:{d['key']}" for d in new_pr}
    state["alerted"] = sorted(alerted)[-3000:]
    logged = log_outcomes(paths, state, today, store or PriceStore(paths.prices_cache))
    paths.ipo_state.parent.mkdir(parents=True, exist_ok=True)
    paths.ipo_state.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    return {"skipped": False, "source": source, "new_upcoming": len(new_up), "new_priced": len(new_pr), "outcomes_logged": logged}
