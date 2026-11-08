"""Stock specialists: a general model, corrected for each stock where the stock has earned a correction (4.0, phase 7).

The general model is fitted on every stock together. On the calibration block, later than its training rows and
never trained on, each stock's average miss (in volatility units) is measured. A stock's correction is that average
shrunk toward zero by how much evidence there is, n / (n + SHRINK). It is kept only when the miss is clearly not
chance (|t| at least T_MIN = 3: across a hundred stocks a two-sigma rule would correct about five of them by luck
alone; at three, fewer than one in three hundred); otherwise the stock is forecast by the general model alone.

So a stock with too little evidence falls back to the general model, and a hundred tickers do not become a hundred
overconfident models. The specialist family then competes with the champion under the same promotion rule as any
other: if the corrections do not improve forecasts on the validation window, it is not promoted.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .baseline import ReturnModel

SHRINK = 250.0
T_MIN = 3.0


def _symbols(rows: pd.DataFrame) -> np.ndarray:
    idx = rows.index
    return np.asarray(idx.get_level_values("symbol") if isinstance(idx, pd.MultiIndex) else idx)


class Specialist(ReturnModel):
    def __init__(self, horizon: int, interval: float = 0.80, seed: int = 7, base: str = "ridge"):
        super().__init__(horizon, interval=interval, seed=seed)
        from .daily import make
        self.base_family = base
        self.family = f"{base}_spec"
        self.base = make(base, horizon, interval, seed)
        self.adjust: dict[str, float] = {}             # z units (multiplied by each forecast's sigma)
        self.by_stock: dict[str, dict[str, Any]] = {}

    def fit_point(self, rows: pd.DataFrame) -> "Specialist":
        super().fit_point(rows)
        self.base.fit_point(rows)
        return self

    def fit_band(self, rows: pd.DataFrame) -> "Specialist":
        """The per-stock corrections and the band, both from the calibration block."""
        self.adjust, self.by_stock = {}, {}
        if len(rows):
            s = self.sigma(rows)
            z = (rows[f"y{self.horizon}"].to_numpy(float) - self.base.point(rows)) / np.where(s > 0, s, np.nan)
            frame = pd.DataFrame({"sym": _symbols(rows), "z": z}).dropna()
            for sym, g in frame.groupby("sym"):
                n = len(g)
                mean = float(g["z"].mean())
                sd = float(g["z"].std(ddof=1)) if n > 2 else float("nan")
                t = mean / (sd / math.sqrt(n)) if n > 2 and sd > 0 else 0.0
                used = abs(t) >= T_MIN
                adj = mean * n / (n + SHRINK) if used else 0.0
                self.by_stock[str(sym)] = {"n": n, "mean_z": mean, "t": t, "used": used, "adjust_z": adj}
                if used:
                    self.adjust[str(sym)] = adj
        return super().fit_band(rows)

    def point(self, rows: pd.DataFrame) -> np.ndarray:
        base = self.base.point(rows)
        if not self.adjust or not len(rows):
            return base
        adj = np.array([self.adjust.get(str(sym), 0.0) for sym in _symbols(rows)])
        return base + adj * self.sigma(rows)

    def params(self) -> dict[str, Any]:
        used = {k: v for k, v in self.by_stock.items() if v["used"]}
        return {**super().params(), "base": self.base.params(), "stocks_corrected": len(used), "stocks_seen": len(self.by_stock),
                "by_stock": self.by_stock}


__all__ = ["Specialist", "SHRINK", "T_MIN"]
