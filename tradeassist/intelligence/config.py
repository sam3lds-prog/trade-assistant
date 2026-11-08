"""What a replay does, fixed before it runs (4.0).

Every rule that decides a result is here and hashed into the run (`config_sha256`): the dates, the horizons, the
retraining schedule, the model families allowed to compete, the promotion rule, the trade's barriers and costs. A
replay never changes them part way; changing one is a new run with a new hash, so two results can only be compared
when it is clear what differed.

The untouched period. Development replays end before HOLDOUT_FROM. The year from it to today is kept unseen until
the design is settled (phase 4), then evaluated once (holdout.py records that it was opened). Moving the boundary
is a code change, visible in review, never a setting.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

# Ten years of predictions: nine developed on, the last one held out. The dataset starts a year earlier so the
# 200- and 250-day features are warm on the first prediction day.
DEVELOPMENT_FROM = date(2016, 11, 1)
HOLDOUT_FROM = date(2025, 11, 1)                 # its first session is Monday 3 November 2025
DATASET_FROM = date(2015, 11, 1)

KINDS = ("development", "holdout", "shadow")
DAILY_FAMILIES = ("no_change", "drift", "engines30", "ridge", "gbm")
TRADE_FAMILIES = ("base_rate", "gbm_cls")
RETRAIN = ("weekly", "monthly", "never")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PromotionRule:
    """When a challenger replaces the champion. Declared before any replay runs, and the same for every run.

    Daily models: on the validation window (the last `validation_sessions` sessions whose outcomes had matured by
    the training day, never trained on), the challenger's absolute error must beat the champion's by more than
    chance, its 80% band must hold 80% +/- `coverage_tolerance` of outcomes, and its 95th-percentile error may be at
    most `tail_tolerance` worse than the champion's. Trade models: the log loss must improve by more than chance, and
    the trades it would take must not earn less after costs than the champion's.

    "More than chance" is a one-sided Student t test at `alpha` on the per-date mean gain, using only dates whose
    windows do not overlap (every h-th date for an h-session horizon), with alpha divided among the challengers
    tested that day (Bonferroni): four challengers every week at a plain 5% would promote a lucky one most months.
    The validation window is long enough to hold `min_validation_dates` such dates (60 sessions for the one-day
    forecast, 150 for five days). And a challenger must pass on `confirmations` training days in a row before it is
    promoted, so one good window is not enough."""
    validation_sessions: int = 60
    min_validation_dates: int = 30
    min_validation_rows: int = 1000
    alpha: float = 0.05
    confirmations: int = 2
    coverage_tolerance: float = 0.07
    tail_quantile: float = 0.95
    tail_tolerance: float = 0.05
    trade_min_validation_rows: int = 150
    trade_min_train_rows: int = 300


def validation_window(rule: PromotionRule, horizon: int) -> int:
    """Sessions in the validation window for an h-session horizon: enough for the independent dates the test needs."""
    return max(rule.validation_sessions, rule.min_validation_dates * horizon)


@dataclass(frozen=True)
class TradeSpec:
    """The narrow trade the target/stop/timeout model judges (a triple barrier).

    A setup qualifies after session T's close; the trade enters at T+1's open and is held at most `hold_sessions`
    sessions (T+1 to T+hold). The target is `target_atr` average true ranges above the entry and the stop
    `stop_atr` below it, the range measured at T. A day whose high reaches the target and whose low reaches the stop
    is `ambiguous`: daily bars cannot say which came first (Tradier's intraday history reaches back weeks, not
    years), so it is scored as the stop. Costs are charged on entry and exit."""
    setups: tuple[str, ...] = ("pullback",)
    hold_sessions: int = 5
    target_atr: float = 1.5
    stop_atr: float = 1.0
    cost_bps_per_side: float = 5.0
    min_edge: float = 0.001                      # the take/pass gate: expected net return above 0.10%

    @property
    def version(self) -> str:
        body = json.dumps(asdict(self), sort_keys=True)
        return "barrier-" + hashlib.sha256(body.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class ReplayConfig:
    kind: str = "development"
    start: date = DEVELOPMENT_FROM
    end: date = date(2025, 10, 31)
    horizons: tuple[int, ...] = (1, 5)
    universe: str = "large-caps-2026"
    retrain: str = "weekly"                      # after the last session of each week
    min_train_sessions: int = 250                # matured sessions needed before the first challenger
    train_window_sessions: int = 1000            # each fit sees at most the last four years
    families: tuple[str, ...] = ("drift", "engines30", "ridge", "gbm")
    trade_families: tuple[str, ...] = ("gbm_cls",)
    interval: float = 0.80
    promotion: PromotionRule = field(default_factory=PromotionRule)
    trade: TradeSpec = field(default_factory=TradeSpec)
    seed: int = 7

    def check(self) -> "ReplayConfig":
        if self.kind not in KINDS:
            raise ConfigError(f"kind must be one of {', '.join(KINDS)}")
        if self.start > self.end:
            raise ConfigError("start is after end")
        if self.kind == "development" and self.end >= HOLDOUT_FROM:
            raise ConfigError(f"A development replay must end before {HOLDOUT_FROM}: the year from then on is held out "
                              "untouched for the final evaluation (phase 4).")
        if self.kind == "holdout" and self.start < HOLDOUT_FROM:
            raise ConfigError(f"A holdout replay starts on or after {HOLDOUT_FROM}.")
        if not self.horizons or any(h < 1 or h > 20 for h in self.horizons):
            raise ConfigError("horizons must be between 1 and 20 sessions")
        bad = [f for f in self.families if f not in DAILY_FAMILIES]
        if bad:
            raise ConfigError(f"unknown model families: {', '.join(bad)}")
        bad = [f for f in self.trade_families if f not in TRADE_FAMILIES]
        if bad:
            raise ConfigError(f"unknown trade model families: {', '.join(bad)}")
        if self.retrain not in RETRAIN:
            raise ConfigError(f"retrain must be one of {', '.join(RETRAIN)}")
        if not 0.5 <= self.interval < 1.0:
            raise ConfigError("interval must be between 0.5 and 1")
        return self

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        d["horizons"], d["families"], d["trade_families"] = list(self.horizons), list(self.families), list(self.trade_families)
        d["trade"]["setups"] = list(self.trade.setups)
        d["trade"]["version"] = self.trade.version
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayConfig":
        d = dict(d)
        trade = dict(d.pop("trade", {}) or {})
        trade.pop("version", None)
        if "setups" in trade:
            trade["setups"] = tuple(trade["setups"])
        promo = dict(d.pop("promotion", {}) or {})
        for k in ("horizons", "families", "trade_families"):
            if k in d:
                d[k] = tuple(d[k])
        for k in ("start", "end"):
            if isinstance(d.get(k), str):
                d[k] = date.fromisoformat(d[k])
        return cls(**d, promotion=PromotionRule(**promo), trade=TradeSpec(**trade))

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()

    def with_(self, **changes: Any) -> "ReplayConfig":
        return replace(self, **changes)


__all__ = ["ReplayConfig", "PromotionRule", "validation_window", "TradeSpec", "ConfigError", "DEVELOPMENT_FROM", "HOLDOUT_FROM", "DATASET_FROM",
           "DAILY_FAMILIES", "TRADE_FAMILIES", "KINDS"]
