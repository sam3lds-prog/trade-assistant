"""The uncertainty band around a return forecast, fitted on outcomes the model never trained on (4.0).

Residuals are measured in volatility units, z = (log(1+y) - log(1+mu)) / sigma, on a calibration block that is
later than the model's training rows and purged of overlapping windows. The band's edges are z's quantiles, so an
80% band holds 80% of outcomes on that block by construction; whether it keeps holding is what the validation
window and the scorecards check. The probability of a rise comes from the same residuals.
"""

from __future__ import annotations

import math

import numpy as np

Z80 = 1.2815516
GRID = np.linspace(0.0, 1.0, 201)


class ResidualBand:
    def __init__(self, interval: float = 0.80):
        self.interval = interval
        self.quantiles: np.ndarray | None = None       # z at GRID
        self.cases = 0

    def fit(self, y: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> "ResidualBand":
        ok = np.isfinite(y) & np.isfinite(mu) & np.isfinite(sigma) & (sigma > 0) & (y > -1) & (mu > -1)
        if ok.sum() >= 50:
            z = (np.log1p(y[ok]) - np.log1p(mu[ok])) / sigma[ok]
            self.quantiles = np.quantile(z, GRID)
            self.cases = int(ok.sum())
        return self

    @property
    def fitted(self) -> bool:
        return self.quantiles is not None

    def z_edges(self) -> tuple[float, float]:
        lo_q, hi_q = (1 - self.interval) / 2, 1 - (1 - self.interval) / 2
        if self.quantiles is None:
            z = Z80 if abs(self.interval - 0.8) < 1e-9 else _normal_ppf(hi_q)
            return -z, z
        return float(np.interp(lo_q, GRID, self.quantiles)), float(np.interp(hi_q, GRID, self.quantiles))

    def apply(self, mu: np.ndarray, sigma: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(low, high, p_up) as simple returns."""
        zl, zh = self.z_edges()
        centre = np.log1p(mu)
        low, high = np.expm1(centre + zl * sigma), np.expm1(centre + zh * sigma)
        cut = -centre / np.where(sigma > 0, sigma, np.nan)       # y > 0  <=>  z > -log(1+mu)/sigma
        if self.quantiles is None:
            p_up = 1 - _normal_cdf(cut)
        else:
            p_up = 1 - np.interp(cut, self.quantiles, GRID, left=0.0, right=1.0)
        return low, high, np.clip(p_up, 0.0, 1.0)


def _normal_cdf(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1 + np.vectorize(math.erf)(np.asarray(x, dtype=float) / math.sqrt(2)))


def _normal_ppf(p: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if 0.5 * (1 + math.erf(mid / math.sqrt(2))) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


__all__ = ["ResidualBand", "Z80"]
