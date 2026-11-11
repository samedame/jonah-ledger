"""Command line entry point: jonah-ledger <command>. Paper only; no orders, no broker code."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime

from . import discover, extract, ipo_watch, pipeline, score, seed_check
from .config import UTC, get_paths
from .ledger import Ledger
from .prices import PriceStore
from .symbols import load_symbols
from .transcript import fetch_transcript


def _kv(items):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v.strip()
    return out


def cmd_ledger(args, paths) -> int:
    led = Ledger(paths.ledger)
    if args.action == "verify":
        r = led.verify()
        if r.ok:
            print(f"OK: {r.rows} rows, head {r.head}")
            return 0
        print(f"FAILED: {len(r.errors)} problem(s)")
        for e in r.errors[:20]:
            print("  -", e)
        return 1
    if args.action == "head":
        print(led.head())
        return 0
    for r in led.rows()[-args.n:]:
        print(f"{r['published_utc']} {r['ticker']:<6} {r['strength']:<8} {r['direction']:<5} {r['quote']}")
    return 0


def cmd_dry_run(args, paths) -> int:
    """Print what the extractor would log for the N most recent videos. Writes nothing to the ledger."""
    use_llm = not args.no_llm
    if use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set: running the transcript stage only (no extraction).\n")
        use_llm = False
    cid = discover.resolve_channel_id(paths.channel)
    vids, source = discover.list_recent(cid)
    print(f"listing source: {source}; model: {extract.model_name() if hasattr(extract, 'model_name') else ''} ; extractor {extract.extractor_version()}")
    symbols = load_symbols(paths.prices_cache) if use_llm else {}
    for v in vids[: args.n]:
        tr = fetch_transcript(v.video_id)
        pub = v.published_utc or tr.published_utc
        words = len(tr.text.split()) if tr.ok else 0
        print(f"\n== {v.video_id} | {pub} | {v.kind} | {v.title}")
        if not tr.ok:
            print(f"   UNREADABLE: {tr.reason}")
            continue
        print(f"   transcript: {words} words (deleted after use)")
        if not use_llm or not pub:
            continue
        res = extract.extract_calls(tr.text, title=v.title, published_utc=pub, symbols=symbols)
        print(f"   calls: {len(res.calls)}   validation stats: {res.stats or '{}'}")
        for c in res.calls:
            tgt = f" target={c['target']}" if c["target"] else ""
            print(f"   - {c['ticker']:<6} {c['strength']:<8} {c['direction']:<5}{tgt}  \"{c['quote']}\"")
    return 0


def cmd_seed_check(args, paths) -> int:
    store = PriceStore(paths.prices_cache)
    asof = date.fromisoformat(args.asof) if args.asof else datetime.now(UTC).date()
    rc = 0
    cp = paths.seed / "calls_scored.csv" if not args.calls else __import__("pathlib").Path(args.calls)
    fp = paths.seed / "forward_ledger.csv" if not args.forward else __import__("pathlib").Path(args.forward)
    for name, p, fn in (("calls_scored", cp, lambda df: seed_check.rescore_calls(df, store, colmap=_kv(args.map))),
                        ("forward_ledger", fp, lambda df: seed_check.check_forward(df, store, asof, colmap=_kv(args.map)))):
        if not p.exists():
            print(f"[{name}] NOT VERIFIED: {p} not found")
            rc = 2
            continue
        res = fn(seed_check.load_csv(p))
        print(f"[{name}] {'PASS' if res['passed'] else 'FAIL'}")
        print(json.dumps(res, indent=1, default=str))
        rc = rc or (0 if res["passed"] else 1)
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jonah-ledger", description="Paper-only forward ledger. Research tool, not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poll", help="discover -> transcript -> extract -> ledger -> alerts")
    p.add_argument("--no-alerts", action="store_true")
    p.add_argument("--max-videos", type=int)
    for name in ("score", "weekly", "ipo-watch"):
        sub.add_parser(name).add_argument("--no-alerts", action="store_true")
    sub.add_parser("heartbeat", help="post a stale-bot warning if discovery has not succeeded for 48h")
    p = sub.add_parser("ledger")
    p.add_argument("action", choices=["verify", "head", "show"])
    p.add_argument("-n", type=int, default=10)
    p = sub.add_parser("dry-run", help="show extraction for the N most recent videos; writes nothing")
    p.add_argument("-n", type=int, default=5)
    p.add_argument("--no-llm", action="store_true")
    p = sub.add_parser("seed-check", help="acceptance checks against data/seed/*.csv")
    p.add_argument("--calls")
    p.add_argument("--forward")
    p.add_argument("--asof")
    p.add_argument("--map", action="append", help="override a guessed column, e.g. --map excess_60=exc_spy_60")
    args = ap.parse_args(argv)
    paths = get_paths()

    if args.cmd == "ledger":
        return cmd_ledger(args, paths)
    if args.cmd == "dry-run":
        return cmd_dry_run(args, paths)
    if args.cmd == "seed-check":
        return cmd_seed_check(args, paths)
    send = not getattr(args, "no_alerts", False)
    if args.cmd == "poll":
        print(json.dumps(pipeline.run_poll(paths, max_videos=args.max_videos, send_alerts=send), indent=1))
    elif args.cmd == "score":
        out = score.run_score(paths, send_alerts=send)
        print(f"scored {len(out)} calls -> {paths.outcomes}")
    elif args.cmd == "weekly":
        print(f"wrote {score.run_weekly(paths, send_alerts=send)}")
    elif args.cmd == "heartbeat":
        print("stale warning sent" if pipeline.run_heartbeat(paths) else "heartbeat ok / already warned")
    elif args.cmd == "ipo-watch":
        print(json.dumps(ipo_watch.run_ipo_watch(paths, send_alerts=send), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
