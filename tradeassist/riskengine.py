"""The deterministic risk engine (3.0 phase 5): one verdict on one order at one moment, fail-closed.

Every check is a plain rule on numbers the desk can read: the policy, the desk's own book (positions from its fills,
capital reserved for approved orders, today's new buying and today's result), the quote, the clock and the kill
switch. Nothing here asks a model. A check that cannot be measured is `unknown`, and an unknown is never a pass:
the order is blocked until it can be measured (2.x let INCOMPLETE_RISK_POLICY=WARN through; the desk does not).

Orders that only reduce risk (selling shares the desk holds) are not stopped by the daily loss stop or by an
unmeasured result, so a position can always be closed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import contracts as C
from .optionmath import NY
from .policy import TradingPolicy

OPENING_SIDES = ("buy", "sell_short", "buy_to_open", "sell_to_open")


def _check(key: str, label: str, status: str, text: str, value: Any = None, limit: Any = None) -> dict[str, Any]:
    return {"key": key, "label": label, "status": status, "value": value, "limit": limit, "text": text}


def assess(terms: C.OrderTerms, policy: TradingPolicy, book: dict[str, Any], quote: dict[str, Any] | None, *, now: datetime,
           market_state: str, data_mode: str, live_ready: bool = False, proposal_id: str | None = None) -> C.RiskAssessment:
    """`book`: positions {symbol: {quantity, avg_cost}}, capital_in_use, today_new_buys, day_pnl (None when it could
    not be measured), open_proposals [{id, symbol, side}], new_symbol_buys (pending buys of stocks not held),
    kill_switch {on, reason}. `market_state`: OPEN, CLOSED or UNKNOWN."""
    checks: list[dict[str, Any]] = []
    add = checks.append
    opening = terms.side in OPENING_SIDES
    notional = terms.notional()
    held = (book.get("positions") or {}).get(terms.symbol, {})
    held_qty = float(held.get("quantity") or 0.0)

    ks = book.get("kill_switch") or {}
    add(_check("kill_switch", "Kill switch", "fail" if ks.get("on") else "pass",
               f"On since {ks.get('at', '')}: {ks.get('reason') or 'no reason given'}" if ks.get("on") else "Off"))
    add(_check("mode", "Trading mode", "fail" if policy.mode == "disabled" else "pass",
               "The policy is off: nothing is sent" if policy.mode == "disabled" else f"{policy.mode}, {policy.account_kind} account"))
    if terms.account_kind != policy.account_kind:
        add(_check("account", "Account", "fail", f"The order is for the {terms.account_kind} account; the policy trades the {policy.account_kind} one"))
    elif terms.account_kind == "live":
        if not live_ready:
            add(_check("account", "Account", "fail", "Live execution is not enabled on this computer"))
        elif data_mode == "demo":
            add(_check("account", "Account", "fail", "Live orders need real prices; the app is on made-up demo data"))
        else:
            add(_check("account", "Account", "pass", f"live account {terms.account_ref or ''}".strip()))
    else:
        add(_check("account", "Account", "pass", f"{terms.account_kind} account" + (" on made-up demo prices" if data_mode == "demo" else "")))

    add(_check("instrument", "Instrument", "pass" if terms.instrument in policy.instruments else "fail",
               f"{terms.instrument} {'is' if terms.instrument in policy.instruments else 'is not'} allowed", terms.instrument, policy.instruments))
    add(_check("side", "Side", "pass" if terms.side in policy.sides else "fail",
               f"{terms.side.replace('_', ' ')} {'is' if terms.side in policy.sides else 'is not'} allowed", terms.side, policy.sides))
    type_ok = terms.order_type in policy.order_types and (terms.order_type not in ("limit", "stop_limit") or terms.limit_price)
    add(_check("order_type", "Order type", "pass" if type_ok else "fail",
               f"{terms.order_type} {'is' if type_ok else 'is not'} allowed", terms.order_type, policy.order_types))
    ext_ok = policy.allow_extended_hours or not terms.extended_hours
    add(_check("extended_hours", "Extended hours", "pass" if ext_ok else "fail",
               "regular hours" if not terms.extended_hours else ("allowed" if ext_ok else "not allowed")))
    sym = terms.symbol.upper()
    if sym in policy.symbols_blocked or (policy.symbols_allowed and sym not in policy.symbols_allowed):
        add(_check("symbol", "Stock", "fail", f"{sym} is not on the policy's list" if policy.symbols_allowed else f"{sym} is blocked"))
    else:
        add(_check("symbol", "Stock", "pass", sym))

    # What the book allows: no selling what is not held (long only), no second order for the same thing.
    if terms.instrument == "equity" and terms.side == "sell":
        ok = held_qty + 1e-9 >= terms.quantity
        add(_check("holding", "Shares held", "pass" if ok else "fail",
                   f"{held_qty:g} held, {terms.quantity:g} to sell" + ("" if ok else ": the desk does not sell what it does not hold"),
                   held_qty, terms.quantity))
    dupes = [p for p in book.get("open_proposals") or [] if p["symbol"] == sym and p["side"] == terms.side and p["id"] != proposal_id]
    add(_check("duplicate", "Not a duplicate", "fail" if dupes else "pass",
               f"proposal {dupes[0]['id']} is already open for the same order" if dupes else "no other open order for it"))

    # The market: a two-sided, fresh quote with a sensible spread, and a limit that does not chase it.
    meta = (quote or {}).get("_meta") or {}
    bid, ask = (quote or {}).get("bid"), (quote or {}).get("ask")
    two_sided = bool(bid and ask and ask >= bid > 0)
    if not quote or not two_sided:
        add(_check("quote", "Quote", "unknown", "no two-sided quote: the order cannot be checked against the market"))
    else:
        age = meta.get("age_seconds")
        if age is None:
            add(_check("quote", "Quote", "unknown", "the quote's time is unknown"))
        elif age > policy.max_quote_age_seconds:
            add(_check("quote", "Quote", "fail", f"{age:.0f} seconds old, more than {policy.max_quote_age_seconds:g}", round(age, 1), policy.max_quote_age_seconds))
        else:
            add(_check("quote", "Quote", "pass", f"bid {bid:.2f}, ask {ask:.2f}, {age:.0f} seconds old", round(age, 1), policy.max_quote_age_seconds))
        spread_bps = 1e4 * (ask - bid) / ((ask + bid) / 2)
        add(_check("spread", "Spread", "pass" if spread_bps <= policy.max_spread_bps else "fail",
                   f"{spread_bps:.1f} basis points", round(spread_bps, 1), policy.max_spread_bps))
        if terms.limit_price:
            through = policy.max_limit_through_bps / 1e4
            if terms.side in ("buy", "buy_to_open", "buy_to_cover", "buy_to_close"):
                ok = terms.limit_price <= ask * (1 + through) + 1e-9
                add(_check("limit", "Limit price", "pass" if ok else "fail",
                           f"{terms.limit_price:.2f} against an ask of {ask:.2f}" + ("" if ok else ": above the ask by more than the policy allows"),
                           terms.limit_price, round(ask * (1 + through), 4)))
            else:
                ok = terms.limit_price >= bid * (1 - through) - 1e-9
                add(_check("limit", "Limit price", "pass" if ok else "fail",
                           f"{terms.limit_price:.2f} against a bid of {bid:.2f}" + ("" if ok else ": below the bid by more than the policy allows"),
                           terms.limit_price, round(bid * (1 - through), 4)))

    # Money: the order, the capital, today's new buying, the number of positions, today's result.
    if opening:
        add(_check("order_size", "Order size", "pass" if notional <= policy.max_order + 1e-9 else "fail",
                   f"${notional:,.2f} of ${policy.max_order:,.0f} an order", round(notional, 2), policy.max_order))
        in_use = float(book.get("capital_in_use") or 0.0)
        add(_check("capital", "Capital", "pass" if in_use + notional <= policy.trial_capital + 1e-9 else "fail",
                   f"${in_use:,.2f} in use, ${in_use + notional:,.2f} with this, of ${policy.trial_capital:,.0f}",
                   round(in_use + notional, 2), policy.trial_capital))
        today = float(book.get("today_new_buys") or 0.0)
        add(_check("daily_buys", "New buying today", "pass" if today + notional <= policy.max_daily_new_buys + 1e-9 else "fail",
                   f"${today:,.2f} so far, ${today + notional:,.2f} with this, of ${policy.max_daily_new_buys:,.0f}",
                   round(today + notional, 2), policy.max_daily_new_buys))
        if held_qty <= 0:
            n = len([s for s, p in (book.get("positions") or {}).items() if float(p.get("quantity") or 0) > 0])
            pending = len([s for s in (book.get("new_symbol_buys") or []) if s != sym])
            add(_check("positions", "Positions", "pass" if n + pending + 1 <= policy.max_open_positions else "fail",
                       f"{n} held, {pending} more waiting, {n + pending + 1} with this, of {policy.max_open_positions}",
                       n + pending + 1, policy.max_open_positions))
        pnl = book.get("day_pnl")
        if pnl is None:
            add(_check("daily_loss", "Today's result", "unknown", "today's result could not be measured (a position has no quote)"))
        else:
            ok = pnl > -policy.max_daily_loss
            add(_check("daily_loss", "Today's result", "pass" if ok else "fail",
                       f"${pnl:+,.2f} today" + ("" if ok else f": the ${policy.max_daily_loss:,.0f} daily loss stop is reached"),
                       round(pnl, 2), -policy.max_daily_loss))

    # When: a weekday inside the policy's window, with the market open.
    ny = now.astimezone(NY)
    in_window = policy.window(True) <= ny.time() < policy.window(False) and ny.weekday() < 5
    if market_state == "UNKNOWN":
        add(_check("window", "Trading window", "unknown", "the market's clock could not be read"))
    else:
        ok = in_window and market_state == "OPEN"
        add(_check("window", "Trading window", "pass" if ok else "fail",
                   f"{ny:%a %H:%M} New York, market {market_state.lower()}" + ("" if ok else f"; orders go {policy.window_start} to {policy.window_end} while it is open")))

    if terms.side in ("sell_short", "sell_to_open"):
        max_loss = None
    elif opening:
        max_loss = notional
    else:
        max_loss = 0.0
    add(_check("max_loss", "Most it can lose", "fail" if max_loss is None else "pass",
               "no limit (a short or an uncovered option)" if max_loss is None else f"${max_loss:,.2f}", max_loss))

    bad = [c for c in checks if c["status"] in ("fail", "unknown")]
    text = (f"Passes all {len(checks)} checks." if not bad else
            "Blocked: " + "; ".join(f"{c['label'].lower()}: {c['text']}" for c in bad[:4]) + ("; and more" if len(bad) > 4 else "") + ".")
    return C.RiskAssessment(assessed_at=C.utc_now(), ok=not bad, checks=[C.RiskCheck(**c) for c in checks], fail_closed=True,
                            theoretical_max_loss=max_loss, capital_required=notional if opening else 0.0,
                            quote_age_seconds=meta.get("age_seconds"), text=text)


__all__ = ["assess", "OPENING_SIDES"]
