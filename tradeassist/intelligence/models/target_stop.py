"""Will a qualifying setup's trade reach its target first, its stop first, or neither in time? (4.0, phase 3)

The question is narrow on purpose: one defined trade (config.TradeSpec), three outcomes. The model gives a
probability for each; the expected net return after costs follows from them and from what each outcome has paid on
average, in units of the stock's daily range (a target hit on a volatile stock pays more than on a quiet one).

- **base_rate**: how often each outcome happened for this setup over the training window. The champion every replay
  starts with: a learned model has to beat history's plain frequencies.
- **gbm_cls**: gradient-boosted trees on the setup's features, its probabilities calibrated (sigmoid) on a later
  block it was not trained on, as scikit-learn advises: calibrating on the training rows biases the probabilities.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..data.features import FEATURES, matrix
from ..replay.labels import SETUPS
from .daily import usable_columns, with_usable

CLASSES = ("target", "stop", "timeout")
PRIOR_UNITS = {"target": 1.5, "stop": -1.0, "timeout": 0.0}


def _units(rows: pd.DataFrame) -> dict[str, float]:
    """The average net return of each outcome, in daily ranges."""
    out = {}
    for c in CLASSES:
        sub = rows[rows["y"] == c]
        r = (sub["net"].astype(float) / sub["atr_pct"].astype(float)).replace([np.inf, -np.inf], np.nan).dropna()
        out[c] = float(r.mean()) if len(r) >= 5 else PRIOR_UNITS[c]
    return out


class TradeModel:
    family = "base"

    def __init__(self, hold: int, seed: int = 7):
        self.hold, self.seed = int(hold), int(seed)
        self.units = dict(PRIOR_UNITS)
        self.train_rows = 0
        self.trained_through: str | None = None
        self.fitted = False

    def fit(self, rows: pd.DataFrame) -> "TradeModel":
        self.train_rows = int(len(rows))
        self.units = _units(rows)
        return self

    def calibrate(self, rows: pd.DataFrame) -> "TradeModel":
        return self

    def proba(self, rows: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def predict(self, rows: pd.DataFrame) -> dict[str, np.ndarray]:
        n = len(rows)
        if not self.fitted or n == 0:
            nan = np.full(n, np.nan)
            return {"p_target": nan, "p_stop": nan, "p_timeout": nan, "expected": nan}
        p = self.proba(rows)
        u = np.array([self.units[c] for c in CLASSES])
        expected = rows["atr_pct"].to_numpy(float) * (p @ u)
        return {"p_target": p[:, 0], "p_stop": p[:, 1], "p_timeout": p[:, 2], "expected": expected}

    def params(self) -> dict[str, Any]:
        return {"family": self.family, "hold": self.hold, "units_atr": self.units, "train_rows": self.train_rows}


class BaseRate(TradeModel):
    family = "base_rate"

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.freq: dict[str, np.ndarray] = {}
        self.pooled = np.full(3, 1 / 3)

    def fit(self, rows: pd.DataFrame) -> "BaseRate":
        super().fit(rows)
        if len(rows) < 30:
            return self
        def freq(sub: pd.DataFrame) -> np.ndarray:
            counts = np.array([(sub["y"] == c).sum() for c in CLASSES], dtype=float) + 1.0   # add-one smoothing
            return counts / counts.sum()
        self.pooled = freq(rows)
        self.freq = {s: freq(sub) for s, sub in rows.groupby("setup") if len(sub) >= 30}
        self.fitted = True
        return self

    def proba(self, rows: pd.DataFrame) -> np.ndarray:
        return np.vstack([self.freq.get(s, self.pooled) for s in rows["setup"]]) if len(rows) else np.zeros((0, 3))

    def params(self) -> dict[str, Any]:
        return {**super().params(), "pooled": dict(zip(CLASSES, map(float, self.pooled))),
                "by_setup": {s: dict(zip(CLASSES, map(float, f))) for s, f in self.freq.items()}}


class GBMClassifier(TradeModel):
    family = "gbm_cls"
    SETTINGS = {"learning_rate": 0.05, "max_iter": 150, "max_leaf_nodes": 15, "min_samples_leaf": 100, "l2_regularization": 1.0}

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.model = None
        self.calibrated = None
        self.usable: np.ndarray | None = None

    def _x(self, rows: pd.DataFrame) -> np.ndarray:
        onehot = np.column_stack([(rows["setup"] == s).to_numpy(float) for s in SETUPS])
        return with_usable(np.column_stack([matrix(rows), onehot]), self.usable)

    def fit(self, rows: pd.DataFrame) -> "GBMClassifier":
        from sklearn.ensemble import HistGradientBoostingClassifier
        super().fit(rows)
        if len(rows) < 100 or rows["y"].nunique() < 3:
            return self
        self.usable = None
        self.usable = usable_columns(self._x(rows))
        self.model = HistGradientBoostingClassifier(early_stopping=False, random_state=self.seed, **self.SETTINGS)
        self.model.fit(self._x(rows), rows["y"].to_numpy())
        self.calibrated = None
        self.fitted = True
        return self

    def calibrate(self, rows: pd.DataFrame) -> "GBMClassifier":
        if self.model is None or len(rows) < 60 or rows["y"].nunique() < 3:
            return self
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.frozen import FrozenEstimator
        cal = CalibratedClassifierCV(FrozenEstimator(self.model), method="sigmoid")
        cal.fit(self._x(rows), rows["y"].to_numpy())
        self.calibrated = cal
        return self

    def proba(self, rows: pd.DataFrame) -> np.ndarray:
        est = self.calibrated or self.model
        raw = est.predict_proba(self._x(rows))
        order = [list(est.classes_).index(c) for c in CLASSES]
        p = raw[:, order]
        return p / p.sum(axis=1, keepdims=True)

    def params(self) -> dict[str, Any]:
        return {**super().params(), "calibrated": self.calibrated is not None, **self.SETTINGS}


FAMILIES: dict[str, type[TradeModel]] = {"base_rate": BaseRate, "gbm_cls": GBMClassifier}


def make(family: str, hold: int, seed: int = 7) -> TradeModel:
    try:
        return FAMILIES[family](hold, seed=seed)
    except KeyError:
        raise ValueError(f"Unknown trade model family {family!r}") from None


def log_loss(p: dict[str, np.ndarray], y: np.ndarray) -> np.ndarray:
    """Each row's log loss (how surprised the probabilities were by what happened)."""
    cols = {"target": p["p_target"], "stop": p["p_stop"], "timeout": p["p_timeout"]}
    got = np.array([cols[c][i] for i, c in enumerate(y)]) if len(y) else np.zeros(0)
    return -np.log(np.clip(got, 1e-6, 1.0))


__all__ = ["CLASSES", "FAMILIES", "make", "BaseRate", "GBMClassifier", "TradeModel", "log_loss", "FEATURES"]
