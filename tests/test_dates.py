"""ET date handling: weekends, after-hours uploads, DST, and the strict 'after' entry rule."""
from datetime import date, datetime

import pytest

from conftest import make_frame
from jonah_ledger.dates import approx_next_session_open_utc, et_date
from jonah_ledger.prices import compute_outcome

# 2026-11-06 is a Friday. Frame starts 2026-01-02 so Nov 2026 exists and has weekends.
STOCK = make_frame()
SPY = make_frame()


def entry_for(published_utc: str) -> str:
    return compute_outcome(published_utc, STOCK, SPY)["entry_date"]


@pytest.mark.parametrize("published, et, entry", [
    ("2026-11-06T20:00:00Z", date(2026, 11, 6), "2026-11-09"),   # Fri 3pm ET -> Monday
    ("2026-11-07T04:30:00Z", date(2026, 11, 6), "2026-11-09"),   # Fri 23:30 ET (Sat in UTC) -> still Friday ET -> Monday
    ("2026-11-07T15:00:00Z", date(2026, 11, 7), "2026-11-09"),   # Saturday -> Monday
    ("2026-11-09T02:00:00Z", date(2026, 11, 8), "2026-11-09"),   # Sunday 9pm ET (Mon in UTC) -> Monday
    ("2026-11-09T12:00:00Z", date(2026, 11, 9), "2026-11-10"),   # Monday 7am ET pre-market -> STRICTLY after -> Tuesday
    ("2026-11-05T22:00:00Z", date(2026, 11, 5), "2026-11-06"),   # Thu 5pm ET after-hours -> Friday
])
def test_et_date_and_entry(published, et, entry):
    assert et_date(published) == et
    assert entry_for(published) == entry


def test_standard_time_rolls_back_a_day():
    # 03:30Z on 2026-11-02 is 22:30 EST on Nov 1 (DST ended Nov 1 2026)
    assert et_date("2026-11-02T03:30:00Z") == date(2026, 11, 1)
    assert et_date("2026-07-02T03:30:00Z") == date(2026, 7, 1)  # EDT, 23:30


def test_approx_open_skips_weekend():
    # Friday-night upload: planned entry is Monday 09:30 ET = 14:30Z (EST)
    assert approx_next_session_open_utc("2026-11-07T04:30:00Z") == datetime.fromisoformat("2026-11-09T14:30:00+00:00")
