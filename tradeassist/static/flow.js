"use strict";
/* Trade Assistant 2.5: pure helpers for the decision path (which way, which stock, shares or an option,
   what it might make or lose, and the limit to type at the broker). Nothing here touches the page, so
   the tests run these functions under Node. Loaded first; app.js, v2.js and lab.js use them. */

const FLOW = (() => {
  const finite = v => typeof v === "number" && isFinite(v);
  const positive = v => finite(v) && v > 0;

  // The /api/price request for a contract chosen on another screen: the limit ladder opens on it.
  function pricingRequest(symbol, contract, contracts, side = "buy") {
    const sym = String(symbol || "").trim().toUpperCase();
    const kind = String((contract && contract.option_type) || "").toLowerCase();
    const strike = Number(contract && contract.strike);
    const expiration = String((contract && contract.expiration) || "");
    if (!sym) throw new Error("No ticker for this contract.");
    if (kind !== "call" && kind !== "put") throw new Error("The contract must be a call or a put.");
    if (!positive(strike)) throw new Error("The contract has no strike.");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(expiration)) throw new Error("The contract has no expiration date.");
    if (side !== "buy" && side !== "sell") throw new Error("Side must be buy or sell.");
    const n = Math.round(Number(contracts));
    return { symbol: sym, expiration, option_type: kind, strike, side, contracts: positive(n) ? Math.min(n, 1000) : 1 };
  }

  // The thesis carried from Today to Decide and between its forms. Prices left out mean "suggest them".
  function thesis(fields = {}) {
    const dir = String(fields.direction || "LONG").toUpperCase();
    const price = v => positive(Number(v)) && v !== "" && v !== null ? Number(v) : null;
    const whole = (v, lo, hi, dflt) => { const n = Math.round(Number(v)); return finite(n) && n >= lo && n <= hi ? n : dflt; };
    return {
      symbol: String(fields.symbol || "").trim().toUpperCase(),
      direction: dir === "SHORT" ? "SHORT" : "LONG",
      entry: price(fields.entry), invalidation: price(fields.invalidation),
      target1: price(fields.target1), target2: price(fields.target2),
      horizonDays: whole(fields.horizonDays, 1, 260, 20), contracts: whole(fields.contracts, 1, 500, 1),
      budget: price(fields.budget),
    };
  }

  // The stock grade's request (POST /v2/position/evaluate).
  function gradeRequest(th) {
    const body = { symbol: th.symbol, direction: th.direction, horizon_days: th.horizonDays };
    if (th.entry !== null) body.entry_price = th.entry;
    if (th.invalidation !== null) body.invalidation_price = th.invalidation;
    if (th.target1 !== null) body.target_1 = th.target1;
    if (th.target2 !== null) body.target_2 = th.target2;
    return body;
  }

  // The option picker's request (POST /v2/options/evaluate), on the levels the grade settled on, so both
  // read the same thesis: long means calls (bullish), short means puts (bearish).
  function optionsRequest(th, levels = {}) {
    const body = { symbol: th.symbol, direction: th.direction === "SHORT" ? "BEARISH" : "BULLISH",
      horizon_days: th.horizonDays, contracts: th.contracts };
    const inv = th.invalidation !== null ? th.invalidation : levels.invalidation;
    const t1 = th.target1 !== null ? th.target1 : levels.target_1;
    const t2 = th.target2 !== null ? th.target2 : levels.target_2;
    if (positive(inv)) body.invalidation_price = inv;
    if (positive(t1)) body.target_price = t1;
    if (positive(t2)) body.target_2 = t2;
    if (th.budget !== null) body.budget = th.budget;
    // The share entry, when typed: the chance the shares are ahead is measured from it (2.5.1).
    if (th.entry !== null) body.entry_price = th.entry;
    return body;
  }

  return { pricingRequest, thesis, gradeRequest, optionsRequest };
})();
