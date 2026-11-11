"""Hash chain: fresh ledger verifies, any changed byte fails."""
import pytest

from jonah_ledger.ledger import GENESIS, DuplicateCall, Ledger, LedgerError


def fields(vid="AAAAAAAAAAA", tick="NVDA", strength="buying", **kw):
    f = {"video_id": vid, "published_utc": "2026-11-08T14:00:00Z", "ticker": tick, "direction": "long", "strength": strength,
         "target": None, "deadline": None, "quote": "i'm buying this stock now", "extractor_version": "1.0.0+x",
         "extracted_at": "2026-11-08T15:00:00Z"}
    f.update(kw)
    return f


@pytest.fixture
def led(tmp_path):
    L = Ledger(tmp_path / "calls.jsonl")
    L.append(fields())
    L.append(fields(tick="AAPL", strength="wants"))
    L.append(fields(vid="BBBBBBBBBBB", tick="TSLA", strength="mention"))
    return L


def test_fresh_and_empty_ledgers_verify(tmp_path):
    L = Ledger(tmp_path / "x.jsonl")
    r = L.verify()
    assert r.ok and r.rows == 0 and r.head == GENESIS
    L.append(fields())
    assert L.verify().ok


def test_chain_links_rows(led):
    rows = led.rows()
    assert rows[0]["prev_hash"] == GENESIS
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert led.head() == rows[-1]["row_hash"]
    assert led.verify().ok and led.verify().rows == 3


@pytest.mark.parametrize("old,new", [("NVDA", "NVDB"), ("2026-11-08T14:00:00Z", "2026-11-07T14:00:00Z"), ("buying", "buyinG")])
def test_any_edited_byte_fails(led, old, new):
    raw = led.path.read_bytes()
    assert old.encode() in raw
    led.path.write_bytes(raw.replace(old.encode(), new.encode(), 1))
    assert not led.verify().ok


def test_whitespace_only_edit_fails(led):
    lines = led.path.read_text().splitlines()
    lines[0] = lines[0].replace('"call_id":', '"call_id": ', 1)
    led.path.write_text("\n".join(lines) + "\n")
    r = led.verify()
    assert not r.ok and any("canonical" in e for e in r.errors)


def test_deleting_or_reordering_rows_fails(led):
    lines = led.path.read_text().splitlines()
    led.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not led.verify().ok
    led.path.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n")
    assert not led.verify().ok


def test_recomputing_a_hash_after_edit_still_fails_downstream(led):
    import json

    from jonah_ledger.ledger import canonical, hash_row

    lines = led.path.read_text().splitlines()
    row = json.loads(lines[0])
    row["ticker"] = "MSFT"
    row["row_hash"] = hash_row(row)       # attacker fixes this row's own hash...
    lines[0] = canonical(row)
    led.path.write_text("\n".join(lines) + "\n")
    r = led.verify()
    assert not r.ok and any("line 2" in e and "prev_hash" in e for e in r.errors)  # ...but the next row no longer links


def test_duplicate_and_bad_rows_rejected(led):
    with pytest.raises(DuplicateCall):
        led.append(fields())
    with pytest.raises(LedgerError):
        led.append(fields(vid="CCCCCCCCCCC", strength="maybe"))
    with pytest.raises(LedgerError):
        led.append({**fields(vid="CCCCCCCCCCC"), "row_hash": "x"})


def test_corrections_are_new_rows_with_supersedes(led):
    before = led.path.read_bytes()
    led.append(fields(call_id="AAAAAAAAAAA:NVDA:r1", strength="wants", supersedes="AAAAAAAAAAA:NVDA"))
    led.append(fields(vid="BBBBBBBBBBB", tick="TSLA", call_id="BBBBBBBBBBB:TSLA:r1", strength="retracted", supersedes="BBBBBBBBBBB:TSLA"))
    assert led.path.read_bytes().startswith(before)     # old bytes untouched
    assert led.verify().ok
    eff = {(r["ticker"], r["strength"]) for r in led.effective_rows()}
    assert eff == {("NVDA", "wants"), ("AAPL", "wants")}
    with pytest.raises(LedgerError):
        led.append(fields(vid="DDDDDDDDDDD", strength="retracted"))   # retraction needs supersedes
