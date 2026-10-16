"""Turn a raw option chain into per-contract analysis: mid, spread, implied vol,
a smoothed model value and Greeks."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any

from .optionmath import (Smile, black76_greeks, black76_price, fit_smile, implied_forward, implied_vol,
                         years_to_expiry)


@dataclass
class ChainAnalysis:
    symbol: str
    expiration: str
    spot: float
    forward: float
    forward_method: str      # parity | carry
    t: float
    rate: float
    smile: Smile | None
    contracts: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)     # things a reader should know about the data

    def find(self, option_symbol: str) -> dict[str, Any] | None:
        for c in self.contracts:
            if c["symbol"] == option_symbol:
                return c
        return None

    def find_by_terms(self, option_type: str, strike: float) -> dict[str, Any] | None:
        for c in self.contracts:
            if c["option_type"] == option_type and abs(c["strike"] - strike) < 1e-6:
                return c
        return None


# A data source's volatility or Greek may differ from this app's own calculation by this factor
# (models differ) before it is taken to be in other units and set aside.
UNIT_TOLERANCE = 2.0
UNIT_CHECK_MIN_YEARS = 1.5 / 365.0
UNIT_CHECK_MAX_SPREAD_PCT = 30.0
IV_RANGE = (0.01, 5.0)


def two_sided(contract: dict[str, Any]) -> bool:
    """Both sides quoted, with the ask no lower than the bid. A locked quote (the same price on
    both sides) is the tightest market there is, not a missing one, and counts as two-sided here
    as it does in freshness.quote_completeness; only a crossed quote is thrown out."""
    bid, ask = contract.get("bid"), contract.get("ask")
    return bid is not None and ask is not None and bid > 0 and ask >= bid


def mid_price(contract: dict[str, Any]) -> float | None:
    bid, ask = contract.get("bid"), contract.get("ask")
    if bid is None or ask is None or ask <= 0 or ask < bid:
        return None
    return 0.5 * (bid + ask)


def analyze_chain(symbol: str, expiration: str, contracts: list[dict[str, Any]], spot: float,
                  rate: float, now=None) -> ChainAnalysis:
    if spot is None or spot <= 0:
        raise ValueError(f"No usable price for {symbol}")
    t = years_to_expiry(expiration, now)

    by_strike: dict[float, dict[str, dict[str, Any]]] = {}
    for c in contracts:
        by_strike.setdefault(c["strike"], {})[c["option_type"]] = c

    pairs = []
    for strike, sides in by_strike.items():
        call, put = sides.get("call"), sides.get("put")
        if call and put and two_sided(call) and two_sided(put):
            pairs.append((strike, mid_price(call), mid_price(put)))
    forward, method = implied_forward(pairs, spot, t, rate)

    out: list[dict[str, Any]] = []
    for c in contracts:
        row = dict(c)
        mid = mid_price(c)
        row["mid"] = mid
        row["spread"] = (c["ask"] - c["bid"]) if (c.get("bid") is not None and c.get("ask") is not None) else None
        row["spread_pct"] = (100.0 * row["spread"] / mid) if (mid and row["spread"] is not None) else None
        row["own_iv"] = implied_vol(mid, forward, c["strike"], t, rate, c["option_type"]) if mid else None
        out.append(row)

    # A data source's implied volatility is used only if it is in the units this app works in (0.25
    # for 25%). That is judged across the chain, against the volatility implied by each contract's
    # own midpoint: one source sending percentages would otherwise wreck every model value.
    notes: list[str] = []
    # On the last day or so the two calculations stop being comparable (this app's theta becomes
    # "all the time value that is left"), so the comparison is not made and the source is kept.
    comparable = t > UNIT_CHECK_MIN_YEARS
    iv_ratios = [(r.get("greeks") or {}).get("mid_iv") / r["own_iv"] for r in out
                 if (r.get("greeks") or {}).get("mid_iv") and r["own_iv"] and two_sided(r)
                 and (r["option_type"] == "call") == (r["strike"] >= forward)
                 # a wide quote on a nearly worthless option says little about its volatility
                 and r["spread_pct"] is not None and r["spread_pct"] <= UNIT_CHECK_MAX_SPREAD_PCT]
    # Trusted when it agrees across the chain. With too few quotes to tell, this app's own figure is
    # preferred wherever it has one.
    iv_checked = comparable and len(iv_ratios) >= 3
    iv_trusted = True
    if iv_checked and not (1 / UNIT_TOLERANCE <= median(iv_ratios) <= UNIT_TOLERANCE):
        iv_trusted = False
        notes.append(f"The data source's implied volatilities are about {median(iv_ratios):.3g} times the ones "
                     "implied by its own prices, so they look like different units and were not used. "
                     "Volatility was computed from the quotes instead.")
    for row in out:
        provider_iv = (row.get("greeks") or {}).get("mid_iv")
        usable = bool(provider_iv) and iv_trusted and IV_RANGE[0] <= provider_iv <= IV_RANGE[1]
        if usable and comparable and not iv_checked and row["own_iv"]:
            usable = False
        row["iv"] = provider_iv if usable else row["own_iv"]
        row["iv_source"] = "provider" if usable else ("computed" if row["own_iv"] else None)

    # Smile through out-of-the-money quotes: they carry the least early-exercise value and
    # are usually the most actively quoted side of each strike.
    strikes, ivs, weights = [], [], []
    for row in out:
        is_otm = (row["option_type"] == "call") == (row["strike"] >= forward)
        # A spread of zero is the tightest quote there is, not a missing one: only None means unknown.
        if not is_otm or not two_sided(row) or not row["iv"] or row["spread_pct"] is None:
            continue
        strikes.append(row["strike"])
        ivs.append(row["iv"])
        weights.append(1.0 / max(row["spread_pct"], 1.0) ** 2)
    smile = fit_smile(strikes, ivs, weights, forward)

    for row in out:
        smv = (row.get("greeks") or {}).get("smv_vol")
        if smv and smv > 0:
            model_vol, source = smv, "provider smoothed volatility"
        elif smile is not None:
            model_vol, source = smile.vol_at(row["strike"], forward), "smile fitted across strikes"
        elif row["iv"]:
            model_vol, source = row["iv"], "this contract's own mid (no independent check)"
        else:
            model_vol, source = None, None
        row["model_vol"] = model_vol
        row["model_source"] = source
        row["model_value"] = (black76_price(forward, row["strike"], t, rate, model_vol, row["option_type"])
                              if model_vol else None)

        vol_for_greeks = row["iv"] or model_vol
        row["_computed"] = (black76_greeks(spot, forward, row["strike"], t, rate, vol_for_greeks, row["option_type"])
                            if vol_for_greeks else None)
        intrinsic = max((spot - row["strike"]) if row["option_type"] == "call" else (row["strike"] - spot), 0.0)
        row["intrinsic"] = intrinsic
        row["moneyness_pct"] = 100.0 * (row["strike"] / spot - 1.0)

    # The data source's Greeks are used only if, across the chain, they are in this app's units:
    # theta as dollars per share per day, vega per one percentage point of volatility.
    greeks_trusted, greeks_checked = True, comparable
    for name, unit in (("delta", "this app shows delta between -1 and 1"),
                       ("theta", "this app shows theta as the loss per day"),
                       ("vega", "this app shows vega as the change per one point of volatility")):
        ratios = []
        for row in out:
            theirs, ours = (row.get("greeks") or {}).get(name), row["_computed"]
            if (theirs and ours and two_sided(row) and 0.15 <= abs(ours.delta) <= 0.85
                    and abs(getattr(ours, name)) > 1e-6):
                ratios.append(theirs / getattr(ours, name))
        if len(ratios) < 3:
            greeks_checked = False
        elif comparable and not (1 / UNIT_TOLERANCE <= median(ratios) <= UNIT_TOLERANCE):
            greeks_trusted = False
            notes.append(f"The data source's {name} values are about {median(ratios):.3g} times this app's own "
                         f"calculation, so they appear to be in other units ({unit}). They were not used; the "
                         "Greeks shown were computed here.")
    for row in out:
        provider = row.get("greeks") or {}
        ours = row.pop("_computed")
        delta = provider.get("delta")
        # Tradier's own examples show Greeks filled with zeros. A theta of exactly zero is a
        # placeholder, not a measurement: every option with time left decays.
        provider_ok = (greeks_trusted and delta is not None and provider.get("theta") is not None
                       and abs(delta) <= 1.001 and provider["theta"] < 0
                       and (delta >= -0.001 if row["option_type"] == "call" else delta <= 0.001))
        if provider_ok and comparable and not greeks_checked and ours is not None:
            provider_ok = False            # too few quotes to check the source's units: use our own
        if provider_ok:
            row["delta"], row["gamma"] = delta, provider.get("gamma")
            row["theta"], row["vega"] = provider["theta"], provider.get("vega")
            row["greeks_source"] = "provider"
        elif ours is not None:
            row["delta"], row["gamma"], row["theta"], row["vega"] = ours.delta, ours.gamma, ours.theta, ours.vega
            row["greeks_source"] = "computed"
        else:
            row["delta"] = row["gamma"] = row["theta"] = row["vega"] = None
            row["greeks_source"] = None

    out.sort(key=lambda r: (r["strike"], r["option_type"]))
    return ChainAnalysis(symbol=symbol, expiration=expiration, spot=spot, forward=forward,
                         forward_method=method, t=t, rate=rate, smile=smile, contracts=out, notes=notes)


def atm_iv(analysis: ChainAnalysis) -> float | None:
    """Implied volatility at the money: average of the call and put at the strike nearest the forward."""
    candidates = sorted({c["strike"] for c in analysis.contracts}, key=lambda k: abs(k - analysis.forward))
    for strike in candidates[:3]:
        ivs = [c["iv"] for c in analysis.contracts if c["strike"] == strike and c["iv"] and two_sided(c)]
        if ivs:
            return sum(ivs) / len(ivs)
    return None
