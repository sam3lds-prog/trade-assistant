import random

import pytest

from tradeassist.pricer import TickRule, bucket_for, build_ladder, infer_tick_rule, to_cents, valid_prices

PENNY = TickRule(1, 5, "penny class")
NICKEL = TickRule(5, 10, "nickel class")
ALL_PENNY = TickRule(1, 1, "penny everywhere")


def prices(ladder):
    return {r["name"]: r["price"] for r in ladder["rungs"]}


def is_valid(price, rule):
    cents = to_cents(price)
    return cents % rule.tick_at(cents) == 0


def test_tick_rule_inference():
    penny = infer_tick_rule([0.52, 1.10, 3.05, 4.40])
    assert (penny.low, penny.high) == (1, 5)
    nickel = infer_tick_rule([0.50, 1.10, 3.10, 4.40, None, 0])
    assert (nickel.low, nickel.high) == (5, 10)
    everywhere = infer_tick_rule([0.52, 3.07])
    assert (everywhere.low, everywhere.high) == (1, 1)
    only_low = infer_tick_rule([0.52, 0.61])
    assert (only_low.low, only_low.high) == (1, 5)
    only_high_dimes = infer_tick_rule([4.10, 5.20])
    assert (only_high_dimes.low, only_high_dimes.high) == (5, 10)   # no evidence of a finer tick: stay coarse
    assert only_high_dimes.assumed and not penny.assumed and not everywhere.assumed


def test_tick_rule_is_always_one_of_the_three_real_schemes():
    """Evidence on one side of $3 settles the other side too: (1, 10) and (5, 5) do not exist."""
    pennies_below = infer_tick_rule([0.52, 1.10, 3.10, 4.40])        # high quotes all happen to be dimes
    assert (pennies_below.low, pennies_below.high) == (1, 5)
    nickels_above = infer_tick_rule([0.50, 1.10, 3.05, 4.40])        # low quotes all happen to be nickels
    assert (nickels_above.low, nickels_above.high) == (1, 5)
    rng = random.Random(7)
    for _ in range(2000):
        quotes = [round(rng.choice([0.01, 0.05, 0.10]) * rng.randrange(1, 90), 2) for _ in range(rng.randrange(0, 8))]
        rule = infer_tick_rule(quotes)
        assert (rule.low, rule.high) in {(1, 5), (5, 10), (1, 1)}


def test_one_tick_message_is_not_claimed_on_an_assumed_scheme():
    assumed = infer_tick_rule([3.10, 3.20])
    ladder = build_ladder(3.10, 3.20, assumed)
    assert not any("one tick wide" in f["text"] for f in ladder["flags"])
    assert any("finer" in f["text"] for f in ladder["flags"])
    proven = infer_tick_rule([0.52, 3.10, 3.20])
    assert 3.15 in [r["price"] for r in build_ladder(3.10, 3.20, proven)["rungs"]]


def test_valid_prices_change_increment_at_three_dollars():
    assert valid_prices(295, 310, PENNY) == [295, 296, 297, 298, 299, 300, 305, 310]
    assert valid_prices(290, 320, NICKEL) == [290, 295, 300, 310, 320]
    assert valid_prices(297, 320, NICKEL) == [300, 310, 320]
    assert valid_prices(301, 312, PENNY) == [305, 310]


def test_plan_example_twenty_cent_spread():
    """2.10: with nothing but the quote, the fair price is the midpoint; patient sits below it, the walk stops at
    it, and the fast price is only 30% of the way on to the ask (it used to be 80%)."""
    ladder = build_ladder(2.40, 2.60, PENNY)
    assert prices(ladder) == {"patient": 2.46, "balanced": 2.50, "fast": 2.53}
    assert ladder["mid"] == 2.50 and ladder["spread"] == pytest.approx(0.20)
    assert ladder["walk"]["start"] == 2.46 and ladder["walk"]["cap"] == 2.50
    assert ladder["fair"]["anchor"] == 2.50 and ladder["fair"]["used"] == 1
    rung = ladder["rungs"][0]
    assert rung["saving_vs_crossing"] == 14          # fourteen cents under the ask on one contract is $14


def test_savings_scale_with_contracts():
    ladder = build_ladder(2.40, 2.60, PENNY, contracts=5)
    assert ladder["rungs"][0]["saving_vs_crossing"] == 70


def test_nickel_spread_snaps_to_valid_prices():
    ladder = build_ladder(4.00, 4.40, TickRule(1, 5, ""))
    assert prices(ladder) == {"patient": 4.10, "balanced": 4.20, "fast": 4.30}


def test_one_tick_spread_offers_join_or_cross():
    ladder = build_ladder(0.10, 0.11, PENNY)
    assert [r["price"] for r in ladder["rungs"]] == [0.10, 0.11]
    assert any("one tick" in f["text"] for f in ladder["flags"])


def test_sell_side_mirrors_buy_side():
    ladder = build_ladder(2.40, 2.60, PENNY, side="sell")
    assert prices(ladder) == {"patient": 2.54, "balanced": 2.50, "fast": 2.47}
    assert ladder["walk"]["steps"][0] == 2.54 and ladder["walk"]["steps"][-1] == 2.50
    assert ladder["rungs"][0]["saving_vs_crossing"] == 14


def test_model_value_below_mid_lowers_the_fair_price():
    ladder = build_ladder(2.40, 2.60, PENNY, model_value=2.44)
    # 2.44 is 0.3 of the spread under the midpoint: one side looks stale, so the model value is the fair price.
    assert prices(ladder) == {"patient": 2.42, "balanced": 2.44, "fast": 2.49}
    assert ladder["fair"]["below_mid"] and ladder["fair"]["stale_quote"] and ladder["walk"]["cap"] == 2.44
    near = build_ladder(2.40, 2.60, PENNY, model_value=2.47)      # 0.15 of the spread: both count
    assert near["fair"]["anchor"] == pytest.approx(2.485) and not near["fair"]["stale_quote"]
    assert any(f["level"] == "warn" and "stale" in f["text"] for f in ladder["flags"])


def test_model_value_outside_quote_is_flagged_and_not_used():
    ladder = build_ladder(2.40, 2.60, PENNY, model_value=2.90)
    assert prices(ladder)["balanced"] == 2.50
    assert len(ladder["flags"]) == 1 and "outside the bid-ask range" in ladder["flags"][0]["text"]
    model = next(e for e in ladder["fair"]["evidence"] if e["key"] == "model")
    assert model["used"] is False


def test_sub_tick_model_difference_is_ignored():
    ladder = build_ladder(4.05, 4.15, PENNY, model_value=4.14)
    assert ladder["flags"] == []


def test_degenerate_quotes():
    assert build_ladder(2.0, None, PENNY)["ok"] is False
    assert build_ladder(2.0, 0, PENNY)["ok"] is False
    crossed = build_ladder(2.10, 2.00, PENNY)
    assert crossed["ok"] is False and "stale or crossed" in crossed["flags"][0]["text"]
    locked = build_ladder(2.00, 2.00, PENNY)
    assert locked["ok"] and [r["price"] for r in locked["rungs"]] == [2.00]
    no_bid = build_ladder(0.0, 0.05, PENNY)
    assert no_bid["ok"] and all(r["price"] > 0 for r in no_bid["rungs"])
    assert build_ladder(0.0, 0.05, PENNY, side="sell")["ok"] is False
    with pytest.raises(ValueError):
        build_ladder(1, 2, PENNY, side="hold")


def test_every_suggestion_is_a_valid_price_inside_the_quote():
    rng = random.Random(42)
    for _ in range(3000):
        rule = rng.choice([PENNY, NICKEL, ALL_PENNY])
        bid_c = rng.randrange(0, 900)
        bid_c -= bid_c % rule.tick_at(bid_c)
        ask_c = bid_c + rng.randrange(0, 120)
        ask_c += (-ask_c) % rule.tick_at(ask_c)
        if ask_c == 0:
            continue
        model = rng.choice([None, (bid_c + rng.uniform(-0.3, 1.3) * (ask_c - bid_c)) / 100])
        for side in ("buy", "sell"):
            if side == "sell" and bid_c == 0:
                continue
            ladder = build_ladder(bid_c / 100, ask_c / 100, rule, side=side, model_value=model)
            assert ladder["ok"]
            seq = [r["price"] for r in ladder["rungs"]]
            for p in seq + ladder["walk"]["steps"]:
                assert is_valid(p, rule), (bid_c, ask_c, rule, side, p)
                assert bid_c / 100 - 1e-9 <= p <= ask_c / 100 + 1e-9
            assert seq == sorted(seq, reverse=(side == "sell"))        # ladder moves toward the far side
            assert ladder["walk"]["steps"] == sorted(ladder["walk"]["steps"], reverse=(side == "sell"))


def test_calibration_is_attached_to_rungs():
    cal = {"buckets": [{"label": "a", "attempts": 12, "fill_rate": 0.25},
                       {"label": "b", "attempts": 0, "fill_rate": None},
                       {"label": "c", "attempts": 20, "fill_rate": 0.9},
                       {"label": "d", "attempts": 3, "fill_rate": 1.0}]}
    ladder = build_ladder(2.40, 2.60, PENNY, calibration=cal)
    rates = {r["name"]: r["observed_fill_rate"] for r in ladder["rungs"]}
    assert rates["patient"]["fill_rate"] == 0.25        # 30% across the spread
    assert rates["balanced"]["fill_rate"] == 0.25       # the midpoint: up to the midpoint
    assert rates["fast"] is None                        # 65%: a bucket with no attempts yet


def test_bucket_edges():
    assert [bucket_for(p) for p in (0.0, 0.5, 0.51, 0.65, 0.66, 0.8, 0.81, 1.0, 1.4)] == [0, 0, 1, 1, 2, 2, 3, 3, 3]


# --- 2.10: the fair price -------------------------------------------------------------------------------------

def test_evidence_lowers_a_buyers_price_but_never_raises_it():
    from tradeassist.pricer import fair_value
    up = fair_value(2.40, 2.60, "buy", model_value=2.54)
    assert up["fair"] == pytest.approx(2.52) and up["anchor"] == 2.50 and not up["below_mid"]
    assert "starts from the midpoint" in up["basis"]
    seller = fair_value(2.40, 2.60, "sell", model_value=2.54)
    assert seller["anchor"] == pytest.approx(2.52)          # a seller may be told to ask more than the midpoint
    stale_up = fair_value(2.40, 2.60, "buy", model_value=2.58)
    assert stale_up["fair"] == pytest.approx(2.58) and stale_up["anchor"] == 2.50   # never above the midpoint
    down = fair_value(2.40, 2.60, "sell", model_value=2.42)
    assert down["anchor"] == 2.50                           # ...never less


def test_size_weighted_midpoint_leans_toward_the_thin_side():
    from tradeassist.pricer import fair_value
    f = fair_value(2.40, 2.60, "buy", bidsize=5, asksize=80)       # few buyers, many sellers: worth less
    micro = next(e for e in f["evidence"] if e["key"] == "size_weighted")
    assert micro["price"] == pytest.approx((2.40 * 80 + 2.60 * 5) / 85)
    assert f["anchor"] == pytest.approx((2.50 + micro["price"]) / 2)
    assert fair_value(2.40, 2.41, "buy", bidsize=5, asksize=80)["used"] == 1   # a one-tick spread: nothing to weigh


def test_a_trade_counts_only_when_recent_inside_the_quote_and_the_market_is_open():
    from tradeassist.pricer import fair_value
    keys = lambda f: {e["key"]: e["used"] for e in f["evidence"]}        # noqa: E731
    assert keys(fair_value(2.40, 2.60, last=2.44, last_age_seconds=60))["last_trade"] is True
    assert keys(fair_value(2.40, 2.60, last=2.44, last_age_seconds=3600))["last_trade"] is False
    assert keys(fair_value(2.40, 2.60, last=2.44, last_age_seconds=None))["last_trade"] is False
    assert keys(fair_value(2.40, 2.60, last=2.70, last_age_seconds=60))["last_trade"] is False
    assert keys(fair_value(2.40, 2.60, last=2.44, last_age_seconds=60, quote_live=False))["last_trade"] is False
    assert fair_value(2.40, 2.60, last=2.44, last_age_seconds=60)["anchor"] == pytest.approx(2.47)


def test_robinhoods_fill_estimates_are_shown_for_the_side_and_never_used():
    from tradeassist.pricer import fair_value
    fills = {"buy_likely": 2.55, "buy_less_likely": 2.45, "sell_likely": 2.42}
    f = fair_value(2.40, 2.60, "buy", fill_prices=fills)
    shown = [e for e in f["evidence"] if e["key"].startswith("robinhood_")]
    assert {e["key"] for e in shown} == {"robinhood_buy_likely", "robinhood_buy_less_likely"}
    assert not any(e["used"] for e in shown) and f["anchor"] == 2.50


def test_no_usable_quote_gives_no_fair_price():
    from tradeassist.pricer import fair_value
    assert fair_value(2.0, None) is None and fair_value(2.1, 2.0) is None and fair_value(None, 0) is None
    assert fair_value(None, 0.05)["anchor"] == pytest.approx(0.025)


def test_the_buyers_walk_never_passes_the_fair_price_or_the_midpoint():
    rng = random.Random(2010)
    for _ in range(3000):
        rule = rng.choice([PENNY, NICKEL, ALL_PENNY])
        bid_c = rng.randrange(0, 900)
        bid_c -= bid_c % rule.tick_at(bid_c)
        ask_c = bid_c + rng.randrange(0, 120)
        ask_c += (-ask_c) % rule.tick_at(ask_c)
        if ask_c == 0:
            continue
        span = ask_c - bid_c
        kw = {"model_value": rng.choice([None, (bid_c + rng.uniform(-0.3, 1.3) * span) / 100]),
              "bidsize": rng.choice([None, rng.randrange(1, 500)]), "asksize": rng.choice([None, rng.randrange(1, 500)]),
              "last": rng.choice([None, (bid_c + rng.uniform(-0.2, 1.2) * span) / 100]),
              "last_age_seconds": rng.choice([None, 30, 5000])}
        for side in ("buy", "sell"):
            if side == "sell" and bid_c == 0:
                continue
            ladder = build_ladder(bid_c / 100, ask_c / 100, rule, side=side, **kw)
            mid = (bid_c + ask_c) / 200
            seq = [r["price"] for r in ladder["rungs"]]
            for p in seq + ladder["walk"]["steps"]:
                assert is_valid(p, rule) and bid_c / 100 - 1e-9 <= p <= ask_c / 100 + 1e-9
            assert seq == sorted(seq, reverse=(side == "sell"))
            fair = ladder["fair"]["anchor"]
            if side == "buy":
                assert fair <= mid + 1e-9
                if ladder["fair"]["limit"] is None:       # no valid price at or below the estimate: said so
                    assert bid_c == 0 and any("only price to pay is the ask" in f["text"] for f in ladder["flags"])
                else:
                    assert max(ladder["walk"]["steps"]) <= fair + 1e-9
            else:
                assert fair >= mid - 1e-9
                assert min(ladder["walk"]["steps"]) >= fair - 1e-9


def test_a_stale_side_leaves_the_midpoint_and_the_size_weighting_out():
    from tradeassist.pricer import fair_value
    f = fair_value(2.40, 2.80, "buy", model_value=2.45, bidsize=40, asksize=10, last=2.47, last_age_seconds=30)
    used = {e["key"]: e["used"] for e in f["evidence"]}
    assert used == {"midpoint": False, "model": True, "size_weighted": False, "last_trade": True}
    assert f["stale_quote"] and f["anchor"] == pytest.approx(2.46) and "left out" in f["basis"]
    alone = fair_value(2.40, 2.80, "buy", model_value=2.45)
    assert alone["anchor"] == pytest.approx(2.45) and "only evidence" in alone["basis"]


def test_no_bid_and_a_one_step_offer_says_the_ask_is_the_only_price():
    ladder = build_ladder(0.0, 0.05, NICKEL)
    assert ladder["fair"]["limit"] is None and [r["price"] for r in ladder["rungs"]] == [0.05]
    assert any("only price to pay is the ask" in f["text"] for f in ladder["flags"])
    off_grid = build_ladder(0.0, 0.02, NICKEL)                # quotes off the class's increments
    assert all(r["price"] > 0 for r in off_grid["rungs"])
