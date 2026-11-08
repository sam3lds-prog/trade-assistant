"""Which stocks a replay predicts, and which funds it reads for context (4.0).

`large-caps-2026` is the scanner's fixed list of 100 large US companies as of mid-2026 (scanner.LARGE_CAPS). Used
for a replay back to 2015 it is survivorship-biased: it holds only companies that went on to be among the largest,
and none that fell out of the index. That is acceptable for comparing one model with another on the same names;
it overstates what any of them would have earned, and every scorecard says so.

A stock is eligible on session T only when it has a bar on T and at least MIN_HISTORY bars up to T, so a later
listing (PLTR in 2020) simply joins when it has a year of history.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...sectors import SECTOR_FUNDS, sector_fund

MIN_HISTORY = 260
MARKET = "SPY"

# scanner.LARGE_CAPS, copied: importing the scanner would pull in the data providers, which this package never touches
# (tests/test_v4_isolation.py). tests/test_v4_api.py keeps the two lists identical.
LARGE_CAPS_2026 = tuple((
    "AAPL MSFT NVDA AMZN GOOGL META BRK.B AVGO TSLA LLY JPM V UNH XOM MA COST HD PG JNJ WMT NFLX ABBV "
    "BAC CRM ORCL CVX KO MRK AMD PEP TMO ADBE LIN MCD CSCO ACN WFC ABT PM IBM GE DIS INTU CAT QCOM TXN "
    "VZ AMGN NOW ISRG GS DHR T CMCSA NEE PFE SPGI RTX UNP MS LOW HON AMAT BKNG BLK COP PLTR AXP C SCHW "
    "DE LMT GILD MDT BMY SBUX ADP MO MDLZ CL UPS BA USB SO DUK MMM EMR GD CVS TGT FDX F GM INTC AIG MET "
    "COF NKE PYPL TMUS"
).split())


@dataclass(frozen=True)
class Universe:
    name: str
    stocks: tuple[str, ...]
    context: tuple[str, ...]
    survivorship_biased: bool
    note: str

    @property
    def symbols(self) -> tuple[str, ...]:
        """Everything the dataset holds: the stocks, then the market and sector funds."""
        seen: dict[str, None] = {}
        for s in (*self.stocks, *self.context):
            seen.setdefault(s, None)
        return tuple(seen)

    def sector_of(self, symbol: str) -> str | None:
        fund = sector_fund(symbol)
        return fund if fund in self.context else None


CONTEXT = (MARKET, "QQQ", *SECTOR_FUNDS)

UNIVERSES: dict[str, Universe] = {
    "large-caps-2026": Universe(
        name="large-caps-2026", stocks=LARGE_CAPS_2026, context=CONTEXT, survivorship_biased=True,
        note="Today's 100 large US companies replayed back to 2015: survivorship-biased. Good for comparing models with "
             "each other on the same names; it overstates what any of them would have earned."),
}


def get(name: str) -> Universe:
    try:
        return UNIVERSES[name]
    except KeyError:
        raise ValueError(f"Unknown universe {name!r}; known: {', '.join(UNIVERSES)}") from None


def custom(name: str, stocks: list[str], survivorship_biased: bool = True, context: tuple[str, ...] = CONTEXT) -> Universe:
    """A universe for tests and small experiments (a dataset records it in full, so a run needs no registry entry)."""
    return Universe(name=name, stocks=tuple(stocks), context=tuple(context), survivorship_biased=survivorship_biased,
                    note="A custom list of stocks.")


__all__ = ["Universe", "UNIVERSES", "get", "custom", "MIN_HISTORY", "MARKET", "CONTEXT", "LARGE_CAPS_2026"]
