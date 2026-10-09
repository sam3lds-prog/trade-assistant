import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def make_bars(closes, start="2024-01-02", opens=None, highs=None, lows=None, volume=1_000_000):
    """Daily bars from a list of closes. Unless given, open = previous close and the
    high/low hug the open and close."""
    closes = np.asarray(closes, dtype=float)
    opens = np.asarray(opens, dtype=float) if opens is not None else np.concatenate([[closes[0]], closes[:-1]])
    highs = np.asarray(highs, dtype=float) if highs is not None else np.maximum(opens, closes)
    lows = np.asarray(lows, dtype=float) if lows is not None else np.minimum(opens, closes)
    dates = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"date": dates, "open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": float(volume)})


def random_walk(n=900, seed=1, start=100.0, vol=0.015, drift=0.0003):
    rng = np.random.default_rng(seed)
    rets = drift + vol * rng.standard_normal(n)
    closes = start * np.exp(np.cumsum(rets))
    opens = np.concatenate([[start], closes[:-1]]) * np.exp(0.003 * rng.standard_normal(n))
    highs = np.maximum(opens, closes) * (1 + np.abs(0.006 * rng.standard_normal(n)))
    lows = np.minimum(opens, closes) * (1 - np.abs(0.006 * rng.standard_normal(n)))
    df = make_bars(closes, start="2021-01-04", opens=opens, highs=highs, lows=lows)
    df["volume"] = 1e6 * np.exp(0.3 * rng.standard_normal(n))
    return df


@pytest.fixture
def walk():
    return random_walk()


@pytest.fixture(autouse=True)
def _close_databases(monkeypatch):
    """Close every database a test opened, so no test leaves a connection for the garbage
    collector (the app itself keeps one connection open for its whole life)."""
    from tradeassist import db as dbmod
    opened = []
    real_init = dbmod.Database.__init__

    def tracking_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        opened.append(self)
    monkeypatch.setattr(dbmod.Database, "__init__", tracking_init)
    yield
    for d in opened:
        try:
            d.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
