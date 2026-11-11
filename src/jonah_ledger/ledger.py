"""Append-only, hash-chained call ledger.

Every row carries ``prev_hash`` (the previous row's ``row_hash``) and ``row_hash`` (sha256 of the
canonical JSON of the row without ``row_hash``). Because each hash covers the previous one, rows
cannot be edited after prices are known without breaking the chain. ``verify`` also requires every
line to be byte-identical to its canonical serialisation, so a whitespace-only edit fails too.

Limits worth knowing: truncating the *tail* of the file leaves a valid shorter chain. The head hash
is therefore printed by ``ledger head`` and included in the weekly digest, and git history is the
second witness. Corrections are new rows with ``supersedes``; nothing is edited or deleted.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import LONG_STRENGTHS, RETRACTED, STRENGTHS, UTC

GENESIS = "0" * 64
REQUIRED = (
    "call_id", "video_id", "published_utc", "ticker", "direction", "strength", "target",
    "deadline", "quote", "extractor_version", "extracted_at", "prev_hash", "row_hash",
)


class LedgerError(Exception):
    pass


class DuplicateCall(LedgerError):
    pass


def canonical(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_row(row: dict[str, Any]) -> str:
    body = {k: v for k, v in row.items() if k != "row_hash"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class VerifyResult:
    ok: bool
    rows: int
    head: str
    errors: list[str] = field(default_factory=list)


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    # ---- reading -------------------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line:
                    out.append(json.loads(line))
        return out

    def head(self) -> str:
        rows = self.rows()
        return rows[-1]["row_hash"] if rows else GENESIS

    def effective_rows(self) -> list[dict[str, Any]]:
        """Rows that are not superseded and not retractions."""
        rows = self.rows()
        superseded = {r["supersedes"] for r in rows if r.get("supersedes")}
        return [r for r in rows if r["call_id"] not in superseded and r["strength"] != RETRACTED]

    def has_video(self, video_id: str) -> bool:
        return any(r["video_id"] == video_id for r in self.rows())

    # ---- writing -------------------------------------------------------------------------
    def append(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Append one row. ``fields`` must not contain prev_hash/row_hash. Returns the stored row."""
        return self.append_many([fields])[0]

    def append_many(self, batch: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        batch = list(batch)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                existing = [json.loads(line) for line in fh.read().splitlines() if line]
                prev = existing[-1]["row_hash"] if existing else GENESIS
                known_ids = {r["call_id"] for r in existing}
                written: list[dict[str, Any]] = []
                for fields in batch:
                    row = self._prepare(fields, prev, known_ids)
                    fh.seek(0, os.SEEK_END)
                    fh.write(canonical(row) + "\n")
                    prev = row["row_hash"]
                    known_ids.add(row["call_id"])
                    written.append(row)
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return written

    @staticmethod
    def _prepare(fields: dict[str, Any], prev: str, known_ids: set[str]) -> dict[str, Any]:
        if "prev_hash" in fields or "row_hash" in fields:
            raise LedgerError("callers must not set prev_hash/row_hash")
        row = dict(fields)
        row.setdefault("target", None)
        row.setdefault("deadline", None)
        row.setdefault("call_id", f"{row['video_id']}:{row['ticker']}")
        if row["strength"] not in STRENGTHS and not (row["strength"] == RETRACTED and row.get("supersedes")):
            raise LedgerError(f"bad strength {row['strength']!r}")
        if row.get("supersedes") and row["supersedes"] not in known_ids:
            raise LedgerError(f"supersedes unknown call_id {row['supersedes']!r}")
        if row["call_id"] in known_ids:
            raise DuplicateCall(row["call_id"])
        missing = [k for k in REQUIRED if k not in row and k not in ("prev_hash", "row_hash")]
        if missing:
            raise LedgerError(f"missing fields: {missing}")
        row["prev_hash"] = prev
        row["row_hash"] = hash_row(row)
        return row

    # ---- verification --------------------------------------------------------------------
    def verify(self) -> VerifyResult:
        errors: list[str] = []
        if not self.path.exists():
            return VerifyResult(True, 0, GENESIS)
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            errors.append("file does not end with a newline")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return VerifyResult(False, 0, GENESIS, [f"not utf-8: {exc}"])
        prev = GENESIS
        n = 0
        seen_ids: set[str] = set()
        for i, line in enumerate(text.split("\n")[:-1] if text.endswith("\n") else text.split("\n"), start=1):
            if not line:
                errors.append(f"line {i}: blank line")
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {i}: invalid JSON ({exc.msg})")
                continue
            n += 1
            if canonical(row) != line:
                errors.append(f"line {i}: bytes differ from canonical serialisation")
            if row.get("prev_hash") != prev:
                errors.append(f"line {i}: prev_hash does not match previous row_hash")
            if hash_row(row) != row.get("row_hash"):
                errors.append(f"line {i}: row_hash does not match contents")
            cid = row.get("call_id")
            if cid in seen_ids:
                errors.append(f"line {i}: duplicate call_id {cid}")
            seen_ids.add(cid)
            if row.get("supersedes") and row["supersedes"] not in seen_ids:
                errors.append(f"line {i}: supersedes unknown/later call_id {row['supersedes']}")
            prev = row.get("row_hash", prev)
        return VerifyResult(not errors, n, prev, errors)


def is_long(row: dict[str, Any]) -> bool:
    return row["strength"] in LONG_STRENGTHS
