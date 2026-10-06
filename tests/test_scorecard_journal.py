import math
from datetime import date, timedelta

import pytest

from tradeassist.chain import analyze_chain, atm_iv
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.indicators import enrich
from tradeassist.journal import Journal, JournalError
from tradeassist.optionmath import black76_price
from tradeassist.pricer import build_ladder, infer_tick_rule
from tradeassist.providers.demo import DemoData
from tradeassist.scorecard import build_scorecard
from tests.conftest import random_walk

TODAY = date(2026, 10, 5)


@pytest.fixture(scope="module")
def setup():
    demo = DemoData(today=TODAY, with_greeks=False)
    expiration = "2026-11-20"
    spot = demo.quotes(["AAPL"])["AAPL"]["last"]
    analysis = analyze_chain("AAPL", expiration, demo.chain("AAPL", expiration), spot, 0.04)
    enriched = enrich(demo.history("AAPL", "2023-01-01", "2026-12-31"))
    row = min((c for c in analysis.contracts if c["option_type"] == "call"), key=lambda c: abs(c["strike"] - spot))
    return analysis, enriched, row


def card(setup, settings=None, **kw):
    analysis, enriched, row = setup
    row = {**row, **kw.pop("row", {})}
    args = dict(limit_price=row["mid"], contracts=1, earnings={"date": "2027-01-28", "estimated": False, "source": "t"},
                today=TODAY)
    args.update(kw)
    return build_scorecard(row, analysis, enriched, settings or Settings(account_size=100_000), **args)


def status(result, key):
    return next(c for c in result["checks"] if c["key"] == key)


def test_chain_analysis_without_provider_greeks(setup):
    analysis, _, row = setup
    assert analysis.forward_method == "parity" and analysis.smile is not None
    assert row["greeks_source"] == "computed" and row["iv_source"] == "computed"
    assert 0.3 < row["delta"] < 0.7 and row["theta"] < 0
    assert row["bid"] <= row["model_value"] + 0.25 and row["model_value"] <= row["ask"] + 0.25
    assert 0.1 < atm_iv(analysis) < 0.6
    # Recompute the model value from the textbook formula with scipy, not the app's own pricer.
    from scipy.stats import norm
    f, k, t, vol = analysis.forward, row["strike"], analysis.t, row["model_vol"]
    d1 = (math.log(f / k) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))
    textbook = math.exp(-0.04 * t) * (f * norm.cdf(d1) - k * norm.cdf(d1 - vol * math.sqrt(t)))
    assert row["model_value"] == pytest.approx(textbook, abs=1e-9)
    assert abs(row["model_vol"] - row["iv"]) < 0.02                 # the fitted smile passes close to this quote


def test_zero_filled_provider_greeks_are_replaced_by_computed_ones():
    demo = DemoData(today=TODAY, with_greeks=True)
    spot = demo.quotes(["AAPL"])["AAPL"]["last"]
    raw = demo.chain("AAPL", "2026-11-20")
    target = min((c for c in raw if c["option_type"] == "call"), key=lambda c: abs(c["strike"] - spot))
    target["greeks"] = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "mid_iv": 0.0, "smv_vol": 0.0}
    row = analyze_chain("AAPL", "2026-11-20", raw, spot, 0.04).find(target["symbol"])
    assert row["greeks_source"] == "computed" and 0.3 < row["delta"] < 0.7 and row["theta"] < 0
    assert row["iv_source"] == "computed" and row["model_vol"] > 0.05
    target["greeks"] = {"delta": 0.5, "gamma": 0.02, "theta": 0.0, "vega": 0.2, "mid_iv": 0.25, "smv_vol": 0.25}
    row = analyze_chain("AAPL", "2026-11-20", raw, spot, 0.04).find(target["symbol"])
    assert row["greeks_source"] == "computed" and row["theta"] < 0           # zero decay is a placeholder too


@pytest.mark.parametrize("with_greeks", [False, True])
def test_stale_offer_is_caught_and_the_ladder_starts_lower(with_greeks):
    """Push one contract's ask up, as a stale quote would be. Its midpoint rises but the value
    implied by neighbouring strikes does not, so the ladder should start from the model value."""
    demo = DemoData(today=TODAY, with_greeks=with_greeks)
    spot = demo.quotes(["AAPL"])["AAPL"]["last"]
    raw = demo.chain("AAPL", "2026-11-20")
    target = min((c for c in raw if c["option_type"] == "call" and c["strike"] > spot * 1.04), key=lambda c: c["strike"])
    clean = analyze_chain("AAPL", "2026-11-20", raw, spot, 0.04).find(target["symbol"])
    assert abs(clean["model_value"] - clean["mid"]) < 0.03                     # before: model agrees with the quote

    target["ask"] = round(target["ask"] + 0.40, 2)
    row = analyze_chain("AAPL", "2026-11-20", raw, spot, 0.04).find(target["symbol"])
    assert row["mid"] == pytest.approx(clean["mid"] + 0.20)
    assert abs(row["model_value"] - clean["model_value"]) < 0.02               # one bad quote barely moves the fit
    rule = infer_tick_rule(q for c in raw for q in (c["bid"], c["ask"]))
    ladder = build_ladder(row["bid"], row["ask"], rule, model_value=row["model_value"])
    patient = ladder["rungs"][0]
    assert patient["price"] < ladder["mid"] - 0.10 and patient["price"] <= row["model_value"] + 0.005
    assert any("stale" in f["text"] for f in ladder["flags"])


def test_clean_trade_has_no_red_flags(setup):
    result = card(setup)
    assert result["verdict"] in ("pass", "warn") and result["blocking"] == []
    assert status(result, "trade_risk")["status"] == "pass"
    assert 0 < result["odds"]["prob_profit_at_expiry"] < 0.5
    assert result["odds"]["breakeven"] == pytest.approx(setup[2]["strike"] + setup[2]["mid"])


def test_sizing_limits_block(setup):
    small = Settings(account_size=5_000, max_risk_per_trade_pct=2.0)
    result = card(setup, settings=small)
    risk = status(result, "trade_risk")
    assert risk["status"] == "fail" and risk["blocking"] and "trade_risk" in result["blocking"]
    assert result["max_contracts"] == 0 and result["verdict"] == "fail"
    result = card(setup, open_risk=9_900.0)
    assert status(result, "open_risk")["status"] == "fail"


def test_a_trade_exactly_at_the_cap_passes(setup):
    """1.10 x 100 x 5 is 550.0000000000001 in floating point; a $550 cap must still allow it."""
    exact = Settings(account_size=27_500, max_risk_per_trade_pct=2.0)
    result = card(setup, settings=exact, limit_price=1.10, contracts=5)
    assert status(result, "trade_risk")["status"] == "pass" and result["max_contracts"] == 5
    result = card(setup, settings=exact, limit_price=0.55, contracts=10)
    assert status(result, "trade_risk")["status"] == "pass" and result["max_contracts"] == 10
    assert status(card(setup, settings=exact, limit_price=1.11, contracts=5), "trade_risk")["status"] == "fail"


def test_existing_risk_in_the_same_stock_counts_toward_the_per_trade_limit(setup):
    settings = Settings(account_size=50_000, max_risk_per_trade_pct=2.0)     # $1,000 per idea
    alone = card(setup, settings=settings, limit_price=3.00, contracts=1)
    assert status(alone, "trade_risk")["status"] == "pass" and alone["max_contracts"] == 3
    stacked = card(setup, settings=settings, limit_price=3.00, contracts=1, symbol_risk=800.0)
    check = status(stacked, "trade_risk")
    assert check["status"] == "fail" and check["value"].startswith("$1,100") and stacked["max_contracts"] == 0
    assert "$800 already open or planned in AAPL" in check["detail"]
    assert status(card(setup, settings=settings, limit_price=2.00, contracts=1, symbol_risk=800.0),
                  "trade_risk")["status"] == "pass"


def test_put_breakeven_and_direction(setup):
    analysis, enriched, _ = setup
    put = min((c for c in analysis.contracts if c["option_type"] == "put"), key=lambda c: abs(c["strike"] - analysis.spot))
    result = build_scorecard(put, analysis, enriched, Settings(account_size=100_000), put["mid"], 1, today=TODAY)
    assert result["odds"]["breakeven"] == pytest.approx(put["strike"] - put["mid"])
    check = status(result, "breakeven")
    assert "fall" in check["detail"] and check["value"].startswith("-")     # shown as the stock's move: down
    call_check = status(card(setup), "breakeven")
    assert call_check["value"].startswith("+")


@pytest.mark.parametrize("earnings,expected", [
    (None, "warn"),
    ({"date": "2026-11-10", "estimated": False, "source": "x"}, "fail"),      # before expiration
    ({"date": "2026-11-20", "estimated": False, "source": "x"}, "fail"),      # on expiration day
    ({"date": "2026-11-21", "estimated": False, "source": "x"}, "pass"),      # the day after
    ({"date": "2026-11-27", "estimated": True, "source": "x"}, "fail"),       # estimate within 10 days after
    ({"date": "2026-12-15", "estimated": True, "source": "x"}, "pass"),
    ({"date": "2026-09-01", "estimated": False, "source": "x"}, "warn"),      # stale date
    ({"date": "2026-10-01", "estimated": True, "source": "x"}, "fail"),       # estimate just passed: may still be ahead
    ({"date": "2026-09-01", "estimated": True, "source": "x"}, "warn"),
    ({"date": "2026-09-20", "estimated": True, "overdue": True, "source": "x"}, "fail"),   # late report: any day
])
def test_earnings_check(setup, earnings, expected):
    assert status(card(setup, earnings=earnings), "earnings")["status"] == expected


def test_wide_spread_short_dated_and_missing_quote(setup):
    analysis, enriched, row = setup
    wide = {"bid": 1.0, "ask": 1.5, "mid": 1.25, "spread": 0.5, "spread_pct": 40.0}
    assert status(card(setup, row=wide), "spread")["blocking"]
    assert status(card(setup, row={"spread_pct": None}), "spread")["status"] == "fail"
    near = {**row, "expiration": (TODAY + timedelta(days=3)).isoformat()}
    result = build_scorecard(near, analysis, enriched, Settings(account_size=100_000), row["mid"], 1, today=TODAY)
    assert status(result, "dte")["blocking"] and result["dte"] == 3


def test_iv_rank_needs_history_and_a_record_from_today(setup):
    assert status(card(setup, iv_rank={"days": 12, "rank": 80.0, "fresh": True}), "iv_rank")["status"] == "info"
    fresh = {"days": 200, "fresh": True, "as_of": "2026-10-05"}
    assert status(card(setup, iv_rank={**fresh, "rank": 85.0}), "iv_rank")["status"] == "warn"
    assert status(card(setup, iv_rank={**fresh, "rank": 20.0}), "iv_rank")["status"] == "pass"
    stale = status(card(setup, iv_rank={"days": 200, "rank": 0.0, "fresh": False, "as_of": "2026-08-21"}), "iv_rank")
    assert stale["status"] == "info" and "2026-08-21" in stale["value"]     # an old record is never ranked as current


def new_trade(journal, **kw):
    args = dict(data_source="demo", symbol="AAPL", option_symbol="AAPL261120C00210000", option_type="call",
                strike=210.0, expiration="2026-11-20", contracts=2, thesis="test thesis text", planned_limit=2.50,
                max_limit=2.56, plan={"stop_price": 1.28}, quote={"bid": 2.40, "ask": 2.60}, scorecard=[],
                signals=[{"name": "Hammer", "key": "hammer"}], overrides=[])
    args.update(kw)
    return journal.create(**args)


def test_journal_lifecycle_and_profit():
    j = Journal(Database(":memory:"))
    tid = new_trade(j)
    assert j.get(tid)["status"] == "planned" and j.open_risk() == 0
    j.log_attempt(tid, side="buy", bid=2.40, ask=2.60, limit_price=2.50, filled=False)
    t = j.log_attempt(tid, side="buy", bid=2.40, ask=2.60, limit_price=2.54, filled=True, seconds_to_fill=20)
    assert t["status"] == "open" and t["entry_price"] == 2.54 and len(t["attempts"]) == 2
    assert t["attempts"][1]["position"] == pytest.approx(0.7)
    assert j.open_risk() == pytest.approx(2.54 * 100 * 2)
    with pytest.raises(JournalError):
        j.log_attempt(tid, side="buy", bid=2.4, ask=2.6, limit_price=2.5, filled=True)   # already open
    t = j.log_attempt(tid, side="sell", bid=3.90, ask=4.10, limit_price=4.00, filled=True, seconds_to_fill=5)
    assert t["status"] == "closed" and t["pnl"] == pytest.approx((4.00 - 2.54) * 200)
    assert t["attempts"][2]["position"] == pytest.approx(0.5)                             # sell side is mirrored
    assert j.open_risk() == 0 and j.realized_today() == pytest.approx(292.0)
    stats = j.stats()
    assert stats["overall"]["trades"] == 1 and stats["overall"]["win_rate"] == 1.0
    assert stats["by_signal"][0]["signal"] == "Hammer" and stats["followed_rules"]["trades"] == 1


def test_saved_plans_count_as_committed_risk_and_fills_above_plan_are_marked():
    j = Journal(Database(":memory:"))
    first = new_trade(j)                                   # plan allows up to 2.56 on 2 contracts
    second = new_trade(j, contracts=1, max_limit=4.00)
    assert j.open_risk() == 0 and j.planned_risk() == pytest.approx(2.56 * 200 + 400)
    assert j.committed_risk() == pytest.approx(912.0)
    t = j.log_attempt(first, side="buy", bid=2.40, ask=2.60, limit_price=2.55, filled=True)
    assert t["overrides"] == []                             # filled inside the plan
    assert j.committed_risk() == pytest.approx(2.55 * 200 + 400)
    t = j.log_attempt(second, side="buy", bid=4.00, ask=4.40, limit_price=4.40, filled=True)
    assert t["overrides"] == ["paid_above_plan"] and t["entry_price"] == 4.40   # recorded truthfully, and flagged
    j.cancel(new_trade(j))
    assert j.planned_risk() == 0                            # cancelled plans stop counting
    j.close_at(second, 0.0)
    assert j.stats()["overrode_a_limit"]["trades"] == 1


def test_journal_expired_worthless_cancel_and_validation():
    j = Journal(Database(":memory:"))
    tid = new_trade(j, overrides=["earnings"])
    with pytest.raises(JournalError):
        j.close_at(tid, 0.0)                                                              # not open yet
    j.log_attempt(tid, side="buy", bid=2.40, ask=2.60, limit_price=2.60, filled=True)
    t = j.close_at(tid, 0.0, "expired")
    assert t["pnl"] == pytest.approx(-520.0) and "expired" in t["notes"]
    assert j.stats()["overrode_a_limit"]["total_pnl"] == pytest.approx(-520.0)
    tid2 = new_trade(j)
    assert j.cancel(tid2)["status"] == "cancelled"
    with pytest.raises(JournalError):
        j.cancel(tid2)
    with pytest.raises(JournalError):
        j.get(999)
    with pytest.raises(JournalError):
        new_trade(j, contracts=0)
    tid3 = new_trade(j)
    with pytest.raises(JournalError):
        j.log_attempt(tid3, side="buy", bid=2.6, ask=2.4, limit_price=2.5, filled=False)  # crossed quote
    with pytest.raises(JournalError):
        j.log_attempt(tid3, side="sell", bid=2.4, ask=2.6, limit_price=2.5, filled=False) # nothing to sell yet
    with pytest.raises(JournalError, match="above the ask"):
        j.log_attempt(tid3, side="buy", bid=2.4, ask=2.6, limit_price=26.0, filled=True)  # a slipped decimal point
    old_plan = new_trade(j, expiration="2020-01-17")
    assert j.get(old_plan)["status"] == "planned"
    assert j.planned_risk() == pytest.approx(2.56 * 200)                                  # only tid3: expired plans drop out


def test_calibration_suggests_the_cheapest_zone_that_fills():
    j = Journal(Database(":memory:"))
    for i in range(12):
        tid = new_trade(j)
        j.log_attempt(tid, side="buy", bid=2.40, ask=2.60, limit_price=2.50, filled=(i < 4),
                      seconds_to_fill=[10, 30, 50, 90][i] if i < 4 else None)
        if i >= 4:
            j.log_attempt(tid, side="buy", bid=2.40, ask=2.60, limit_price=2.55, filled=(i < 11), seconds_to_fill=10 + i)
    cal = j.calibration("buy")
    mid, upper = cal["buckets"][0], cal["buckets"][2]
    assert mid["attempts"] == 12 and mid["fill_rate"] == pytest.approx(4 / 12)
    assert mid["median_seconds"] == 40                                                    # median of 10, 30, 50, 90
    assert upper["attempts"] == 8 and upper["fill_rate"] == pytest.approx(7 / 8)
    assert cal["total_attempts"] == 20 and "needs 10 attempts" in cal["note"]             # 8 tries is not enough yet
    for _ in range(2):
        tid = new_trade(j)
        j.log_attempt(tid, side="buy", bid=2.40, ask=2.60, limit_price=2.55, filled=True, seconds_to_fill=3)
    assert "65% to 80%" in j.calibration("buy")["note"]
    assert j.calibration("sell")["total_attempts"] == 0
