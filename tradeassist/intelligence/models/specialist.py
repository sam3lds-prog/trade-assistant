"""Stock specialists: a general model, corrected for each stock where the stock has earned a correction (4.0, phase 7).

The general model is fitted on every stock together. On the calibration block, later than its training rows and
never trained on, each stock's average miss (in volatility units) is measured. A stock's correction is that average
shrunk toward zero by how much evidence there is, n / (n + SHRINK). It is kept only when the miss is clearly not
chance (|t| at least T_MIN = 3: across a hundred stocks a two-sigma rule would correct about five of them by luck
alone; at three, fewer than one in three hundred); otherwise the stock is forecast by the general model alone.
For an h-session horizon, n and t count only every h-th day: neighbouring five-day outcomes overlap, and counting
them all inflated t by about the square root of h (with no stock biased at all, 17 of 100 stocks were "corrected" at
five days, 51 at twenty). With few independent days the bar is a Student t at the same rate, so higher than 3.

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


def t_needed(n: int) -> float:
    """|t| for the same false-correction rate as three standard errors with plenty of data (two-sided 0.27%), on
    n independent days: a Student t, so a stock with few days needs more than 3."""
    from scipy.stats import norm, t as student
    if n < 3:
        return math.inf
    return float(student.ppf(1 - norm.sf(T_MIN), n - 1))


def _symbols(rows: pd.DataFrame) -> np.ndarray:
    idx = rows.index
    return np.asarray(idx.get_level_values("symbol") if isinstance(idx, pd.MultiIndex) else idx)


def _sessions(rows: pd.DataFrame) -> np.ndarray:
    idx = rows.index
    if isinstance(idx, pd.MultiIndex) and "session" in idx.names:
        return np.asarray(idx.get_level_values("session"))
    return np.arange(len(rows))


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
            frame = pd.DataFrame({"sym": _symbols(rows), "session": _sessions(rows), "z": z}).dropna()
            h = max(1, int(self.horizon))
            for sym, g in frame.groupby("sym"):
                zs = g.sort_values("session")["z"].to_numpy()
                # h-session outcomes on neighbouring days share h-1 days of returns: only every h-th one, counting
                # back from the latest, is independent evidence (as in the promotion rule's test)
                ind = zs[::-1][::h][::-1]
                n = len(ind)
                mean = float(zs.mean())
                sd = float(ind.std(ddof=1)) if n > 2 else float("nan")
                t = float(ind.mean()) / (sd / math.sqrt(n)) if n > 2 and sd > 0 else 0.0
                need = t_needed(n)
                used = abs(t) >= need
                adj = mean * n / (n + SHRINK) if used else 0.0
                self.by_stock[str(sym)] = {"n": n, "rows": len(zs), "mean_z": mean, "t": t, "t_needed": need, "used": used, "adjust_z": adj}
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


__all__ = ["Specialist", "SHRINK", "T_MIN", "t_needed"]
