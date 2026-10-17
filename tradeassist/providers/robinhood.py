"""Market data from Robinhood, through its Trading MCP server. Read-only.

Robinhood documents how to connect to this server but not the exact arguments its
tools take or the shape of what they return. So this module works from what the
server itself declares (every tool publishes the arguments it accepts) and from the
field names reported by people who have used it, and it is strict about what it
accepts: when a reply does not look the way it expects, it raises an error that
says so instead of guessing. `python run.py --robinhood-check` exercises every call
this module makes and writes a capture file that shows exactly where a mismatch is.

Only the market-data tools named in TOOLS below are ever called: quotes, option
chains, option contracts and price history. Nothing here reads positions, balances
or orders (2.6: robinhood_account.py reads holdings and past orders, only when switched on), and
nothing can place, change or cancel an order; the bridge refuses any
tool whose exact name is not on that list. If Robinhood names a tool differently,
the call fails with a message listing the server's similar names, and the name has
to be added to TOOLS by hand after reading what the tool does.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Any, Iterable

import pandas as pd

from ..optionmath import NY
from .base import DataError, MarketData, ProviderInfo, bars_frame, display_symbol, num
from .mcp_bridge import FileTokenStorage, ReadOnlyBridge, is_read_only

log = logging.getLogger(__name__)

SERVER_URL = "https://agent.robinhood.com/mcp/trading"
HELP = ("Run 'python run.py --robinhood-check' and send the capture file it writes, so the connection "
        "can be matched to what Robinhood actually returns.")

# The complete list of tools this app may call, by the job each does. Nothing else is ever called.
TOOLS: dict[str, tuple[str, ...]] = {
    "stock quotes": ("get_equity_quotes",),
    "option expirations": ("get_option_chains",),
    "option contracts": ("get_option_instruments",),
    "option quotes": ("get_option_quotes",),
    "price history": ("get_equity_historicals", "get_stock_historicals"),
    # Read by the check command only, so the reply can be studied; the app does not use it yet.
    "earnings dates": ("get_earnings_results",),
}
ALLOWED_TOOLS = frozenset(name for names in TOOLS.values() for name in names)
# Words that help a person find the right tool when the server uses another name (for messages only).
SIMILAR = {"stock quotes": ("quote",), "option expirations": ("chain", "expir"), "option contracts": ("instrument", "contract"),
           "option quotes": ("quote",), "price history": ("histor", "bar", "candle"), "earnings dates": ("earning",)}
DEFAULT_BATCH = 20            # Robinhood documents 20 symbols per equity-quote request
OPTION_QUOTE_BATCH = 100      # contracts per option-quote request when the tool states no limit of its own
WHOLE_CHAIN_LIMIT = 600       # an expiration with more contracts than this is quoted only near the stock price
STRIKE_WINDOW = 0.50          # ... within this fraction of it
EQUITY_QUOTE_TTL = 1.5        # seconds: lets the chain and the spot lookup share one request
CHAIN_LIST_TTL = 600.0
INSTRUMENT_TTL = 6 * 3600.0


NO_QUOTE: dict[str, Any] = {"bid": None, "ask": None, "bidsize": None, "asksize": None, "last": None, "volume": 0.0,
                            "open_interest": 0.0, "bid_time": None, "ask_time": None, "trade_time": None,
                            "prevclose": None, "fill_prices": None}

# 2.10: Robinhood's option market data has carried its own estimates of a buy or sell price likely (and less
# likely) to fill. Read when the reply has them, shown beside the fair price for reference, never used in it.
FILL_PRICE_FIELDS = {"buy_likely": ("high_fill_rate_buy_price",), "buy_less_likely": ("low_fill_rate_buy_price",),
                     "sell_likely": ("high_fill_rate_sell_price",), "sell_less_likely": ("low_fill_rate_sell_price",)}


def fill_prices(raw: dict[str, Any]) -> dict[str, float] | None:
    """Robinhood's fill-likelihood prices in a quote, when present and usable; None otherwise."""
    out = {}
    for key, names in FILL_PRICE_FIELDS.items():
        value = num(pick(raw, *names))
        if value is not None and value > 0:
            out[key] = value
    return out or None


def is_market_data_tool(name: str, annotations: Any = None) -> bool:
    """True only for the tools listed in TOOLS (and never one the server marks as changing anything)."""
    return is_read_only(name, annotations, ALLOWED_TOOLS)


assert all(is_market_data_tool(name) for name in ALLOWED_TOOLS), "TOOLS holds a name that is not plain market data"


# --- reading replies ----------------------------------------------------------------

def unwrap(payload: Any) -> Any:
    """Replies arrive as {"data": ..., "guide": "..."}; the content is under "data"."""
    if isinstance(payload, dict) and "data" in payload and set(payload) <= {"data", "guide", "meta", "message"}:
        return payload["data"]
    return payload


def records(payload: Any, *hints: str) -> list[dict[str, Any]]:
    """The list of records inside a reply, wherever the server put it."""
    data = unwrap(payload)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in (*hints, "results", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    lists = [v for v in data.values() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)]
    if len(lists) == 1:
        return lists[0]
    if not lists:
        return [data]          # a single record
    return []


def flatten_quote(raw: dict[str, Any]) -> dict[str, Any]:
    """Robinhood pairs each live quote with the last official close: {"quote": {...}, "close": {...}}.
    This returns the quote's own fields, with the official close added as "official_close"."""
    inner = raw.get("quote")
    if not isinstance(inner, dict):
        return raw
    out = dict(inner)
    close = raw.get("close")
    if isinstance(close, dict):
        out["official_close"] = pick(close, "price", "close_price", "close")
    for key in ("instrument_id", "id", "symbol"):          # an id kept beside the quote rather than inside it
        if key not in out and raw.get(key) is not None:
            out[key] = raw[key]
    return out


def newest(*pairs: tuple[Any, Any]) -> tuple[Any, str | None]:
    """Of several (value, time) pairs, the value with the latest time. Pairs without a value are ignored;
    a value without a time loses to any value that has one."""
    best: tuple[Any, str | None] = (None, None)
    for value, when in pairs:
        if value is None:
            continue
        stamp = iso_time(when)
        if best[0] is None or (stamp is not None and (best[1] is None or stamp > best[1])):
            best = (value, stamp)
    return best


def pick(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def iso_time(value: Any) -> str | None:
    """A timestamp in any of the usual forms as an ISO string with a time zone."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.isascii() and value.strip().replace(".", "", 1).isdigit() \
            and float(value) > 1e9:
        value = float(value)                    # seconds or milliseconds since 1970, sent as text
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)           # Robinhood sends nanoseconds; keep microseconds
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _bare(symbol: str) -> str:
    """A ticker without the punctuation sources disagree on (BRK.B, BRK/B, BRKB)."""
    return "".join(ch for ch in symbol.upper() if ch.isalnum())


def last_path_part(value: Any) -> str | None:
    """Robinhood sometimes refers to a contract by URL; its id is the last part of the path."""
    if not isinstance(value, str) or not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def occ_symbol(root: str, expiration: str, option_type: str, strike: float) -> str:
    if option_type not in ("call", "put"):
        raise ValueError(f"option_type must be call or put, got {option_type!r}")
    d = date.fromisoformat(expiration)
    clean = root.replace(".", "").replace("/", "")
    return f"{clean}{d:%y%m%d}{'C' if option_type == 'call' else 'P'}{int(round(strike * 1000)):08d}"


# --- building requests from what each tool says it accepts ----------------------------

def properties(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None) or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    return props if isinstance(props, dict) else {}


def required(tool: Any) -> list[str]:
    schema = getattr(tool, "input_schema", None) or {}
    req = schema.get("required") if isinstance(schema, dict) else None
    return list(req) if isinstance(req, list) else []


def choose(props: dict[str, Any], *candidates: str) -> str | None:
    return next((c for c in candidates if c in props), None)


def wants_list(prop: Any) -> bool:
    """Whether an argument is declared as a list (rather than one comma-separated string)."""
    if not isinstance(prop, dict):
        return True
    kind = prop.get("type")
    if isinstance(kind, list):                 # for example ["null", "array"]
        return "array" in kind or "string" not in kind
    if kind == "array":
        return True
    if kind == "string":
        return False
    options = prop.get("anyOf") or prop.get("oneOf") or []
    return any(isinstance(o, dict) and o.get("type") == "array" for o in options) or not options


def many(prop: Any, values: list[str]) -> Any:
    return list(values) if wants_list(prop) else ",".join(values)


def batch_size(prop: Any, default: int = DEFAULT_BATCH) -> int:
    limit = prop.get("maxItems") if isinstance(prop, dict) else None
    return int(limit) if isinstance(limit, int) and 0 < limit <= 500 else default


def enum_of(prop: Any) -> list[str]:
    if not isinstance(prop, dict):
        return []
    values = prop.get("enum")
    if not values:
        for option in prop.get("anyOf") or prop.get("oneOf") or []:
            if isinstance(option, dict) and option.get("enum"):
                values = option["enum"]
                break
    return [str(v) for v in values or []]


def from_enum(prop: Any, preferences: Iterable[str], default: str) -> str:
    allowed = enum_of(prop)
    if not allowed:
        return default
    lowered = {a.lower(): a for a in allowed}
    for wanted in preferences:
        if wanted in lowered:
            return lowered[wanted]
    return allowed[0]


class RobinhoodData:
    """MarketData provider backed by Robinhood's Trading MCP server."""

    def __init__(self, bridge: ReadOnlyBridge, history_fallback: MarketData | None = None,
                 poll_seconds: float = 3.0):
        self.bridge = bridge
        self.history_fallback = history_fallback
        self.info = ProviderInfo(
            mode="robinhood", label="Robinhood", realtime=True, has_greeks=True,
            note="Quotes and Greeks from your Robinhood account's data connection. Market data only: "
                 "this app never calls a tool that can place or change an order.",
            poll_seconds=poll_seconds)
        self.recorder: list[dict[str, Any]] | None = None     # filled by --robinhood-check
        self.signin: FileTokenStorage | None = None           # where the sign-in is kept, when there is one
        self._lock = threading.RLock()
        self._equity_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._chain_lists: dict[str, tuple[float, dict[str, Any]]] = {}
        self._instruments: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._contracts: dict[str, dict[str, Any]] = {}       # our option symbol -> contract terms and id
        self._quote_batch = OPTION_QUOTE_BATCH

    # --- plumbing ----------------------------------------------------------
    def _tool(self, job: str) -> Any:
        """The tool that does `job`, by exact name. A tool with any other name is never used."""
        tools = self.bridge.tools()
        names = TOOLS[job]
        for name in names:
            if name in tools and is_market_data_tool(name, getattr(tools[name], "annotations", None)):
                return tools[name]
        marked = [name for name in names if name in tools]
        if marked:
            raise DataError(f"Robinhood describes {marked[0]} as a tool that changes something, so this app "
                            f"will not call it. {HELP}")
        similar = sorted(name for name in tools if any(word in name.lower() for word in SIMILAR[job]))[:8]
        hint = (f" The server does have: {', '.join(similar)}. None of them is on this app's list of "
                "market-data tools, so none was called.") if similar else ""
        raise DataError(f"Robinhood's connection has no tool named {' or '.join(names)} for {job}.{hint} "
                        f"Options data may not be switched on for your account yet. {HELP}")

    def _call(self, tool: Any, arguments: dict[str, Any]) -> Any:
        if not is_market_data_tool(tool.name, getattr(tool, "annotations", None)):
            raise PermissionError(f"Refusing to call {tool.name}: it is not a market-data tool")
        missing = [r for r in required(tool) if r not in arguments]
        if missing:
            raise DataError(f"Robinhood's {tool.name} needs arguments this app does not know how to fill: "
                            f"{', '.join(missing)}. {HELP}")
        payload = self.bridge.call(tool.name, arguments)
        if self.recorder is not None:
            self.recorder.append({"tool": tool.name, "arguments": arguments, "reply": payload})
        return payload

    # --- stock quotes --------------------------------------------------------
    def _equity_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        need = []
        for s in symbols:
            cached = self._equity_cache.get(s)
            if cached and now - cached[0] < EQUITY_QUOTE_TTL:
                out[s] = cached[1]
            else:
                need.append(s)
        if not need:
            return out
        tool = self._tool("stock quotes")
        props = properties(tool)
        arg = choose(props, "symbols", "symbol", "tickers", "ticker")
        if arg is None:
            raise DataError(f"Robinhood's {tool.name} does not take a symbols argument this app recognises. {HELP}")
        plural = arg.endswith("s")
        size = batch_size(props[arg]) if plural else 1
        for i in range(0, len(need), size):
            group = need[i:i + size]
            payload = self._call(tool, {arg: many(props[arg], group) if plural else group[0]})
            for raw in records(payload, "quotes", "equity_quotes"):
                raw = flatten_quote(raw)
                symbol = display_symbol(str(pick(raw, "symbol", "ticker") or ""))
                if not symbol:
                    continue
                general = pick(raw, "updated_at", "timestamp")
                # The newest trade may be from outside regular hours; Robinhood reports the two separately.
                last, trade_time = newest(
                    (num(pick(raw, "last_trade_price", "last_price", "price")),
                     pick(raw, "venue_last_trade_time", "last_trade_time") or general),
                    (num(pick(raw, "last_non_reg_trade_price", "last_extended_hours_trade_price")),
                     pick(raw, "venue_last_non_reg_trade_time")))
                if last is None:
                    last = num(pick(raw, "mark_price"))
                bid_time = iso_time(pick(raw, "venue_bid_time", "bid_time") or general) or trade_time
                ask_time = iso_time(pick(raw, "venue_ask_time", "ask_time") or general) or trade_time
                quote = {
                    "symbol": symbol, "description": str(pick(raw, "name", "simple_name", "description") or ""),
                    "type": "stock", "last": last,
                    "bid": num(pick(raw, "bid_price", "bid")), "ask": num(pick(raw, "ask_price", "ask")),
                    "bidsize": num(pick(raw, "bid_size", "bidsize")), "asksize": num(pick(raw, "ask_size", "asksize")),
                    "prevclose": num(pick(raw, "previous_close", "adjusted_previous_close", "prior_close",
                                          "previous_close_price", "official_close")),
                    "open": None, "high": None, "low": None, "close": None,
                    "volume": num(pick(raw, "volume")), "average_volume": None,
                    "week_52_high": None, "week_52_low": None,
                    "bid_time": bid_time, "ask_time": ask_time, "trade_time": trade_time or bid_time,
                }
                if last is None and quote["bid"] is None and quote["ask"] is None:
                    continue
                out[symbol] = quote
                self._equity_cache[symbol] = (time.time(), quote)
        return out

    # --- option contracts ------------------------------------------------------
    def _chain_list(self, symbol: str) -> dict[str, Any]:
        cached = self._chain_lists.get(symbol)
        if cached and time.time() - cached[0] < CHAIN_LIST_TTL:
            return cached[1]
        tool = self._tool("option expirations")
        props = properties(tool)
        arg = choose(props, "symbol", "symbols", "underlying_symbol", "chain_symbol", "ticker", "underlying")
        if arg is None:
            raise DataError(f"Robinhood's {tool.name} does not take a symbol argument this app recognises. {HELP}")
        payload = self._call(tool, {arg: many(props[arg], [symbol]) if arg == "symbols" else symbol})
        chains = [c for c in records(payload, "chains", "option_chains")
                  if isinstance(pick(c, "expiration_dates", "expirations"), list)]
        if not chains:
            raise DataError(f"Robinhood returned no option expirations for {symbol}. {HELP}")

        def standard(chain: dict[str, Any]) -> tuple[int, int]:
            multiplier = num(pick(chain, "trade_value_multiplier", "multiplier", "contract_size"))
            same = display_symbol(str(pick(chain, "symbol", "chain_symbol") or symbol)) == symbol
            return (int(same and multiplier in (None, 100.0)), len(pick(chain, "expiration_dates", "expirations")))

        best = max(chains, key=standard)
        multiplier = num(pick(best, "trade_value_multiplier", "multiplier", "contract_size"))
        if multiplier not in (None, 100.0):
            raise DataError(f"{symbol} only has adjusted option contracts ({multiplier:g} shares each), "
                            "which this app does not price.")
        ticks = best.get("min_ticks")
        entry = {"id": pick(best, "id", "chain_id"), "several": len(chains) > 1,
                 "min_ticks": ticks if isinstance(ticks, dict) else None,
                 "expirations": sorted(str(d) for d in pick(best, "expiration_dates", "expirations"))}
        self._chain_lists[symbol] = (time.time(), entry)
        return entry

    def _instrument_list(self, symbol: str, expiration: str) -> list[dict[str, Any]]:
        key = (symbol, expiration)
        cached = self._instruments.get(key)
        if cached and time.time() - cached[0] < INSTRUMENT_TTL:
            return cached[1]
        chain = self._chain_list(symbol)
        tool = self._tool("option contracts")
        props = properties(tool)
        args: dict[str, Any] = {}
        sym_arg = choose(props, "chain_symbol", "symbol", "underlying_symbol", "ticker", "symbols")
        by_chain = "chain_id" in props and bool(chain.get("id"))
        if sym_arg and not (by_chain and sym_arg not in required(tool)):
            # Robinhood asks for the chain id or the symbol; the id is exact, so it is used when available.
            args[sym_arg] = many(props[sym_arg], [symbol]) if sym_arg == "symbols" else symbol
        exp_arg = choose(props, "expiration_dates", "expiration_date", "expiration", "expirations")
        if exp_arg:
            args[exp_arg] = many(props[exp_arg], [expiration]) if exp_arg.endswith("s") else expiration
        if by_chain:
            args["chain_id"] = chain["id"]         # asks for the standard set of contracts only
        if "state" in props:
            args["state"] = from_enum(props["state"], ("active",), "active")
        if not sym_arg and "chain_id" not in args:
            raise DataError(f"Robinhood's {tool.name} does not take a symbol or chain argument this app "
                            f"recognises. {HELP}")
        cursor_arg = choose(props, "cursor", "page_token", "next_cursor")
        found: list[dict[str, Any]] = []
        chain_id = str(chain["id"]) if chain.get("id") else None
        complete = False
        marked = "chain_id" in args               # whether anything says which set each contract belongs to
        for _ in range(40):
            payload = self._call(tool, args)
            for raw in records(payload, "instruments", "option_instruments"):
                # A stock can have a second set of contracts, adjusted after a split or merger, at the
                # same strikes. Keep only contracts that belong to the standard chain chosen above.
                their_chain = pick(raw, "chain_id") or last_path_part(raw.get("chain"))
                their_root = pick(raw, "chain_symbol", "underlying_symbol", "root_symbol")
                marked = marked or bool(their_chain or their_root)
                if chain_id and their_chain and str(their_chain) != chain_id:
                    continue
                if their_root and _bare(str(their_root)) != _bare(symbol):
                    continue
                strike = num(pick(raw, "strike_price", "strike"))
                kind = str(pick(raw, "type", "option_type") or "").lower()
                when = str(pick(raw, "expiration_date", "expiration") or "")
                ident = pick(raw, "id", "instrument_id", "option_id") or last_path_part(raw.get("url"))
                state = str(pick(raw, "state") or "active").lower()
                if strike is None or strike <= 0 or kind not in ("call", "put") or not ident or state != "active":
                    continue
                if when != expiration and (when or not exp_arg):
                    continue          # keep only the expiration asked for, whether or not the server filtered
                ticks = raw.get("min_ticks") if isinstance(raw.get("min_ticks"), dict) else chain.get("min_ticks")
                found.append({"id": str(ident), "strike": strike, "option_type": kind, "expiration": expiration,
                              "min_ticks": ticks})
            data = unwrap(payload)
            nxt = (pick(data, "next_cursor", "next_page_token", "cursor", "next") if isinstance(data, dict) else None) \
                or (pick(payload, "next_cursor", "next_page_token", "next") if isinstance(payload, dict) else None)
            if not nxt:
                complete = True
                break
            if not cursor_arg:
                raise DataError(f"Robinhood's {tool.name} says there are more contracts than it sent, and this "
                                f"app does not know how to ask for the rest. {HELP}")
            if args.get(cursor_arg) == nxt:
                raise DataError(f"Robinhood's {tool.name} keeps pointing at the same page of contracts. {HELP}")
            args = {**args, cursor_arg: nxt}
        if not complete:
            raise DataError(f"Robinhood's list of contracts for {symbol} did not end after 40 pages. {HELP}")
        if not found:
            raise DataError(f"Robinhood returned no contracts for {symbol} {expiration}. {HELP}")
        if chain.get("several") and not marked:
            raise DataError(f"{symbol} has more than one set of option contracts (one was adjusted after a split "
                            "or merger) and Robinhood's reply does not say which set each contract belongs to, "
                            f"so this app cannot be sure it is pricing standard 100-share contracts. {HELP}")
        seen: set[tuple[float, str]] = set()
        for c in found:
            terms = (c["strike"], c["option_type"])
            if terms in seen:
                raise DataError(f"Robinhood listed two different {symbol} {expiration} {c['strike']:g} "
                                f"{c['option_type']} contracts, and this app cannot tell which is the standard "
                                f"one. {HELP}")
            seen.add(terms)
        self._instruments[key] = (time.time(), found)
        return found

    def _option_quotes(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        tool = self._tool("option quotes")
        props = properties(tool)
        arg = choose(props, "ids", "instrument_ids", "option_ids", "option_instrument_ids", "instruments",
                     "id", "instrument_id", "option_id")
        if arg is None:
            raise DataError(f"Robinhood's {tool.name} does not take a contract-id argument this app recognises. {HELP}")
        plural = arg.endswith("s")
        stated = props[arg].get("maxItems") if isinstance(props[arg], dict) else None
        # Robinhood's tool states no limit and says larger requests still return quotes, so a whole
        # expiration is asked for in a few large requests. If one is refused, or comes back short,
        # the app goes back to small requests for the contracts it is still missing.
        out: dict[str, dict[str, Any]] = {}
        pending = list(ids)
        while pending:
            size = 1 if not plural else batch_size(props[arg]) if stated else self._quote_batch
            group, pending = pending[:size], pending[size:]
            try:
                got = self._option_quote_batch(tool, props, arg, plural, group)
            except DataError:
                if len(group) <= DEFAULT_BATCH:
                    raise
                log.info("Robinhood refused %d option quotes in one request; using %d from now on",
                         len(group), DEFAULT_BATCH)
                self._quote_batch = DEFAULT_BATCH
                pending = group + pending
                continue
            out.update(got)
            missing = [x for x in group if x not in got]
            if missing and len(group) > DEFAULT_BATCH:
                if len(missing) > len(group) // 2:          # the large request was cut short: stop making them
                    self._quote_batch = DEFAULT_BATCH
                for j in range(0, len(missing), DEFAULT_BATCH):
                    out.update(self._option_quote_batch(tool, props, arg, plural, missing[j:j + DEFAULT_BATCH]))
        return out

    def _option_quote_batch(self, tool: Any, props: dict[str, Any], arg: str, plural: bool,
                            group: list[str]) -> dict[str, dict[str, Any]]:
        """One request for the quotes of `group`, keyed by contract id."""
        out: dict[str, dict[str, Any]] = {}
        payload = self._call(tool, {arg: many(props[arg], group) if plural else group[0]})
        rows = records(payload, "quotes", "option_quotes")
        for raw in rows:
            raw = flatten_quote(raw)
            ident = (pick(raw, "instrument_id", "option_id", "option_instrument_id", "id")
                     or last_path_part(raw.get("instrument")) or last_path_part(raw.get("url")))
            if ident is None and len(group) == 1 and len(rows) == 1:
                ident = group[0]                   # one contract asked for, one answered
            # A quote is matched to its contract by id only, never by its position in the reply:
            # a reply in another order would put one contract's prices on another.
            if ident is None or str(ident) not in group:
                continue
            greeks_raw = raw.get("greeks") if isinstance(raw.get("greeks"), dict) else raw
            iv = num(pick(raw, "implied_volatility", "iv")) or num(pick(greeks_raw, "implied_volatility", "iv"))
            stamp = iso_time(pick(raw, "updated_at", "timestamp"))
            bid_time = iso_time(pick(raw, "venue_bid_time", "bid_time")) or stamp
            ask_time = iso_time(pick(raw, "venue_ask_time", "ask_time")) or stamp
            trade_time = iso_time(pick(raw, "venue_last_trade_time", "last_trade_time")) or stamp
            out[str(ident)] = {
                "bid": num(pick(raw, "bid_price", "bid")), "ask": num(pick(raw, "ask_price", "ask")),
                "bidsize": num(pick(raw, "bid_size", "bidsize")), "asksize": num(pick(raw, "ask_size", "asksize")),
                "last": num(pick(raw, "last_trade_price", "last_price")) or num(pick(raw, "mark_price", "adjusted_mark_price")),
                "volume": num(pick(raw, "volume")) or 0.0,
                "open_interest": num(pick(raw, "open_interest")) or 0.0,
                "bid_time": bid_time, "ask_time": ask_time, "trade_time": trade_time,
                "prevclose": num(pick(raw, "previous_close_price", "previous_close", "adjusted_previous_close",
                                      "official_close")),
                "fill_prices": fill_prices(raw),
                "greeks": {"delta": num(pick(greeks_raw, "delta")), "gamma": num(pick(greeks_raw, "gamma")),
                           "theta": num(pick(greeks_raw, "theta")), "vega": num(pick(greeks_raw, "vega")),
                           "bid_iv": None, "mid_iv": iv, "ask_iv": None, "smv_vol": None,
                           "updated_at": stamp or bid_time},
            }
        return out

    # --- provider interface ----------------------------------------------------
    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        with self._lock:
            option_symbols = [s for s in symbols if s in self._contracts]
            stock_symbols = [display_symbol(s) for s in symbols if s not in self._contracts]
            out = self._equity_quotes(stock_symbols) if stock_symbols else {}
            if option_symbols:
                by_id = self._option_quotes([self._contracts[s]["id"] for s in option_symbols])
                for s in option_symbols:
                    quote = by_id.get(self._contracts[s]["id"])
                    if quote:
                        out[s] = {**quote, "symbol": s, "type": "option",
                                  "description": self._contracts[s]["description"]}
            return out

    def expirations(self, symbol: str) -> list[str]:
        with self._lock:
            return list(self._chain_list(display_symbol(symbol))["expirations"])

    def chain(self, symbol: str, expiration: str) -> list[dict[str, Any]]:
        with self._lock:
            symbol = display_symbol(symbol)
            instruments = self._instrument_list(symbol, expiration)
            spot_quote = self._equity_quotes([symbol]).get(symbol)
            spot = (spot_quote or {}).get("last")
            chosen = instruments
            if spot and len(instruments) > WHOLE_CHAIN_LIMIT:      # an unusually long ladder: leave out the far ends
                near = [c for c in instruments if abs(c["strike"] / spot - 1.0) <= STRIKE_WINDOW]
                chosen = near if len(near) >= 10 else instruments
            quotes = self._option_quotes([c["id"] for c in chosen])
            contracts: list[dict[str, Any]] = []
            for c in chosen:
                # A listed contract Robinhood sent no quote for is still shown, as having no quote.
                quote = quotes.get(c["id"]) or dict(NO_QUOTE, greeks={})
                occ = occ_symbol(symbol, expiration, c["option_type"], c["strike"])
                description = f"{symbol} {expiration} {c['strike']:g} {c['option_type']}"
                self._contracts[occ] = {"id": c["id"], "description": description}
                contracts.append({"symbol": occ, "underlying": symbol, "description": description,
                                  "option_type": c["option_type"], "strike": c["strike"],
                                  "expiration": expiration, "contract_size": 100,
                                  "min_ticks": c.get("min_ticks"), **quote})
            usable = [c for c in contracts if c["bid"] is not None or c["ask"] is not None]
            if not usable:
                raise DataError(f"Robinhood returned {len(chosen)} contracts for {symbol} {expiration} but no "
                                f"readable quotes for them. {HELP}")
            return contracts

    def history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        symbol = display_symbol(symbol)
        wanted_days = max((date.fromisoformat(end) - date.fromisoformat(start)).days, 1)
        own, reason = None, ""
        try:
            with self._lock:
                own = self._history(symbol, start, end)
        except DataError as exc:
            reason = str(exc)
        if own is not None and len(own) >= 60:
            covered = (own["date"].iloc[-1] - own["date"].iloc[0]).days
            if covered >= 0.8 * wanted_days or self.history_fallback is None:
                return own
            reason = f"Robinhood supplied only {covered} days of the {wanted_days} asked for"
        if self.history_fallback is not None:
            log.info("Using %s for %s price history (%s)", self.history_fallback.info.label, symbol, reason)
            return self.history_fallback.history(symbol, start, end)
        if own is not None and len(own):
            return own
        raise DataError(f"No price history for {symbol} from Robinhood ({reason or 'empty reply'}). Add a Tradier "
                        "token to .env and it will be used for price history instead.")

    def _history(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        tool = self._tool("price history")
        props = properties(tool)
        args: dict[str, Any] = {}
        sym_arg = choose(props, "symbols", "symbol", "ticker", "tickers")
        if sym_arg is None:
            raise DataError(f"Robinhood's {tool.name} does not take a symbol argument this app recognises")
        args[sym_arg] = many(props[sym_arg], [symbol]) if sym_arg.endswith("s") else symbol
        if "interval" in props:
            daily = ("day", "1d", "daily", "1day", "d")
            offered = enum_of(props["interval"])
            if offered and not any(o.lower() in daily for o in offered):
                raise DataError(f"Robinhood's {tool.name} does not offer daily bars (it offers {', '.join(offered)})")
            args["interval"] = from_enum(props["interval"], daily, "day")
        if "bounds" in props:
            args["bounds"] = from_enum(props["bounds"], ("regular",), "regular")
        start_arg = choose(props, "start", "start_date", "from", "start_time", "begin")
        end_arg = choose(props, "end", "end_date", "to", "end_time")
        def moment(arg: str, day: str, end_of_day: bool) -> str:
            """A date in the form the argument wants: a plain date, or a full time when it is a *_time."""
            wants = str((props.get(arg) or {}).get("description") or "") if isinstance(props.get(arg), dict) else ""
            if arg.endswith("_time") or "RFC3339" in wants or "ISO 8601" in wants:
                return f"{day}T23:59:59Z" if end_of_day else f"{day}T00:00:00Z"
            return day
        if start_arg:
            args[start_arg] = moment(start_arg, start, False)
        if end_arg:
            args[end_arg] = moment(end_arg, end, True)
        spans = [None]
        if "span" in props:
            years = (date.fromisoformat(end) - date.fromisoformat(start)).days / 365.25
            order = ("5year", "5y", "all", "max", "year", "1y") if years > 1.05 else ("year", "1y", "5year", "5y")
            allowed = {a.lower(): a for a in enum_of(props["span"])}
            spans = [allowed[s] for s in order if s in allowed] if allowed else ["5year", "year"]
            spans = spans or [enum_of(props["span"])[0]]
        last_error: DataError | None = None
        for span in spans:
            try:
                payload = self._call(tool, {**args, **({"span": span} if span else {})})
            except DataError as exc:           # for example a span that does not allow daily bars
                last_error = exc
                continue
            bars = find_bars(unwrap(payload))
            if bars:
                days = [b["date"] for b in bars]
                if len(set(days)) < len(days):
                    last_error = DataError("Robinhood returned more than one bar per day (bars within the day, "
                                           "not daily bars)")
                    continue
                frame = bars_frame(bars)
                # The market is shut at weekends. Bars dated on them mean this app is reading the bars'
                # times wrongly and every date may be a day out.
                weekend = int((frame["date"].dt.dayofweek >= 5).sum())
                if weekend > max(1, len(frame) // 50):
                    last_error = DataError(f"{weekend} of Robinhood's {len(frame)} daily bars fall on a weekend, "
                                           "so their dates are being read wrongly")
                    continue
                frame = frame[(frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))]
                if len(frame) > 2:
                    gaps = frame["date"].diff().dt.days.dropna()
                    if gaps.median() > 3:
                        last_error = DataError("Robinhood returned weekly rather than daily bars")
                        continue
                return frame.reset_index(drop=True)
        raise last_error or DataError("Robinhood returned no price bars this app could read")

    def earnings_raw(self, symbol: str) -> Any:
        """Robinhood's earnings reply for one stock, exactly as sent. Only the check command reads this,
        so that the reply's layout is known before the app starts relying on it."""
        with self._lock:
            tool = self._tool("earnings dates")
            props = properties(tool)
            arg = choose(props, "symbol", "symbols", "ticker", "equity_symbol")
            if arg is None:
                raise DataError(f"Robinhood's {tool.name} does not take a symbol argument this app recognises")
            symbol = display_symbol(symbol)
            return self._call(tool, {arg: many(props[arg], [symbol]) if arg == "symbols" else symbol})

    def clock(self) -> dict[str, Any]:
        """Regular trading hours from the New York clock. Market holidays are not known here."""
        if self.history_fallback is not None:
            try:
                return self.history_fallback.clock()
            except DataError:
                pass
        now = datetime.now(NY)
        minutes = now.hour * 60 + now.minute
        if now.weekday() >= 5:
            state = "closed"
        elif 570 <= minutes < 960:
            state = "open"
        elif 240 <= minutes < 570:
            state = "pre"
        elif 960 <= minutes < 1200:
            state = "post"
        else:
            state = "closed"
        return {"state": state, "description": "Regular hours by the clock; market holidays are not known",
                "date": now.date().isoformat(), "next_change": "", "next_state": ""}


def find_bars(data: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Daily bars from a history reply, wherever they are nested, as date/open/high/low/close/volume."""
    if depth > 5:
        return []
    if isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
        bars = []
        for raw in rows:
            if raw.get("interpolated") is True:
                continue          # Robinhood's filler for a gap: it is not a traded day
            when = pick(raw, "begins_at", "date", "timestamp", "time", "start_time", "begin_time", "open_time",
                        "start", "datetime", "ts", "t")
            close = num(pick(raw, "close_price", "close", "c"))
            if when is None or close is None:
                continue
            stamp = iso_time(when)
            if stamp is None:
                continue
            moment = datetime.fromisoformat(stamp)
            # Robinhood stamps a daily bar with midnight UTC of its trading day; a bar stamped with a
            # time of day belongs to the New York date at that moment.
            day = moment.date() if (moment.hour, moment.minute, moment.second) == (0, 0, 0) \
                else moment.astimezone(NY).date()
            bars.append({"date": day.isoformat(), "open": num(pick(raw, "open_price", "open", "o")),
                         "high": num(pick(raw, "high_price", "high", "h")),
                         "low": num(pick(raw, "low_price", "low", "l")), "close": close,
                         "volume": num(pick(raw, "volume", "v")) or 0.0})
        if bars:
            return bars
        for raw in rows:
            nested = find_bars(raw, depth + 1)
            if nested:
                return nested
        return []
    if isinstance(data, dict):
        for key in ("historicals", "bars", "candles", "results", "data", "items"):
            if key in data:
                nested = find_bars(data[key], depth + 1)
                if nested:
                    return nested
        for value in data.values():
            if isinstance(value, (list, dict)):
                nested = find_bars(value, depth + 1)
                if nested:
                    return nested
    return []


def make_robinhood(token_path, callback_port: int, interactive: bool, history_fallback: MarketData | None,
                   poll_seconds: float = 3.0, url: str = SERVER_URL, account_read: bool = False) -> RobinhoodData:
    from ..robinhood_account import ACCOUNT_ALLOWED
    from .mcp_bridge import SIGN_IN_TIMEOUT, oauth_connector
    bridge = ReadOnlyBridge(oauth_connector(url, token_path, callback_port, interactive), ALLOWED_TOOLS,
                            label="Robinhood", first_call_timeout=SIGN_IN_TIMEOUT + 30 if interactive else 30.0,
                            account_reads=ACCOUNT_ALLOWED if account_read else frozenset())
    provider = RobinhoodData(bridge, history_fallback=history_fallback, poll_seconds=poll_seconds)
    provider.signin = FileTokenStorage(token_path)
    return provider


__all__ = ["RobinhoodData", "make_robinhood", "is_market_data_tool", "ALLOWED_TOOLS", "TOOLS", "SERVER_URL",
           "find_bars", "records", "unwrap"]
