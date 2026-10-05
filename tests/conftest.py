import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_ohlc(close: np.ndarray, spread_pts: int = 10) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + 0.0002
    low = np.minimum(open_, close) - 0.0002
    idx = pd.date_range("2026-01-01", periods=len(close), freq="15min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "spread": spread_pts}, index=idx)


@pytest.fixture
def random_walk():
    rng = np.random.default_rng(42)
    # marché avec des phases de tendance pour générer des croisements
    drift = np.repeat(rng.choice([-1, 1], 60), 100) * 0.00005
    close = 1.10 + np.cumsum(drift + rng.normal(0, 0.0008, drift.size))
    return make_ohlc(close)
