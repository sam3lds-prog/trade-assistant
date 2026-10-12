"""Feature flags for the V2 build-out.

Each V2 area can be switched off without touching the code, which keeps the older screens
working while a new area is tried. Set FEATURE_<NAME>=0 (or 1) in .env to override a
default, for example FEATURE_PORTFOLIO_RISK=0. An area that is switched off answers its
/v2 requests with 404 and its screen says so.
"""

from __future__ import annotations

import os
from typing import Mapping

# A feature is on by default once it is built and tested; options_backtest stays off until a
# source of historical option prices is connected (see options/history.py).
FEATURE_DEFAULTS: dict[str, bool] = {
    "unified_positions": True,
    "trend_engine_v2": True,
    "portfolio_risk": True,
    "signal_calibration": True,
    "reversal_analysis": True,
    "options_lab": True,
    "scanner": True,
    "strategy_builder": True,
    "market_news": True,
    "ai_assistant": True,
    "today": True,
    "trade_history": True,
    "trade_ideas": True,
    "options_backtest": False,
}

TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


def load_features(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    env = os.environ if env is None else env
    out = dict(FEATURE_DEFAULTS)
    for name in FEATURE_DEFAULTS:
        raw = (env.get(f"FEATURE_{name.upper()}") or "").strip().lower()
        if raw in TRUE_WORDS:
            out[name] = True
        elif raw in FALSE_WORDS:
            out[name] = False
        elif raw:
            raise ValueError(f"FEATURE_{name.upper()} must be 1 or 0, got {raw!r}")
    return out
