from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from jonah_ledger.prices import clean_frame

SYMBOLS = {t: {"name": t, "exchange": "NASDAQ", "etf": False} for t in ["NVDA", "AAPL", "TSLA", "MSFT", "BRK-B", "SPY", "PENNY"]}


def make_frame(start="2026-01-02", periods=300, base=100.0, step=1.0) -> pd.DataFrame:
    """Deterministic business-day bars: open=base+i*step, close=open+0.5, high=close+1, low=open-1."""
    idx = pd.bdate_range(start, periods=periods)
    o = pd.Series([base + i * step for i in range(periods)], index=idx)
    df = pd.DataFrame({"open": o, "close": o + 0.5, "high": o + 1.5, "low": o - 1.0})
    return clean_frame(df)


class FakeClient:
    """Mimics anthropic.Anthropic().messages.create returning a forced tool_use block."""

    def __init__(self, payload, reject_temperature=False):
        self.payload, self.reject_temperature, self.calls = payload, reject_temperature, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        if self.reject_temperature and "temperature" in kw:
            raise RuntimeError("temperature is deprecated for this model")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.payload)])


@pytest.fixture
def symbols():
    return dict(SYMBOLS)
