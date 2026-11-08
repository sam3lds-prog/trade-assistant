"""What happened after each session, and from when it may be known (4.0).

A label is the outcome of a prediction made after session S's close:

    daily, horizon h: the simple return from S's close to the close h sessions later (S+h)
    trade:            the barrier the setup's trade reached, entering at S+1's open and held to S+hold at most

**Maturity.** A label exists only once its window has closed. The daily label of S matures at S+h's close; a
trade's at S+hold's close (even when it reached a barrier earlier, so that every trade label of a session matures
together). Training on session T may use a label only if it had matured by T's close: the five-day outcome of a
prediction made on T is invisible on T+1 to T+4 and usable from T+5 (tests/test_v4_replay.py).

The tables here are computed once over the whole dataset for speed; nothing outside this module and the as-of
view (clock.py) reads them, and the view hands out only rows that have matured.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from ..config import TradeSpec
from ..data.calendar import Calendar
from ..data.features import FACTORS, FEATURES, Panel
from .execution import outcome_class, simulate

SETUPS = {
    "pullback": "a pullback of at least 2.5 daily ranges from the 20-day high, above the 50- and 200-day averages",
    "drop": "a fall of at least two daily ranges in the session",
    "gap_down": "an opening gap down of at least one daily range",
}


def setup_mask(f: pd.DataFrame, setup: str) -> np.ndarray:
    """Where a setup qualifies, from the features of the same session (analogs.detect's definitions). Unknown
    features (NaN) never qualify."""
    with np.errstate(invalid="ignore"):
        if setup == "pullback":
            return ((f["drawdown20"].to_numpy(float) <= -2.5 * f["atr_pct"].to_numpy(float)) & (f["dist50"].to_numpy(float) > 0)
                    & (f["above200"].to_numpy(float) == 1.0))
        if setup == "drop":
            return f["move_atr"].to_numpy(float) <= -2.0
        if setup == "gap_down":
            return f["gap_atr"].to_numpy(float) <= -1.0
    raise ValueError(f"Unknown setup {setup!r}; known: {', '.join(SETUPS)}")


class LabelBook:
    def __init__(self, panel: Panel, calendar: Calendar, horizons: tuple[int, ...], trade: TradeSpec):
        self.panel, self.cal, self.horizons, self.trade = panel, calendar, tuple(horizons), trade
        self.pos = {pd.Timestamp(d): i for i, d in enumerate(calendar.sessions)}
        table = panel.features[list(FEATURES) + list(FACTORS) + ["close", "rvol", "atr_pct", "dist50", "drawdown20", "above200"]].copy()
        table = table.loc[:, ~table.columns.duplicated()]
        table["eligible"] = panel.eligible.stack().reindex(table.index).fillna(False).astype(bool).to_numpy()
        table["regime"] = panel.regime.reindex(table.index.get_level_values("session")).to_numpy()
        for h in self.horizons:
            fwd = panel.close.shift(-h) / panel.close - 1.0
            table[f"y{h}"] = fwd.stack().reindex(table.index).to_numpy()
            table[f"sigma{h}"] = table["rvol"].to_numpy() * math.sqrt(h)
        # The setup that qualified on each row (the first listed wins), from that row's own features.
        setup = np.full(len(table), None, dtype=object)
        eligible = table["eligible"].to_numpy(bool)
        for name in trade.setups:
            hit = setup_mask(table, name) & eligible & (setup == None)  # noqa: E711 - elementwise on an object array
            setup[hit] = name
        table["setup"] = setup
        self.table = table
        self._trades: pd.DataFrame | None = None

    def matured_through(self, h: int, t: pd.Timestamp) -> pd.Timestamp | None:
        """The last as-of session whose h-session window has closed by session t's close."""
        i = self.pos[t] - h
        return self.panel.sessions[i] if i >= 0 else None

    # ---- daily -----------------------------------------------------------------------------------------------
    def daily(self, h: int, first: pd.Timestamp, last: pd.Timestamp) -> pd.DataFrame:
        """Eligible rows with a known outcome for as-of sessions first..last (the caller enforces maturity)."""
        block = self.table.loc[(slice(first, last), slice(None)), :]
        block = block[block["eligible"] & np.isfinite(block[f"y{h}"]) & (block[f"sigma{h}"] > 0)]
        return block

    def realized(self, h: int, session: pd.Timestamp, symbol: str) -> float | None:
        try:
            v = float(self.table.at[(session, symbol), f"y{h}"])
        except KeyError:
            return None
        return v if math.isfinite(v) else None

    # ---- trades ----------------------------------------------------------------------------------------------
    def setups_on(self, session: pd.Timestamp) -> pd.DataFrame:
        """The eligible stocks whose setup qualified after this session's close, with which setup (first listed wins)."""
        try:
            block = self.table.xs(session, level="session")
        except KeyError:
            return self.table.iloc[0:0].droplevel("session")
        return block[block["setup"].notna()]

    def trade_outcome(self, session: pd.Timestamp, symbol: str) -> dict[str, Any]:
        """The barrier outcome of a trade set up after `session`'s close. Reads bars after the session: only the
        labeller (at maturity) and the training table call it."""
        i = self.pos[session]
        hold = self.trade.hold_sessions
        if i + hold >= len(self.panel.sessions):
            return {"barrier": None}
        sl = slice(i + 1, i + 1 + hold)
        p = self.panel
        atr_abs = float(self.table.at[(session, symbol), "atr_pct"]) * float(self.table.at[(session, symbol), "close"])
        out = simulate(p.open[symbol].to_numpy()[sl], p.high[symbol].to_numpy()[sl], p.low[symbol].to_numpy()[sl],
                       p.close[symbol].to_numpy()[sl], atr_abs, target_atr=self.trade.target_atr, stop_atr=self.trade.stop_atr,
                       cost_bps_per_side=self.trade.cost_bps_per_side)
        if out["barrier"] is not None:
            out["exit_session"] = self.panel.sessions[i + 1 + out["exit_offset"]]
        return out

    def trades(self) -> pd.DataFrame:
        """Every setup in the dataset with its outcome (computed once). The as-of view filters it by maturity."""
        if self._trades is None:
            rows = []
            hits = self.table[self.table["setup"].notna()]
            for (s, sym), name in zip(hits.index, hits["setup"]):
                o = self.trade_outcome(s, sym)
                rows.append({"session": s, "symbol": sym, "setup": name, "barrier": o.get("barrier"),
                             "y": outcome_class(o.get("barrier")), "net": o.get("net"), "gross": o.get("gross")})
            t = pd.DataFrame(rows, columns=["session", "symbol", "setup", "barrier", "y", "net", "gross"])
            if len(t):
                t = t.set_index(["session", "symbol"]).sort_index()
                t = t.drop(columns=["setup"]).join(self.table[list(FEATURES) + list(FACTORS) + ["regime", "setup"]], how="left")
            self._trades = t
        return self._trades


__all__ = ["LabelBook", "SETUPS", "setup_mask"]
