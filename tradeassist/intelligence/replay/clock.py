"""The as-of view: what could be known after a session's close, and nothing later (4.0).

The replay hands every model, trainer and grader an `AsOf` for the session it is on, never the dataset. Reading past
the cutoff raises `LookaheadError` instead of returning a number:

- `rows()`: the eligible stocks' features from the session's bar and earlier;
- `daily_examples(h)` / `trade_examples()`: training rows whose outcome had matured by the session's close;
- `close(symbol, session)`: a closing price, refused for any session after the cutoff.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from ..data.calendar import Calendar
from ..data.features import Panel
from .labels import LabelBook


def _is_label(column: str) -> bool:
    return column.startswith("y") and column[1:].isdigit()


class LookaheadError(RuntimeError):
    """Something asked for data from after the cutoff."""


class AsOf:
    def __init__(self, panel: Panel, labels: LabelBook, calendar: Calendar, session: date):
        if session not in calendar:
            raise LookaheadError(f"{session} is not a session")
        self.panel, self.labels, self.cal, self.session = panel, labels, calendar, session
        self.ts = pd.Timestamp(session)

    def _guard(self, d: pd.Timestamp | date) -> pd.Timestamp:
        ts = pd.Timestamp(d)
        if ts > self.ts:
            raise LookaheadError(f"Asked for {ts.date()} from a view as of {self.session}: that is after the cutoff.")
        return ts

    def rows(self) -> pd.DataFrame:
        return self.panel.rows(self.ts)

    def regime(self) -> str:
        return str(self.panel.regime.get(self.ts, "unknown"))

    def close(self, symbol: str, d: pd.Timestamp | date) -> float | None:
        ts = self._guard(d)
        v = self.panel.close.at[ts, symbol] if symbol in self.panel.close.columns else None
        return None if v is None or pd.isna(v) else float(v)

    def setups(self) -> pd.DataFrame:
        """Today's qualifying setups, without a single label column (every one of them reads later prices)."""
        out = self.labels.setups_on(self.ts)
        return out.drop(columns=[c for c in out.columns if _is_label(c)])

    def daily_examples(self, h: int, window_sessions: int) -> pd.DataFrame:
        """Rows whose h-session outcome had closed by this session's close, from the last `window_sessions`
        as-of sessions at most."""
        last = self.labels.matured_through(h, self.ts)
        if last is None:
            return self.labels.table.iloc[0:0]
        i = self.labels.pos[last]
        first = self.panel.sessions[max(0, i - window_sessions + 1)]
        out = self.labels.daily(h, first, last)
        assert out.index.get_level_values("session").max() <= last if len(out) else True
        # Only this horizon's label: a longer horizon's label on the latest rows has not matured yet.
        return out.drop(columns=[c for c in out.columns if _is_label(c) and c != f"y{h}"])

    def trade_examples(self, window_sessions: int) -> pd.DataFrame:
        last = self.labels.matured_through(self.labels.trade.hold_sessions, self.ts)
        t = self.labels.trades()
        if last is None or not len(t):
            return t.iloc[0:0] if len(t) else t
        i = self.labels.pos[last]
        first = self.panel.sessions[max(0, i - window_sessions + 1)]
        out = t.loc[(slice(first, last), slice(None)), :]
        return out[out["y"].notna()]


__all__ = ["AsOf", "LookaheadError"]
