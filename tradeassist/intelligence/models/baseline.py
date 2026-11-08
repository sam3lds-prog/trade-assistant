"""The return models every learned one must beat (4.0).

- **no_change**: the price stays where it closed. The champion every replay starts with. A model that forecasts
  almost no movement scores a small error simply because most daily moves are small, so every other model is judged
  by how much it improves on this one, never by its error alone.
- **drift**: the stock's own average daily return over the last year, carried forward.

Both still get a band fitted on residuals they never saw (band.py); before any fit, it is the normal 80% band on the
stock's 20-day volatility, the same as Research shows for an uncalibrated forecast.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .band import ResidualBand


class ReturnModel:
    """A forecast of the simple return from a session's close to the close `horizon` sessions later."""
    family = "base"

    def __init__(self, horizon: int, interval: float = 0.80, seed: int = 7):
        self.horizon, self.interval, self.seed = int(horizon), float(interval), int(seed)
        self.band = ResidualBand(interval)
        self.train_rows = 0
        self.trained_through: str | None = None

    # The point forecast ---------------------------------------------------------------------------------------
    def fit_point(self, rows: pd.DataFrame) -> "ReturnModel":
        self.train_rows = int(len(rows))
        return self

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    # The band and the whole forecast -------------------------------------------------------------------------
    def sigma(self, rows: pd.DataFrame) -> np.ndarray:
        return rows["rvol"].to_numpy(float) * math.sqrt(self.horizon)

    def fit_band(self, rows: pd.DataFrame) -> "ReturnModel":
        if len(rows):
            self.band.fit(rows[f"y{self.horizon}"].to_numpy(float), self.point(rows), self.sigma(rows))
        return self

    def predict(self, rows: pd.DataFrame) -> dict[str, np.ndarray]:
        mu = np.clip(self.point(rows), -0.5, 0.5)
        s = self.sigma(rows)
        low, high, p_up = self.band.apply(mu, s)
        return {"mu": mu, "low": low, "high": high, "p_up": p_up, "sigma": s}

    def params(self) -> dict[str, Any]:
        return {"family": self.family, "horizon": self.horizon, "interval": self.interval, "band_cases": self.band.cases,
                "band_z": list(self.band.z_edges())}


class NoChange(ReturnModel):
    family = "no_change"

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        return np.zeros(len(rows))


class Drift(ReturnModel):
    family = "drift"

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        d = rows["drift250"].to_numpy(float) * self.horizon
        return np.where(np.isfinite(d), d, 0.0)


__all__ = ["ReturnModel", "NoChange", "Drift"]
