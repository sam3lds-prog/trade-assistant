"""Sign in to Robinhood, try every data call the app makes, and write a capture file.

Run with: python run.py --robinhood-check [SYMBOL]

Robinhood does not publish the exact form of its tools' arguments and replies, so
this is how the connection is verified on a real account. Only market-data tools
are called. The capture file holds the list of tools the server offers (names and
the arguments they accept) and a shortened copy of each market-data reply, so that
a mismatch can be fixed from the file alone. Before it is written, everything in it
is passed through `shorten` and `scrub_text`, which remove values under keys that
could identify a person, email addresses, account references and anything shaped
like a sign-in token. That is a filter, not a guarantee: read the file before
sending it.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .chain import analyze_chain, two_sided
from .optionmath import NY, days_to_expiry
from .pricer import tick_rule
from .providers.base import DataError
from .providers.robinhood import RobinhoodData, is_market_data_tool

SENSITIVE = re.compile(r"account|acct|user|email|phone|address|ssn|token|secret|password|owner|holder|customer"
                       r"|portfolio|beneficiary|nickname|birth|dob|tax_?id|auth|bearer|jwt|cookie|device"
                       r"|session_?(id|token|key)|api_?key|(^|_)ip$|(^|_)(first|last|full|legal|display)?_?name$",
                       re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_ACCOUNT_PATH = re.compile(r"(/(?:accounts?|users?|customers?)/)[^/?#\s\"']+", re.IGNORECASE)
# "account 5QR12345", "account number is 5QR12345", "Account: #5QR12345", "acct. 5QR12345",
# '"account_number": "5QR12345"', "account_number=5QR12345": the word, a little glue, then an id with a digit.
_ACCOUNT_WORDS = re.compile(r"\b((?:account|acct|user|customer|portfolio)[\w ]{0,12}?)([\s\"'.:=#(\[]|\bis\b|\bid\b){1,6}"
                            r"([A-Za-z0-9-]*\d[A-Za-z0-9-]*)", re.IGNORECASE)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")
_TOKENISH = re.compile(r"\b(?:Bearer\s+\S+|eyJ[\w-]{8,}(?:\.[\w-]+){0,2})")
_QUERY_SECRET = re.compile(r"([?&](?:access_token|token|code|key|signature|sig)=)[^&#\s]+", re.IGNORECASE)


def scrub_text(text: str, limit: int = 300) -> str:
    """Text with email addresses, account references and token-like strings taken out, and cut short."""
    text = _EMAIL.sub("[email removed]", text)
    text = _ACCOUNT_PATH.sub(r"\1[removed]", text)
    text = _ACCOUNT_WORDS.sub(lambda m: m.group(0)[:m.start(3) - m.start(0)] + "[removed]", text)
    text = _SSN.sub("[removed]", text)
    text = _PHONE.sub("[removed]", text)
    text = _TOKENISH.sub("[removed]", text)
    text = _QUERY_SECRET.sub(r"\1[removed]", text)
    return text if len(text) <= limit else text[:limit] + "..."


def shorten(value: Any, depth: int = 0) -> Any:
    """A copy small enough to share: long lists cut to three items, text scrubbed and cut, and
    anything under a key that could identify a person replaced. Robinhood's own usage notes
    ("guide") are kept at greater length, because they say how to read the reply."""
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        return {scrub_text(str(k), 80): ("[removed]" if SENSITIVE.search(str(k)) else
                                         scrub_text(v, 3000) if k in ("guide", "message") and isinstance(v, str) else
                                         shorten(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, list):
        kept = [shorten(v, depth + 1) for v in value[:3]]
        if len(value) > 3:
            kept.append(f"... {len(value) - 3} more")
        return kept
    if isinstance(value, str):
        return scrub_text(value)
    return value


VALUE_KEYS = ("default", "enum", "examples", "const")


def scrub_schema(schema: Any, keep_values: bool = True, depth: int = 0) -> Any:
    """A tool's argument description with argument names and types kept (they are what a fix needs).

    Example, default and allowed values are where a server could put something about the person
    (a list of their account numbers, say). They are kept only for the market-data tools this app
    calls (`keep_values`), and even there dropped for any argument that is about a person or an
    account. For every other tool they are dropped altogether."""
    if depth > 8:
        return "..."
    if isinstance(schema, list):
        return [scrub_schema(item, keep_values, depth + 1) for item in schema[:40]]
    if not isinstance(schema, dict):
        return scrub_text(schema, 1500 if keep_values else 300) if isinstance(schema, str) else schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            props = {}
            for name, prop in value.items():
                prop = scrub_schema(prop, keep_values and not SENSITIVE.search(str(name)), depth + 1)
                props[scrub_text(str(name), 80)] = prop
            out[key] = props
        elif key == "examples" or (key in VALUE_KEYS and not keep_values):
            continue
        elif key == "description" and not keep_values:
            continue
        else:
            out[str(key)] = scrub_schema(value, keep_values, depth + 1)
    return out


TRADING_WORDS = re.compile(r"trad|order|write|execut|transfer|withdraw|full|all|admin|manage", re.IGNORECASE)
READING_WORDS = re.compile(r"^(read([:._-]?only)?|market[:._-]?data([:._-]read)?|quotes?([:._-]read)?|data[:._-]read"
                           r"|offline_access|openid)$", re.IGNORECASE)


def scope_notes(scope: str | None) -> list[str]:
    """What to tell the user about how far the stored sign-in reaches."""
    keep_private = ("This app uses only the market-data tools listed above, but the sign-in file itself could be "
                    "used for more by another program, so keep it private and sign out when you stop using this.")
    if not scope:
        return ["Robinhood did not say what this sign-in permits, so assume it covers everything its approval "
                "page listed. " + keep_private]
    out = [f"Robinhood describes what this sign-in permits as: {scope}"]
    words = scope.split()
    if any(TRADING_WORDS.search(w) for w in words):
        out.append("That appears to include more than reading market data. " + keep_private)
    elif not all(READING_WORDS.match(w) for w in words):
        out.append("It is not clear from that wording whether it covers more than reading market data, so "
                   "assume it does. " + keep_private)
    return out


def run_check(provider: RobinhoodData, symbol: str, out_path: Path, rate: float = 0.04) -> dict[str, Any]:
    """Exercise the connection and return {"ok": bool, "steps": [...], "capture": path}."""
    steps: list[dict[str, Any]] = []
    provider.recorder = []
    tools_out: list[dict[str, Any]] = []
    notes: list[str] = []            # things that worked but that the user should know about

    def step(name: str, fn, optional: bool = False) -> Any:
        """Run one check. An optional step is recorded but does not decide whether the check passed."""
        notes.clear()
        try:
            detail, value = fn()
            steps.append({"step": name, "ok": True, "optional": optional, "detail": scrub_text(detail, 900),
                          "notes": [scrub_text(n, 500) for n in notes]})
            return value
        except (DataError, PermissionError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            steps.append({"step": name, "ok": False, "optional": optional, "notes": [],
                          "detail": scrub_text(f"{exc.__class__.__name__}: {exc}", 900)})
            return None

    def connect():
        tools = provider.bridge.tools()
        for name, tool in sorted(tools.items()):
            annotations = getattr(tool, "annotations", None)
            callable_here = is_market_data_tool(name, annotations) or (
                name in getattr(provider.bridge, "account_reads", ()) and provider.bridge.may_call(name))
            tools_out.append({
                "name": scrub_text(name, 120),
                # The full text for the tools this app calls: it is Robinhood's own account of how to use them.
                "description": scrub_text(getattr(tool, "description", "") or "", 6000 if callable_here else 160),
                "input_schema": scrub_schema(getattr(tool, "input_schema", None), keep_values=callable_here),
                "annotations": annotations.model_dump(exclude_none=True) if annotations is not None else None,
                "may_be_called": callable_here})
        usable = sorted(t["name"] for t in tools_out if t["may_be_called"])
        if provider.signin is not None:
            notes.extend(scope_notes(provider.signin.scope()))
        return (f"signed in; the server offers {len(tools)} tools and this app will only ever call these "
                f"{len(usable)}: {', '.join(usable) or 'none'}"), tools

    if step("Connect and list tools", connect) is None:
        return _finish(provider, steps, tools_out, symbol, out_path)

    def stock():
        quote = provider.quotes([symbol]).get(symbol)
        if not quote or not quote.get("last"):
            raise DataError(f"no usable quote for {symbol}")
        if not quote.get("bid_time"):
            notes.append("Robinhood gave no time for the stock quote, so the app cannot show how fresh it is.")
        return f"{symbol} last {quote['last']}, bid {quote['bid']}, ask {quote['ask']}, quoted {quote['bid_time']}", quote
    quote = step("Stock quote", stock)

    def expirations():
        dates = provider.expirations(symbol)
        today = datetime.now(NY).date().isoformat()
        future = [d for d in dates if d >= today]
        if not future:
            raise DataError("no future expirations listed")
        return f"{len(future)} expirations, {future[0]} to {future[-1]}", future
    dates = step("Option expirations", expirations)

    sample = None
    if dates:
        target = min(dates, key=lambda d: abs(days_to_expiry(d) - 30))

        def chain():
            nonlocal sample
            contracts = provider.chain(symbol, target)       # tried even without a stock price, to record the replies
            both = [c for c in contracts if two_sided(c)]
            if not quote:
                raise DataError(f"{len(contracts)} contracts were read for {target} ({len(both)} with a bid and an "
                                "ask), but without a stock price they cannot be analysed")
            analysis = analyze_chain(symbol, target, contracts, quote["last"], rate)
            calls = [c for c in analysis.contracts if c["option_type"] == "call" and two_sided(c)]
            if not calls:
                closed = provider.clock().get("state") != "open"
                raise DataError(f"{len(contracts)} contracts returned for {target} but no call has both a bid and "
                                "an ask" + (". The market is closed, which may be the whole reason: run the check "
                                            "again during market hours" if closed else ""))
            sample = min(calls, key=lambda c: abs(c["strike"] - quote["last"]))
            quotes_seen = [q for c in contracts for q in (c.get("bid"), c.get("ask"))]
            rule = tick_rule(quotes_seen, contracts[0].get("min_ticks"))
            if provider.clock().get("state") != "open":
                notes.append("The market is closed, so these are the last quotes of the session. Run the check "
                             "once more during market hours before relying on live prices.")
            with_greeks = sum(1 for c in analysis.contracts if c.get("greeks_source") == "provider")
            notes.extend(analysis.notes)
            if not with_greeks and not analysis.notes:
                notes.append("Robinhood sent no usable Greeks; the app computes its own.")
            return (f"{target}: {len(contracts)} contracts, {len(both)} with a bid and an ask, "
                    f"{with_greeks} with Robinhood's own Greeks; forward {analysis.forward:.2f} "
                    f"({analysis.forward_method}); price steps ${rule.low / 100:.2f}/${rule.high / 100:.2f} "
                    f"({'stated by Robinhood' if rule.stated else 'worked out from the quotes'}). "
                    f"Sample {sample['symbol']}: bid {sample['bid']}, ask {sample['ask']}, "
                    f"delta {sample['delta']:.3f}, implied volatility {sample['iv']:.1%}, "
                    f"model value {sample['model_value']:.2f}"), contracts
        step("Option chain and Greeks", chain)

    if sample:
        def live():
            both = provider.quotes([symbol, sample["symbol"]])
            option = both.get(sample["symbol"])
            if not option or option.get("ask") is None:
                raise DataError("the single-contract quote came back empty")
            if not option.get("bid_time"):
                notes.append("Robinhood gave no time for the option quote, so the app cannot show how fresh "
                             "it is and will say so on the pricing screen.")
            else:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(option["bid_time"])).total_seconds()
                is_open = provider.clock().get("state") == "open"
                if age < -120:
                    notes.append(f"The option quote's time is {-age / 60:.0f} minutes in the future, so the app "
                                 "is probably reading Robinhood's times wrongly.")
                elif is_open and age > 900:
                    notes.append(f"The market looks open but the option quote's time is {age / 60:.0f} minutes "
                                 "old. Either this contract is quiet, or the app is reading Robinhood's times "
                                 "wrongly (a whole number of hours suggests the second).")
            return f"{sample['symbol']} bid {option['bid']}, ask {option['ask']}, quoted {option['bid_time']}", option
        step("Live quote for one contract", live)

    def history():
        end = datetime.now(NY).date()
        start = end - timedelta(days=3 * 365)
        own_error = ""
        try:
            own = provider._history(symbol, start.isoformat(), end.isoformat())
            own_text = f"Robinhood: {len(own)} daily bars, {own['date'].iloc[0].date()} to {own['date'].iloc[-1].date()}"
        except DataError as exc:
            own, own_error = None, str(exc)
            own_text = f"Robinhood history not usable ({own_error})"
        used = provider.history(symbol, start.isoformat(), end.isoformat())
        source = "Robinhood" if own is not None and len(used) == len(own) else (
            provider.history_fallback.info.label if provider.history_fallback else "Robinhood")
        return f"{own_text}. The app will use {len(used)} bars from {source}.", used
    step("Daily price history", history)

    def earnings():
        payload = provider.earnings_raw(symbol)
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        shape = ", ".join(sorted(map(str, data))[:8]) if isinstance(data, dict) else type(data).__name__
        return f"reply recorded for a later update (the app does not use it yet); it holds: {shape}", payload
    step("Earnings dates (not used yet)", earnings, optional=True)

    if getattr(provider.bridge, "account_reads", None):
        # 2.6, only with ROBINHOOD_ACCOUNT_READ=1: which account reads the server offers, and the SHAPE of each
        # reply (field names and types, never values), so the reader can be matched to it without your data.
        def account():
            from .robinhood_account import RobinhoodAccount, shape
            acct = RobinhoodAccount(provider.bridge)
            offered = acct.offered()
            shapes = {}
            for name in offered.values():
                if name:
                    try:
                        shapes[name] = shape(provider.bridge.call(name, {}))
                    except (DataError, PermissionError) as exc:
                        shapes[name] = f"error: {scrub_text(str(exc), 200)}"
            account_shapes.update(shapes)
            have = [f"{job} ({name})" for job, name in offered.items() if name]
            missing = [job for job, name in offered.items() if not name]
            return (f"offers {', '.join(have) or 'none of the account reads'}"
                    + (f"; not offered: {', '.join(missing)}" if missing else "")
                    + ". Reply shapes recorded, without values."), offered
        account_shapes: dict[str, Any] = {}
        step("Your holdings and past orders (account reads)", account, optional=True)
        tools_out.append({"name": "(account reply shapes)", "description": "field names and types only",
                          "input_schema": None, "annotations": None, "may_be_called": False, "shapes": account_shapes})

    return _finish(provider, steps, tools_out, symbol, out_path)


def _finish(provider: RobinhoodData, steps: list[dict[str, Any]], tools_out: list[dict[str, Any]], symbol: str,
            out_path: Path) -> dict[str, Any]:
    calls = [{"tool": c["tool"], "arguments": shorten(c["arguments"]), "reply": shorten(c["reply"])}
             for c in (provider.recorder or [])]
    provider.recorder = None
    capture = {"captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "app_version": __version__,
               "symbol": symbol, "steps": steps, "tools": tools_out, "calls": calls,
               "note": "Only the tools marked may_be_called were called. Account replies (when switched on) are "
                       "recorded as field names and types only, never values. Market replies are shortened, and "
                       "email addresses, account references, token-like text and values under keys that could "
                       "identify a person are removed. Read this file before sending it to anyone."}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        out_path.unlink()                                  # so the new file is private from its first byte
    with os.fdopen(os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        handle.write(json.dumps(capture, indent=1, default=str))
    needed = [s for s in steps if not s.get("optional")]
    return {"ok": bool(needed) and all(s["ok"] for s in needed), "steps": steps, "capture": str(out_path),
            "noted": any(s["notes"] for s in steps)}
