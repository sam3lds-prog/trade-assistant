"""Property tests for the position ledger (positions.account).

For any valid history of fills, long or short, stock or option: what is open equals what was
opened less what was closed; once nothing is open, realized profit equals the cash in less the
cash out less fees, exactly; every figure is finite. Any NaN or infinity anywhere is refused,
and so is any fill after the position has closed. Hypothesis runs derandomized, so a failure
reproduces.
"""

from __future__ import annotations

import math

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from tradeassist.positions import Fill, PositionError, account  # noqa: E402

PRICES = st.floats(min_value=0.0, max_value=5_000.0, allow_nan=False, allow_infinity=False)
FEES = st.floats(min_value=0.0, max_value=25.0, allow_nan=False, allow_infinity=False)
BAD = st.sampled_from([float("nan"), float("inf"), float("-inf")])
SETTINGS = settings(max_examples=300, derandomize=True, deadline=None)


@st.composite
def history(draw, close_at_end: bool = True):
    direction = draw(st.sampled_from(["LONG", "SHORT"]))
    mult = draw(st.sampled_from([1.0, 100.0, 10.0]))
    opening, closing = ("BUY", "SELL") if direction == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
    held, rows = 0, []
    for _ in range(draw(st.integers(1, 10))):
        if held and draw(st.booleans()):
            q = draw(st.integers(1, held - 1)) if held > 1 else None
            if q is None:
                continue                      # closing the last unit is left to the end
            held -= q
            rows.append((closing, q))
        else:
            q = draw(st.integers(1, 500))
            held += q
            rows.append((opening, q))
    if close_at_end:
        rows.append((closing, held))
        held = 0
    fills = [Fill(side, float(q), draw(PRICES), draw(FEES), f"2026-01-01T{10 + i // 60:02d}:{i % 60:02d}:00+00:00")
             for i, (side, q) in enumerate(rows)]
    return direction, mult, fills, held


@SETTINGS
@given(history(close_at_end=True))
def test_a_closed_position_realizes_exactly_its_cash_flows(h):
    direction, mult, fills, _ = h
    led = account(direction, mult, fills)
    cash = sum((1 if f.side in ("SELL", "SELL_SHORT") else -1) * f.quantity * f.price * mult - f.fees for f in fills)
    assert led.status() == "CLOSED" and led.open_quantity == 0.0
    assert led.realized_pnl == pytest.approx(cash, rel=1e-9, abs=1e-6)
    assert led.fees == pytest.approx(sum(f.fees for f in fills))


@SETTINGS
@given(history(close_at_end=False))
def test_open_quantity_and_every_figure_add_up(h):
    direction, mult, fills, held = h
    led = account(direction, mult, fills)
    assert led.open_quantity == pytest.approx(held)
    assert led.opened_quantity - led.closed_quantity == pytest.approx(held)
    for v in (led.realized_pnl, led.gross_realized, led.fees, led.cost_basis, led.average_entry_price):
        assert v is None or math.isfinite(v)
    if held:
        assert led.status() in ("OPEN", "PARTIALLY_CLOSED") and led.cost_basis is not None and led.cost_basis >= 0


@SETTINGS
@given(history(close_at_end=False), st.integers(0, 50), st.sampled_from(["quantity", "price", "fees"]), BAD)
def test_any_non_finite_number_is_refused(h, where, field, bad):
    direction, mult, fills, _ = h
    i = where % len(fills)
    f = fills[i]
    fills[i] = Fill(f.side, bad if field == "quantity" else f.quantity, bad if field == "price" else f.price,
                    bad if field == "fees" else f.fees, f.executed_at)
    with pytest.raises(PositionError):
        account(direction, mult, fills)


@SETTINGS
@given(history(close_at_end=True), st.booleans(), st.integers(1, 100), PRICES)
def test_nothing_is_accepted_after_a_full_close(h, opening, q, price):
    direction, mult, fills, _ = h
    side = {("LONG", True): "BUY", ("LONG", False): "SELL", ("SHORT", True): "SELL_SHORT",
            ("SHORT", False): "BUY_TO_COVER"}[(direction, opening)]
    with pytest.raises(PositionError):
        account(direction, mult, [*fills, Fill(side, float(q), price, 0.0, "2026-01-02T10:00:00+00:00")])
