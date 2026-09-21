import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from crypto_ai.config import DEFAULT_SETTINGS  # noqa: E402


@pytest.fixture
def cfg():
    return dict(DEFAULT_SETTINGS)


def synthetic_frames(n=3000, seed=1, start="2025-01-01"):
    """Random-walk 15m candles for BTC/ETH/XRP (no real edge by construction)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC", name="ts")
    frames = {}
    for i, s in enumerate(["BTC", "ETH", "XRP"]):
        r = rng.normal(0, 0.003, n)
        close = 100 * (i + 1) * np.exp(np.cumsum(r))
        frames[s] = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close,
                                  "volume": rng.uniform(50, 150, n)}, index=idx)
    return frames


@pytest.fixture
def frames():
    return synthetic_frames()
