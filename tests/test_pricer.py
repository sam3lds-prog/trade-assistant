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
    ladder = build_ladder(2.40, 2.60, PENNY)
    assert prices(ladder) == {"patient": 2.50, "balanced": 2.53, "fast": 2.56}
    assert ladder["mid"] == 2.50 and ladder["spread"] == pytest.approx(0.20)
    assert ladder["walk"]["start"] == 2.50 and ladder["walk"]["cap"] == 2.56
    rung = ladder["rungs"][0]
    assert rung["saving_vs_crossing"] == 10          # ten cents under the ask on one contract is $10


def test_savings_scale_with_contracts():
    ladder = build_ladder(2.40, 2.60, PENNY, contracts=5)
    assert ladder["rungs"][0]["saving_vs_crossing"] == 50


def test_nickel_spread_snaps_to_valid_prices():
    ladder = build_ladder(4.00, 4.40, TickRule(1, 5, ""))
    assert prices(ladder) == {"patient": 4.20, "balanced": 4.25, "fast": 4.35}


def test_one_tick_spread_offers_join_or_cross():
    ladder = build_ladder(0.10, 0.11, PENNY)
    assert [r["price"] for r in ladder["rungs"]] == [0.10, 0.11]
    assert any("one tick" in f["text"] for f in ladder["flags"])


def test_sell_side_mirrors_buy_side():
    ladder = build_ladder(2.40, 2.60, PENNY, side="sell")
    assert prices(ladder) == {"patient": 2.50, "balanced": 2.47, "fast": 2.44}
    assert ladder["walk"]["steps"][0] == 2.50 and ladder["walk"]["steps"][-1] == 2.44
    assert ladder["rungs"][0]["saving_vs_crossing"] == 10


def test_model_value_below_mid_lowers_the_patient_price():
    ladder = build_ladder(2.40, 2.60, PENNY, model_value=2.44)
    assert prices(ladder)["patient"] == 2.44
    assert any(f["level"] == "warn" and "stale" in f["text"] for f in ladder["flags"])


def test_model_value_outside_quote_is_flagged_and_not_used():
    ladder = build_ladder(2.40, 2.60, PENNY, model_value=2.90)
    assert prices(ladder)["patient"] == 2.50
    assert len(ladder["flags"]) == 1 and "outside the bid-ask range" in ladder["flags"][0]["text"]


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
    assert rates["patient"]["fill_rate"] == 0.25
    assert rates["balanced"] is None
    assert rates["fast"]["attempts"] == 20


def test_bucket_edges():
    assert [bucket_for(p) for p in (0.0, 0.5, 0.51, 0.65, 0.66, 0.8, 0.81, 1.0, 1.4)] == [0, 0, 1, 1, 2, 2, 3, 3, 3]
