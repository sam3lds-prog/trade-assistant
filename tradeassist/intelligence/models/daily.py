"""Learned forecasts of the next-day and five-day return (4.0, phase 2).

- **engines30**: 3.0's Research forecast as it would run every day: momentum, mean reversion and drift, weighted by
  ridge regression shrunk toward "no move" (forecast._ridge). The analog engine is left out: its search over every
  past situation is too slow to run for a hundred stocks on every session of ten years.
- **ridge**: a linear model of all the features, in volatility units, shrunk toward zero.
- **gbm**: gradient-boosted trees (scikit-learn's histogram boosting, the LightGBM algorithm without LightGBM's
  macOS OpenMP install), on the same features and target.

The target is the return divided by the stock's volatility to the horizon, clipped at six, so one stock's wild
week does not dominate; the forecast multiplies back.
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

from ... import forecast as F
from ..data.features import FEATURES, matrix
from .baseline import Drift, NoChange, ReturnModel

Z_CLIP = 6.0


def _z_target(rows: pd.DataFrame, h: int) -> np.ndarray:
    y, s = rows[f"y{h}"].to_numpy(float), rows[f"sigma{h}"].to_numpy(float)
    return np.clip(y / s, -Z_CLIP, Z_CLIP)


class Engines30(ReturnModel):
    family = "engines30"
    COMPONENTS = ("momentum", "meanrev", "drift")

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.weights = np.zeros(3)

    def _x(self, rows: pd.DataFrame) -> np.ndarray:
        h = self.horizon
        x = np.column_stack([rows["ret20"].to_numpy(float) * h / 20.0, -rows["dist50"].to_numpy(float) * h / 50.0,
                             rows["drift250"].to_numpy(float) * h])
        return np.where(np.isfinite(x), x, 0.0)

    def fit_point(self, rows: pd.DataFrame) -> "Engines30":
        super().fit_point(rows)
        y = rows[f"y{self.horizon}"].to_numpy(float)
        self.weights = F._ridge(self._x(rows), y) if len(rows) >= 30 else np.zeros(3)
        return self

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        return self._x(rows) @ self.weights

    def params(self) -> dict[str, Any]:
        return {**super().params(), "weights": dict(zip(self.COMPONENTS, map(float, self.weights)))}


class Ridge(ReturnModel):
    family = "ridge"
    STRENGTH = 1.0

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.mean = np.zeros(len(FEATURES))
        self.scale = np.ones(len(FEATURES))
        self.coef = np.zeros(len(FEATURES))
        self.intercept = 0.0

    def _x(self, rows: pd.DataFrame) -> np.ndarray:
        x = (matrix(rows) - self.mean) / self.scale
        return np.where(np.isfinite(x), np.clip(x, -8, 8), 0.0)

    def fit_point(self, rows: pd.DataFrame) -> "Ridge":
        super().fit_point(rows)
        raw = matrix(rows)
        with warnings.catch_warnings():                 # a column with no values yet (a sector fund before it listed)
            warnings.simplefilter("ignore", RuntimeWarning)
            self.mean = np.nan_to_num(np.nanmean(raw, axis=0))
            sd = np.nanstd(raw, axis=0)
        self.scale = np.where(np.isfinite(sd) & (sd > 1e-12), sd, 1.0)
        x, z = self._x(rows), _z_target(rows, self.horizon)
        self.intercept = float(z.mean()) if len(z) else 0.0
        zc = z - self.intercept
        xtx = x.T @ x
        lam = self.STRENGTH * np.trace(xtx) / max(1, x.shape[1])
        self.coef = np.linalg.solve(xtx + lam * np.eye(x.shape[1]), x.T @ zc) if len(z) else np.zeros(x.shape[1])
        return self

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        return (self.intercept + self._x(rows) @ self.coef) * self.sigma(rows)

    def params(self) -> dict[str, Any]:
        top = np.argsort(-np.abs(self.coef))[:6]
        return {**super().params(), "intercept": self.intercept, "top_features": {FEATURES[i]: float(self.coef[i]) for i in top}}


def usable_columns(x: np.ndarray) -> np.ndarray:
    """Columns with at least one value. Tree binning fails on a column that is empty throughout (the sector-strength
    features of a stock whose sector fund had not listed yet, XLC before June 2018)."""
    return np.isfinite(x).any(axis=0) if len(x) else np.ones(x.shape[1], dtype=bool)


def with_usable(x: np.ndarray, usable: np.ndarray | None) -> np.ndarray:
    if usable is None or usable.all():
        return x
    x = x.copy()
    x[:, ~usable] = 0.0
    return x


class GBM(ReturnModel):
    family = "gbm"
    SETTINGS = {"learning_rate": 0.05, "max_iter": 150, "max_leaf_nodes": 15, "min_samples_leaf": 200, "l2_regularization": 1.0}

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.model = None
        self.usable: np.ndarray | None = None

    def fit_point(self, rows: pd.DataFrame) -> "GBM":
        from sklearn.ensemble import HistGradientBoostingRegressor
        super().fit_point(rows)
        if len(rows) < 50:
            self.model = None
            return self
        x = matrix(rows)
        self.usable = usable_columns(x)
        self.model = HistGradientBoostingRegressor(loss="squared_error", early_stopping=False, random_state=self.seed,
                                                   **self.SETTINGS)
        self.model.fit(with_usable(x, self.usable), _z_target(rows, self.horizon))
        return self

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        if self.model is None or not len(rows):
            return np.zeros(len(rows))
        return self.model.predict(with_usable(matrix(rows), self.usable)) * self.sigma(rows)

    def params(self) -> dict[str, Any]:
        return {**super().params(), **self.SETTINGS}


FAMILIES: dict[str, type[ReturnModel]] = {"no_change": NoChange, "drift": Drift, "engines30": Engines30, "ridge": Ridge, "gbm": GBM}


def make(family: str, horizon: int, interval: float = 0.80, seed: int = 7) -> ReturnModel:
    try:
        return FAMILIES[family](horizon, interval=interval, seed=seed)
    except KeyError:
        raise ValueError(f"Unknown model family {family!r}") from None


__all__ = ["FAMILIES", "make", "Engines30", "Ridge", "GBM", "usable_columns", "with_usable"]
