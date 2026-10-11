"""2.5: the pure helpers in static/flow.js, run under Node.

The contract cards on Decide, the reversal scan and the option picker call FLOW.pricingRequest to open
Price an option on the same contract; the thesis form calls FLOW.gradeRequest and FLOW.optionsRequest so
the stock grade and the option picker read one set of levels. These tests pin those mappings, and check
the requests they build are accepted by the server's request models.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[1] / "tradeassist" / "static" / "flow.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")

RUNNER = r"""
const fs = require("fs"), vm = require("vm");
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\n;globalThis.FLOW = FLOW;", ctx);
const calls = JSON.parse(process.argv[2]);
const out = calls.map(([fn, args]) => {
  try { return { ok: ctx.FLOW[fn](...args) }; } catch (e) { return { error: String(e.message) }; }
});
process.stdout.write(JSON.stringify(out));
"""


def run(*calls):
    done = subprocess.run(["node", "-e", RUNNER, str(FLOW), json.dumps(calls)],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


CARD = {"option_type": "CALL", "strike": 105.0, "expiration": "2026-11-20", "mid": 2.35, "delta": 0.41}


def test_a_contract_card_maps_to_the_price_request():
    [r] = run(["pricingRequest", ["aapl ", CARD, 3, "buy"]])
    assert r == {"ok": {"symbol": "AAPL", "expiration": "2026-11-20", "option_type": "call",
                        "strike": 105, "side": "buy", "contracts": 3}}


def test_the_price_request_is_what_api_price_accepts():
    from tradeassist.api import PriceRequest

    [r] = run(["pricingRequest", ["MSFT", {**CARD, "option_type": "put", "strike": 402.5}, 2]])
    body = PriceRequest(**r["ok"])
    assert (body.symbol, body.option_type, body.strike, body.side, body.contracts) == ("MSFT", "put", 402.5, "buy", 2)


def test_contract_count_defaults_to_one_and_is_capped():
    a, b, c = run(["pricingRequest", ["X", CARD, None]], ["pricingRequest", ["X", CARD, 0]],
                  ["pricingRequest", ["X", CARD, 5000]])
    assert [a["ok"]["contracts"], b["ok"]["contracts"], c["ok"]["contracts"]] == [1, 1, 1000]


def test_a_broken_card_is_refused_with_a_reason_not_a_wrong_order_ticket():
    out = run(["pricingRequest", ["", CARD, 1]],
              ["pricingRequest", ["X", {**CARD, "option_type": "stock"}, 1]],
              ["pricingRequest", ["X", {**CARD, "strike": None}, 1]],
              ["pricingRequest", ["X", {**CARD, "expiration": "Nov 20"}, 1]],
              ["pricingRequest", ["X", CARD, 1, "short"]])
    assert [o.get("error") for o in out] == [
        "No ticker for this contract.", "The contract must be a call or a put.", "The contract has no strike.",
        "The contract has no expiration date.", "Side must be buy or sell."]


def test_the_thesis_keeps_blanks_as_blank_not_zero():
    [r] = run(["thesis", [{"symbol": " nvda", "direction": "short", "entry": "", "invalidation": 0,
                           "target1": "120.5", "target2": None, "horizonDays": "999", "contracts": "2", "budget": -5}]])
    assert r["ok"] == {"symbol": "NVDA", "direction": "SHORT", "entry": None, "invalidation": None,
                       "target1": 120.5, "target2": None, "horizonDays": 20, "contracts": 2, "budget": None}


def test_the_grade_request_sends_only_the_prices_given():
    [th] = run(["thesis", [{"symbol": "AAPL", "direction": "LONG", "invalidation": 180, "horizonDays": 30}]])
    [r] = run(["gradeRequest", [th["ok"]]])
    assert r["ok"] == {"symbol": "AAPL", "direction": "LONG", "horizon_days": 30, "invalidation_price": 180}

    from tradeassist.api_v2 import PositionEvaluate
    PositionEvaluate(**r["ok"])


def test_the_option_request_uses_the_levels_the_grade_settled_on():
    """Typed prices win; blanks take the grade's suggested levels, so both forms read one thesis."""
    [th] = run(["thesis", [{"symbol": "AAPL", "direction": "SHORT", "target1": 170, "horizonDays": 25,
                            "contracts": 4, "budget": 900}]])
    levels = {"invalidation": 196.4, "target_1": 165.0, "target_2": 158.2}
    [r] = run(["optionsRequest", [th["ok"], levels]])
    assert r["ok"] == {"symbol": "AAPL", "direction": "BEARISH", "horizon_days": 25, "contracts": 4,
                       "invalidation_price": 196.4, "target_price": 170, "target_2": 158.2, "budget": 900}

    from tradeassist.api_v2 import OptionsEvaluate
    OptionsEvaluate(**r["ok"])


def test_the_option_request_leaves_out_levels_nobody_knows():
    [th] = run(["thesis", [{"symbol": "AAPL"}]])
    [r] = run(["optionsRequest", [th["ok"], {"invalidation": None, "target_1": None}]])
    assert r["ok"] == {"symbol": "AAPL", "direction": "BULLISH", "horizon_days": 20, "contracts": 1}


def test_every_contract_card_button_goes_through_the_pure_mapping():
    """The 'Set the limit' buttons are wired to FLOW.pricingRequest, not to a hand-built body."""
    v2 = (FLOW.parent / "v2.js").read_text()
    assert "state.openPricing(FLOW.pricingRequest(symbol, k, contracts, \"buy\"))" in v2
    assert v2.count("limitButton(") >= 3  # the helper, the picker cards, the reversal option card
    app = (FLOW.parent / "app.js").read_text()
    assert "state.openPricing = spec =>" in app and "view.loadContract = async spec =>" in app
