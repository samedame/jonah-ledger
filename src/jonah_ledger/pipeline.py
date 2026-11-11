"""The poll pipeline: discover -> transcript -> extract -> ledger -> alerts."""
from __future__ import annotations

from datetime import datetime

from . import alerts, discover, extract
from .config import UTC, Paths, model_name
from .dates import approx_next_session_open_utc, parse_utc
from .ledger import DuplicateCall, Ledger
from .prices import PriceStore, price_vs_high
from .symbols import load_symbols
from .transcript import fetch_transcript


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_poll(
    paths: Paths, *, now: datetime | None = None, client=None, symbols=None, store: PriceStore | None = None,
    transcript_fn=fetch_transcript, list_fn=None, max_videos: int | None = None, send_alerts: bool = True,
) -> dict:
    now = now or datetime.now(UTC)
    ledger = Ledger(paths.ledger)
    seen = discover.load_seen(paths.seen)
    first_run = not seen["videos"] and not ledger.rows()
    cid = discover.resolve_channel_id(paths.channel)
    videos, source = (list_fn or discover.list_recent)(cid)
    alerts.write_heartbeat(paths.heartbeat, now, source)   # discovery succeeded
    todo, baseline = discover.select_new(videos, seen, now=now, first_run=first_run)
    for v in baseline:
        seen["videos"][v.video_id] = {"status": "baseline_skipped", "published_utc": v.published_utc, "title": v.title}
    discover.save_seen(paths.seen, seen)
    summary = {"source": source, "listed": len(videos), "to_process": len(todo), "extracted": 0, "no_calls": 0,
               "unreadable": 0, "new_calls": 0, "errors": 0, "blocked": 0}
    if not todo:
        return summary
    symbols = symbols if symbols is not None else load_symbols(paths.prices_cache)
    store = store or PriceStore(paths.prices_cache)
    state = alerts.AlertState(paths.alerts_state)
    model = model_name()
    for v in todo[: max_videos or len(todo)]:
        prev = seen["videos"].get(v.video_id, {})
        if ledger.has_video(v.video_id):   # crash recovery: calls already logged
            seen["videos"][v.video_id] = {**prev, "status": "extracted", "published_utc": v.published_utc or prev.get("published_utc"), "title": v.title}
            continue
        tr = transcript_fn(v.video_id)
        published = v.published_utc or tr.published_utc
        if not tr.ok or not published:
            reason = tr.reason or "no publish timestamp"
            if tr.live_status in ("is_upcoming", "is_live"):
                continue   # not a finished video yet; look again next run
            seen["videos"][v.video_id] = {"status": "unreadable", "published_utc": published, "title": v.title,
                                          "attempts": prev.get("attempts", 0) + 1, "reason": reason}
            summary["unreadable"] += 1
            summary["blocked"] += reason == "blocked"
            discover.save_seen(paths.seen, seen)
            continue
        try:
            res = extract.extract_calls(tr.text, title=v.title, published_utc=published, symbols=symbols, client=client, model=model)
        except Exception as exc:  # noqa: BLE001 - API trouble: leave the video unseen so the next run retries
            print(f"[poll] extraction failed for {v.video_id}: {type(exc).__name__}: {exc}")
            summary["errors"] += 1
            continue
        extracted_at = _iso(now)
        late = now >= approx_next_session_open_utc(published)
        batch = []
        for c in res.calls:
            f = extract.to_ledger_fields(c, video_id=v.video_id, published_utc=published, model=res.model, extracted_at=extracted_at)
            f["late_extraction"] = late
            batch.append(f)
        try:
            rows = ledger.append_many(batch)
        except DuplicateCall:
            rows = []
        seen["videos"][v.video_id] = {
            "status": "extracted" if rows else "no_calls", "published_utc": published, "title": v.title,
            "n_calls": len(rows), "dropped": res.stats, "kind": v.kind,
        }
        discover.save_seen(paths.seen, seen)
        summary["extracted" if rows else "no_calls"] += 1
        summary["new_calls"] += len(rows)
        if send_alerts and rows:
            embeds, keys = [], []
            for r in rows:
                key = f"new:{r['call_id']}"
                if state.seen(key):
                    continue
                try:
                    vs_high = price_vs_high(store.get(r["ticker"]))
                except Exception:  # noqa: BLE001
                    vs_high = None
                embeds.append(alerts.new_call_embed(r, vs_high=vs_high, late=late))
                keys.append(key)
            alerts.send_embeds(embeds)
            for k in keys:
                state.mark(k)
            state.save()
    if send_alerts and summary["blocked"] >= 3:
        key = f"blocked:{now.date().isoformat()}"
        if not state.seen(key):
            alerts.send_embeds([alerts.make_embed(
                "Transcript fetches look blocked",
                f"{summary['blocked']} videos could not be read because YouTube blocked the request. "
                "Run scripts/local_run.sh from your own computer.", color=alerts.AMBER)])
            state.mark(key)
            state.save()
    return summary


def run_heartbeat(paths: Paths, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    state = alerts.AlertState(paths.alerts_state)
    return alerts.maybe_stale_alert(paths.heartbeat, state, now)


__all__ = ["run_poll", "run_heartbeat", "parse_utc"]
