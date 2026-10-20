"""News-to-trade intelligence (2.16): what a headline is, whether its stock can be traded right now, which way
the news should push it, how much of that push the price already holds, and only then an entry signal.

Live news (livenews.py) watches the price after each item and calls a move when it confirms. This module is
the reasoning that runs beside it, on the same quote, so that each item is judged in five steps the screen
can show:

    1. detected     the kind of announcement (an acquisition of the company, an earnings release, a guidance
                    cut, an FDA decision, an offering, ...) read from the headline, the 8-K item codes or RTPR's
                    event type, with a severity from 0 to 100; "major" at 60 and above
    2. tradable     the quote now: a usable price, the spread, how old the last trade is (a liquid stock
                    with no print for a minute and a half in regular hours is most likely halted), the
                    session, the stock's size
    3. effect       the direction the news should push the price, how far such news usually moves a stock,
                    and what the move usually does after its first minute: pinned (a takeover target trades
                    to the deal price and stays there), drift (earnings and guidance keep going for days),
                    fade (contract wins give it back), or mixed
    4. priced in    the move already made, as a share of what such news usually makes: the first quote the
                    app gets already holds whatever moved in the seconds before it arrived, and a stock up
                    more than the usual move has nothing left to give
    5. signal       enter (a tradable stock, a known direction, a move not yet made, and a kind of news that
                    keeps going), wait (the price has not answered yet, or the record is too thin), or pass
                    (not tradable, fully priced, or a kind of news whose move is over in a second)

The record behind step 5 comes from the replay harness (newsreplay.py): for each kind of news, what entering
30 seconds after the headline and holding 30 minutes has made, after the spread and slippage. Everything here
is arithmetic on text and quotes, so it can be tested alone, and nothing here places an order.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from .news import word_tone

MAJOR_SEVERITY = 60                 # at or above this, an item counts as a major announcement
MAX_SPREAD_BPS_OPEN = 50.0          # a spread wider than this in regular hours is too wide to trade on news
MAX_SPREAD_BPS_EXTENDED = 150.0     # ... and outside them
HALT_TRADE_AGE_SECONDS = 90.0       # a liquid stock with no print this long in regular hours is most likely halted
STALE_QUOTE_SECONDS = 15 * 60       # outside regular hours a quote with no trade this recent is not a price
MIN_PRICE = 5.0
MIN_DOLLAR_VOLUME = 20e6
MIN_RECORD_CASES = 8                # fewer cases than this and the second leg is "unproven"
FRESH_RATIO = 0.25                  # under this share of the usual move: the price has not answered yet
FULL_RATIO = 1.25                   # over this share: the move is more than fully made

# What each kind of news does, from the event-study literature and the shape of the trades: direction (+1 up,
# -1 down, 0 from the headline's tone), the usual size of the move for a major instance (percent), what the
# move does after its first minute, and the severity a plain instance earns. The patterns are tried in order;
# the first that matches wins, so the sharper ones come first.
CATALYSTS: dict[str, dict[str, Any]] = {
    "acquisition_target": {"label": "To be acquired", "direction": 1, "typical_pct": 25.0, "persistence": "pinned",
                           "severity": 95, "why": "A takeover target jumps to just under the offer price within a second "
                                                  "and stays there: the move is over before anyone can act, and what is "
                                                  "left is the deal spread."},
    "acquirer": {"label": "Buying a company", "direction": -1, "typical_pct": 3.0, "persistence": "mixed", "severity": 55,
                 "why": "Buyers usually dip on a deal (the price paid, the shares issued); the dip often fades."},
    "bankruptcy": {"label": "Bankruptcy", "direction": -1, "typical_pct": 40.0, "persistence": "drift", "severity": 95,
                   "why": "A filing keeps falling for days; the shares are usually worth little or nothing."},
    "restatement": {"label": "Financial statements withdrawn", "direction": -1, "typical_pct": 15.0,
                    "persistence": "drift", "severity": 85, "why": "Past results can no longer be relied on: a fall "
                                                                    "that tends to continue as the extent becomes clear."},
    "guidance_cut": {"label": "Guidance cut", "direction": -1, "typical_pct": 8.0, "persistence": "drift", "severity": 80,
                     "why": "Guidance cuts keep drifting down for days as estimates are revised."},
    "guidance_raise": {"label": "Guidance raised", "direction": 1, "typical_pct": 5.0, "persistence": "drift", "severity": 70,
                       "why": "Raised guidance tends to keep drifting up as estimates follow."},
    "fda_negative": {"label": "FDA setback or trial failure", "direction": -1, "typical_pct": 30.0, "persistence": "drift",
                     "severity": 90, "why": "A rejection or a failed trial removes a product: the fall is large and "
                                            "tends to continue."},
    "fda_positive": {"label": "FDA approval or trial success", "direction": 1, "typical_pct": 20.0, "persistence": "mixed",
                     "severity": 85, "why": "Approvals and positive trials jump at once; what follows is a coin toss, "
                                            "as the news was often expected."},
    "offering": {"label": "Share or note offering", "direction": -1, "typical_pct": 8.0, "persistence": "drift",
                 "severity": 75, "why": "New shares dilute the old ones and are usually priced at a discount: the stock "
                                        "tends to drift down to the offering price."},
    "earnings": {"label": "Earnings release", "direction": 0, "typical_pct": 6.0, "persistence": "drift", "severity": 75,
                 "why": "After an earnings surprise the price tends to keep drifting the same way for days "
                        "(post-earnings announcement drift)."},
    "buyback_dividend": {"label": "Buyback or dividend", "direction": 1, "typical_pct": 2.0, "persistence": "mixed",
                         "severity": 45, "why": "Buybacks and dividend raises are mildly positive and mostly priced at once."},
    "contract": {"label": "Contract or order win", "direction": 1, "typical_pct": 3.0, "persistence": "fade", "severity": 50,
                 "why": "Contract wins pop and often give the pop back within the session."},
    "legal_regulatory": {"label": "Lawsuit, probe or recall", "direction": -1, "typical_pct": 5.0, "persistence": "drift",
                         "severity": 65, "why": "Legal and regulatory trouble tends to weigh on a stock for days."},
    "delisting": {"label": "Listing notice", "direction": -1, "typical_pct": 6.0, "persistence": "drift", "severity": 65,
                  "why": "A listing deficiency notice is a slow negative."},
    "executive": {"label": "Executive change", "direction": 0, "typical_pct": 3.0, "persistence": "mixed", "severity": 55,
                  "why": "A chief executive leaving is usually negative, a well-known one arriving positive; the move "
                         "depends on the names."},
    "activist": {"label": "Activist stake", "direction": 1, "typical_pct": 5.0, "persistence": "mixed", "severity": 60,
                 "why": "A disclosed activist stake lifts the stock; the lift is usually kept."},
    "analyst": {"label": "Analyst rating", "direction": 0, "typical_pct": 2.5, "persistence": "fade", "severity": 35,
                "why": "Upgrades and downgrades move a stock at the open and often fade by the close."},
    "restructuring": {"label": "Restructuring or job cuts", "direction": 0, "typical_pct": 4.0, "persistence": "mixed",
                      "severity": 50, "why": "Cost cuts can read as discipline or as trouble; the tone decides."},
    "material_agreement": {"label": "Material agreement", "direction": 0, "typical_pct": 3.0, "persistence": "mixed",
                           "severity": 45, "why": "A new material agreement (8-K item 1.01) is whatever the agreement is."},
    "halt": {"label": "Trading halt", "direction": 0, "typical_pct": 0.0, "persistence": "mixed", "severity": 70,
             "why": "The stock is halted: nothing can be traded until it reopens, and it reopens with an auction."},
    "scheduling": {"label": "Scheduled event", "direction": 0, "typical_pct": 0.5, "persistence": "mixed", "severity": 5,
                   "why": "A date for a call or a conference moves nothing."},
    "other": {"label": "Other news", "direction": 0, "typical_pct": 2.0, "persistence": "mixed", "severity": 25,
              "why": "Nothing in the headline marks it as a known kind of announcement."},
}

PERSISTENCE_WORDS = {"pinned": "the move is over in a second and the stock then sits still",
                     "drift": "moves of this kind have tended to keep going for hours or days",
                     "fade": "moves of this kind have tended to give part of themselves back",
                     "mixed": "what follows the first minute has been a coin toss"}

# (catalyst, regex) in the order tried. Headlines are matched in lower case.
_RULES: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(p, re.I)) for k, p in (
    ("halt", r"\btrading (halt|halted|pause)\b|\bhalts? trading\b"),
    ("scheduling", r"\b(to (host|hold|report|announce|release|present|webcast)|schedules?|will (report|host|present|"
                   r"announce|release)|conference call|earnings call|to participate in|investor day|annual meeting|"
                   r"fireside chat)\b.*\b(results|call|conference|webcast|meeting|earnings|quarter|presentation)\b"),
    ("acquisition_target", r"\bto be acquired\b|\bagree(s|d)? to be acquired\b|\bacquired by\b|\breceives? (an? )?"
                           r"(unsolicited |revised |binding |non-binding )?(takeover |acquisition |buyout |cash )?"
                           r"(offer|proposal|bid)\b|\bto be taken private\b|\bgo(es|ing)? private\b|"
                           r"\bbe taken private\b|\bdefinitive agreement to be acquired\b|\bmerger agreement with\b|"
                           r"\bto be bought by\b|\btakeover (bid|offer)\b|\bbuyout\b"),
    ("acquirer", r"\bto acquire\b|\bagree(s|d)? to (acquire|buy|purchase)\b|\bacquires\b|\bcompletes? (the )?"
                 r"acquisition\b|\bacquisition of\b|\bto buy\b|\bto purchase\b|\bannounces? (the )?acquisition\b"),
    ("bankruptcy", r"\bchapter (7|11|15)\b|\bbankruptcy\b|\breceivership\b|\binsolvenc"),
    ("restatement", r"\bnon-reliance\b|\bno longer be relied\b|\brestat(e|es|ement|ing)\b|\bmaterial weakness\b"),
    ("fda_negative", r"\bcomplete response letter\b|\bcrl\b|\bclinical hold\b|\b(fails?|failed) to meet\b|\bdid not meet\b|"
                     r"\bmiss(es|ed)? (its )?primary endpoint\b|\bfda (declines|rejects|refuses)\b|\brefusal to file\b|"
                     r"\bdiscontinu(es|ed|ing) (the |its )?(trial|study|program|development)\b|\badverse event\b|"
                     r"\bsafety concern\b|\btrial (fails|failed|halted)\b"),
    ("fda_positive", r"\bfda (approval|approves|approved|clearance|clears|cleared|grants)\b|\breceives? (fda |marketing |"
                     r"regulatory )?(approval|authorization|clearance)\b|\bmeets? (its )?primary endpoint\b|\bpositive "
                     r"(topline|top-line|phase|results from)\b|\bbreakthrough therapy\b|\bpriority review\b|\bapproves\b"),
    ("guidance_cut", r"\b(cuts?|lowers?|reduces?|trims?|slashes?|withdraws?|suspends?) (its )?(full[- ]year |fiscal |"
                     r"annual |quarterly |\d{4} )?(guidance|outlook|forecast|view)\b|\b(guidance|outlook) (cut|lowered|"
                     r"reduced|withdrawn|below)\b|\bbelow (consensus|expectations|estimates)\b|\bwarns?\b.*\b(revenue|sales|"
                     r"profit|earnings|demand)\b|\bprofit warning\b"),
    ("guidance_raise", r"\b(raises?|lifts?|boosts?|increases?|hikes?) (its )?(full[- ]year |fiscal |annual |quarterly |"
                       r"\d{4} )?(guidance|outlook|forecast|view)\b|\b(guidance|outlook) (raised|lifted|above)\b|"
                       r"\babove (consensus|expectations|estimates)\b"),
    ("offering", r"\b(public|secondary|registered direct|underwritten|follow-on|common stock|equity|share|convertible|"
                 r"senior notes?|notes?) offering\b|\bprices? (its |an? )?(public |secondary |upsized )?offering\b|"
                 r"\bprivate placement\b|\bat-the-market\b|\bproposed offering\b|\bshelf registration\b|\bpipe financing\b"),
    ("earnings", r"\b(reports?|announces?|posts?|delivers?) (record |strong |solid |preliminary |unaudited )?"
                 r"(first|second|third|fourth|1st|2nd|3rd|4th|q[1-4]|fiscal|full[- ]year|half[- ]year|annual)"
                 r"[- ](quarter|year|and|fiscal|results|\d{4})\b|\bquarterly results\b|\b(q[1-4]|fy)\s?\d{2,4} results\b|"
                 r"\bresults of operations\b|\bearnings (release|results|report)\b|\b(beats?|tops?|misses?) (estimates|"
                 r"expectations|consensus|forecasts|the street)\b|\b(eps|earnings per share) of\b|\bquarterly (revenue|"
                 r"earnings|profit|loss)\b|\brevenue (of|rose|fell|grew|up|down) \$?\d"),
    ("buyback_dividend", r"\b(share|stock) (repurchase|buyback)\b|\bbuyback\b|\brepurchase program\b|\b(raises|increases|"
                         r"hikes|declares) (its )?(quarterly |annual |special )?dividend\b|\bspecial dividend\b"),
    ("activist", r"\b(13d|activist|takes? (an? )?(\d+(\.\d+)?% )?stake)\b|\bnominates? directors\b|\bproxy (fight|contest)\b"),
    ("legal_regulatory", r"\b(lawsuit|class action|sues|sued|subpoena|investigation|probe|indict|charges|charged|settles?|"
                         r"settlement|recall|recalls|injunction|antitrust|doj|ftc|sec charges|fine[ds]?|penalty|"
                         r"cease and desist|data breach|cyberattack|ransomware)\b"),
    ("delisting", r"\b(delist|delisting|deficiency notice|non-compliance|minimum bid price|listing (rule|standard|notice)|"
                  r"nasdaq notice|nyse notice)\b"),
    ("executive", r"\b(chief (executive|financial|operating|technology|medical)( officer)?|ceo|cfo|coo|cto|president|chairman|chair|director|"
                  r"founder)\b.*\b(steps? down|resign|resigns|resigned|departs?|departure|retire|retires|retirement|"
                  r"to leave|leaves|leaving|appoint|appoints|appointed|names|named|hires|promot|succeed|successor|"
                  r"transition|terminated|dismissed|fired)\b|\b(appoints|names) .* (as )?(chief|ceo|cfo|president)\b"),
    ("analyst", r"\b(upgrades?|downgrades?|initiates? coverage|price target|rating to|reiterates?)\b"),
    ("restructuring", r"\b(restructur|layoffs?|job cuts|workforce reduction|cuts? \d+(,\d+)?% of|cost[- ]cutting|"
                      r"cost reduction|plant closure|closes? (its )?\w+ plant)\b"),
    ("contract", r"\b(award(ed|s)?|wins?|won|secures?|selected by|receives? (an? )?(order|contract)|contract (worth|"
                 r"valued|with)|multi-year (contract|agreement|deal)|purchase order)\b"),
)]

# RTPR's event types and the SEC's 8-K item codes, mapped onto the same kinds.
RTPR_EVENTS = {"merger": "acquisition_target", "acquisition": "acquisition_target", "takeover": "acquisition_target",
               "earnings": "earnings", "guidance": "earnings", "guidance_raise": "guidance_raise",
               "guidance_cut": "guidance_cut", "fda_approval": "fda_positive", "fda_rejection": "fda_negative",
               "clinical_trial": "fda_positive", "trial_failure": "fda_negative", "offering": "offering",
               "dilution": "offering", "buyback": "buyback_dividend", "dividend": "buyback_dividend",
               "contract": "contract", "lawsuit": "legal_regulatory", "investigation": "legal_regulatory",
               "recall": "legal_regulatory", "bankruptcy": "bankruptcy", "executive_change": "executive",
               "ceo_change": "executive", "activist": "activist", "analyst": "analyst", "layoffs": "restructuring",
               "restructuring": "restructuring", "halt": "halt"}
SEC_ITEMS = {"1.03": "bankruptcy", "4.02": "restatement", "2.02": "earnings", "2.01": "acquirer",
             "2.06": "restatement", "2.05": "restructuring", "5.02": "executive", "3.01": "delisting",
             "1.01": "material_agreement", "2.03": "offering", "3.02": "offering", "4.01": "legal_regulatory"}


def _tone_direction(text: str, tone: float | None) -> int:
    if isinstance(tone, (int, float)) and math.isfinite(tone) and tone:
        return 1 if tone > 0 else -1
    return int(word_tone(text))


def classify(headline: str, summary: str = "", *, kind: str | None = None, extra: dict[str, Any] | None = None,
             tone: float | None = None) -> dict[str, Any]:
    """What an item is: its kind, the direction the news should push the price (+1, -1 or 0 when the headline
    gives none), the usual size of such a move, what the move does after its first minute, and a severity from
    0 to 100. The sources' own words come first (RTPR's event type, the 8-K item codes), then the headline."""
    extra = extra or {}
    text = " ".join(f"{headline or ''} {summary or ''}".split())
    low = text.lower()
    key, via = None, None
    impact = extra.get("impact") if isinstance(extra.get("impact"), dict) else {}
    event = str(impact.get("event_type") or extra.get("event_type") or "").strip().lower()
    if event and event in RTPR_EVENTS:
        key, via = RTPR_EVENTS[event], "RTPR event type"
    if key is None and kind == "filing":
        for code in extra.get("items") or []:
            if code in SEC_ITEMS:
                key, via = SEC_ITEMS[code], f"8-K item {code}"
                break
    if key is None:
        for name, pattern in _RULES:
            if pattern.search(low):
                key, via = name, "headline"
                break
    if key is None:
        key, via = "other", "headline"
    spec = CATALYSTS[key]
    direction = int(spec["direction"]) or _tone_direction(text, tone)
    # RTPR's own direction wins when it sends one: it read the whole release.
    if impact.get("direction") in ("bullish", "bearish"):
        direction = 1 if impact["direction"] == "bullish" else -1
    severity = int(spec["severity"])
    if re.search(r"\b(record|billion|unsolicited|all-cash|premium of \d|\d{2,3}% premium)\b", low):
        severity = min(100, severity + 10)
    if key == "earnings" and direction == 0:
        severity -= 15                                # results with no word on how they compared: little to trade
    score = impact.get("score")
    if isinstance(score, (int, float)) and math.isfinite(score):
        severity = int(round(0.5 * severity + 0.5 * max(0.0, min(100.0, float(score)))))
    severity = max(0, min(100, severity))
    return {"catalyst": key, "label": spec["label"], "direction": direction, "typical_pct": float(spec["typical_pct"]),
            "persistence": spec["persistence"], "severity": severity, "major": severity >= MAJOR_SEVERITY,
            "via": via, "why": spec["why"]}


# --- tradable now? -------------------------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 else None


def _age(stamp: Any, now: datetime) -> float | None:
    from .livenews import parse_time
    when = parse_time(stamp)
    return (now - when).total_seconds() if when is not None else None


def tradability(quote: dict[str, Any], session: str, daily: dict[str, Any] | None, *, now: datetime | None = None,
                holdings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Whether the stock can be traded on this news right now, from the quote alone, with every reason given.

    `state` is one word for the screen: ok, closed, no_price, halt_suspected, stale, wide_spread, thin or
    cheap. A liquid stock with no print for a minute and a half in regular hours is most likely halted (a
    volatility halt lasts five minutes, a news-pending halt longer): nothing can be bought or sold, and it
    reopens with an auction that usually sets the price well away from the last print."""
    now = now or datetime.now(timezone.utc)
    quote = quote or {}
    last, bid, ask = _num(quote.get("last")), _num(quote.get("bid")), _num(quote.get("ask"))
    mid = 0.5 * (bid + ask) if bid and ask and ask >= bid else None
    price = mid if session == "open" and mid and (ask - bid) / ask < 0.01 else last or mid
    spread_bps = 1e4 * (ask - bid) / mid if mid else None
    trade_age = _age(quote.get("trade_time"), now)
    ages = [a for a in (_age(quote.get("bid_time"), now), _age(quote.get("ask_time"), now)) if a is not None]
    quote_age = min(ages) if ages else None
    dollar_volume = (daily or {}).get("dollar_volume")
    liquid = isinstance(dollar_volume, (int, float)) and dollar_volume >= MIN_DOLLAR_VOLUME
    notes: list[str] = []
    state = "ok"
    if session == "closed":
        state = "closed"
        notes.append("The market is closed: the stock is priced when trading resumes (pre-market included), and "
                     "overnight news is usually fully in the opening price.")
    elif price is None:
        state = "no_price"
        notes.append("No usable price in the quote.")
    elif session == "open" and liquid and trade_age is not None and trade_age > HALT_TRADE_AGE_SECONDS:
        state = "halt_suspected"
        notes.append(f"No trade for {trade_age / 60:.1f} minutes in regular hours on a stock that trades "
                     f"${dollar_volume / 1e6:,.0f} million a day: most likely halted. It reopens with an auction, "
                     "usually well away from the last print.")
    elif session == "open" and not (bid and ask):
        state = "halt_suspected"
        notes.append("No bid or ask in regular hours: the book is empty, which is what a halt looks like.")
    elif session == "extended" and trade_age is not None and trade_age > STALE_QUOTE_SECONDS:
        state = "stale"
        notes.append(f"Outside regular hours and the last trade was {trade_age / 60:.0f} minutes ago: the price is "
                     "not current.")
    elif price is not None and price < MIN_PRICE:
        state = "cheap"
        notes.append(f"Under ${MIN_PRICE:g}: too thin and too easily pushed to trade on news.")
    elif spread_bps is not None and spread_bps > (MAX_SPREAD_BPS_OPEN if session == "open" else MAX_SPREAD_BPS_EXTENDED):
        state = "wide_spread"
        notes.append(f"The spread is {spread_bps:.0f} basis points ({ask - bid:.2f} on ${mid:,.2f}): crossing it "
                     "costs a good part of a news move, and it is widest right after a headline.")
    elif daily is not None and not liquid and dollar_volume is not None:
        state = "thin"
        notes.append(f"Trades about ${dollar_volume / 1e6:,.0f} million a day, under the ${MIN_DOLLAR_VOLUME / 1e6:,.0f} "
                     "million this screen needs: a news order would move the price itself.")
    if state == "ok":
        if spread_bps is not None:
            notes.append(f"Spread {spread_bps:.0f} bps; last trade {trade_age:.0f} s ago." if trade_age is not None
                         else f"Spread {spread_bps:.0f} bps.")
        if session == "extended":
            notes.append("Extended hours: thinner books and wider spreads than the session; a limit order only.")
    sizes = (_num(quote.get("bidsize")), _num(quote.get("asksize")))
    held = [h.get("what") for h in (holdings or []) if h.get("what")]
    if held:
        notes.append("You hold " + "; ".join(held) + ".")
    return {"ok": state == "ok", "state": state, "price": price, "bid": bid, "ask": ask, "spread_bps": spread_bps,
            "trade_age_s": trade_age, "quote_age_s": quote_age, "sizes": sizes, "session": session,
            "dollar_volume": dollar_volume, "notes": notes}


# --- how much of the move is already made ----------------------------------------------------------------------

def priced_in(*, ref_price: float | None, price: float | None, prev_close: float | None, atr: float | None,
              catalyst: dict[str, Any], record: dict[str, Any] | None = None, in_session: bool = True,
              arrival_delay_s: float | None = None) -> dict[str, Any]:
    """The move the price has already made against what such news usually makes.

    During the session the move is measured from the app's first quote (`ref_price`), which already holds
    whatever moved in the seconds before it arrived; outside the session, from the last close, since the day's
    move is the reaction. The usual move is the kind's typical size, or the record's median first-minute jump
    when enough replays exist. The ratio is signed by the news's direction: negative means the price went the
    other way."""
    d = int(catalyst.get("direction") or 0)
    since = 100 * (price / ref_price - 1) if price and ref_price else None
    day = 100 * (price / prev_close - 1) if price and prev_close else None
    move = since if in_session else day
    basis = "since the app's first quote" if in_session else "on the day, from the last close"
    usual = float(catalyst.get("typical_pct") or 0.0)
    source = "the usual size of such news"
    if record and (record.get("cases") or 0) >= MIN_RECORD_CASES and isinstance(record.get("median_jump_pct"), (int, float)):
        usual, source = abs(float(record["median_jump_pct"])), f"the record of {record['cases']} replays"
    ratio = None
    if move is not None and usual > 0:
        ratio = (d or (1 if move >= 0 else -1)) * move / usual
    if ratio is None:
        state, text = "unknown", "No price to measure the move with yet."
    elif ratio < -FRESH_RATIO:
        state, text = "against", (f"The price went the other way: {move:+.1f}% {basis}, against news that usually "
                                  f"moves a stock {usual:.0f}% {'up' if d > 0 else 'down' if d < 0 else 'either way'}.")
    elif ratio < FRESH_RATIO:
        state, text = "fresh", (f"Little of the usual move is made: {move:+.1f}% {basis}, against about {usual:.0f}% "
                                f"for such news. Either the price has not answered yet or the market reads it as minor.")
    elif ratio < 0.75:
        state, text = "partly", f"About {100 * ratio:.0f}% of the usual move is made ({move:+.1f}% {basis})."
    elif ratio < FULL_RATIO:
        state, text = "mostly", (f"Most of the usual move is made ({move:+.1f}% {basis}, {100 * ratio:.0f}% of "
                                 f"about {usual:.0f}%): little is left for anyone entering now.")
    else:
        state, text = "full", (f"More than the usual move is made ({move:+.1f}% {basis}, {100 * ratio:.0f}% of about "
                               f"{usual:.0f}%): nothing is left, and an overshoot can snap back.")
    if in_session and arrival_delay_s is not None and ratio is not None and ratio >= FRESH_RATIO:
        text += (f" The first quote came {arrival_delay_s:.1f} s after publication, so the first seconds' move is "
                 "already in it.")
    atr_pct = 100 * atr / prev_close if atr and prev_close else None
    return {"state": state, "ratio": ratio, "move_pct": move, "since_pct": since, "day_pct": day, "usual_pct": usual,
            "usual_from": source, "basis": basis, "atr_pct": atr_pct, "text": text}


# --- the five steps ----------------------------------------------------------------------------------------------

def second_leg(record: dict[str, Any] | None) -> dict[str, Any]:
    """What the replays say about entering 30 seconds after such news and holding 30 minutes, after costs."""
    if not record or not record.get("cases"):
        return {"verdict": "unproven", "cases": 0, "text": "No replays of this kind of news yet: run the replay below "
                                                           "to learn whether a second leg has been there to take."}
    cases = int(record["cases"])
    median = record.get("median_net_pct")
    hit = record.get("hit_rate")
    label = record.get("label") or "this kind of news"
    if cases < MIN_RECORD_CASES or median is None:
        return {"verdict": "unproven", "cases": cases, "median_net_pct": median, "hit_rate": hit,
                "text": f"Only {cases} replay{'s' if cases != 1 else ''} of {label}: too few to call "
                        f"(at least {MIN_RECORD_CASES} are needed)."}
    if median > 0 and (hit or 0) >= 0.5:
        verdict = "exploitable"
        text = (f"In {cases} replays of {label}, entering 30 s after the headline and holding 30 minutes made a median "
                f"{median:+.2f}% after the spread and slippage, positive {100 * hit:.0f}% of the time: a second leg has "
                "been there to take.")
    else:
        verdict = "gone"
        text = (f"In {cases} replays of {label}, entering 30 s after the headline and holding 30 minutes made a median "
                f"{median:+.2f}% after costs, positive {100 * (hit or 0):.0f}% of the time: the move was over before "
                "a trader reacting to the headline could enter.")
    return {"verdict": verdict, "cases": cases, "median_net_pct": median, "hit_rate": hit, "text": text,
            "median_captured": record.get("median_captured")}


def assess(item: dict[str, Any], quote: dict[str, Any], session: str, daily: dict[str, Any] | None, *,
           ref_price: float | None, prev_close: float | None, record: dict[str, Any] | None = None,
           now: datetime | None = None, holdings: list[dict[str, Any]] | None = None,
           in_session_news: bool | None = None, reaction_pct: float = 1.0, price: float | None = None) -> dict[str, Any]:
    """The five steps for one item on one stock, from the quote in hand (and `price`, the price the feed judged the
    move with, when it differs from the quote's). Returns the chain the screen shows and the entry signal: enter,
    wait or pass, with the reason in words."""
    from .livenews import parse_time
    now = now or datetime.now(timezone.utc)
    cat = classify(item.get("headline") or "", item.get("summary") or "", kind=item.get("kind"),
                   extra=item.get("extra") or {}, tone=item.get("tone"))
    trade = tradability(quote, session, daily, now=now, holdings=holdings)
    published = parse_time(item.get("published_at")) or parse_time(item.get("received_at"))
    received = parse_time(item.get("received_at"))
    delay = (received - published).total_seconds() if received and published else None
    if in_session_news is None:
        in_session_news = session == "open" and delay is not None and delay < 120
    price = price if price else trade["price"]
    pin = priced_in(ref_price=ref_price, price=price, prev_close=prev_close, atr=(daily or {}).get("atr"),
                    catalyst=cat, record=record, in_session=in_session_news, arrival_delay_s=delay)
    leg = second_leg(record)
    d = cat["direction"]
    way = "up" if d > 0 else "down" if d < 0 else "either way"
    steps = [
        {"step": "detected", "state": "pass" if cat["major"] else "info",
         "title": f"{cat['label']} ({'major' if cat['major'] else 'minor'}, {cat['severity']}/100)",
         "detail": f"Read from the {cat['via']}. {cat['why']}"},
        {"step": "tradable", "state": "pass" if trade["ok"] else "fail" if trade["state"] in ("halt_suspected", "closed", "no_price") else "warn",
         "title": {"ok": "Tradable now", "closed": "Market closed", "no_price": "No price", "halt_suspected": "Halt suspected",
                   "stale": "Stale quote", "wide_spread": "Spread too wide", "thin": "Too thin", "cheap": "Under $5"}[trade["state"]],
         "detail": " ".join(trade["notes"])},
        {"step": "effect", "state": "pass" if d else "warn",
         "title": (f"Should push it {way}" if d else "Direction unclear") + f" · usually about {cat['typical_pct']:.0f}%",
         "detail": PERSISTENCE_WORDS[cat["persistence"]].capitalize() + "."},
        {"step": "priced_in", "state": {"fresh": "pass", "partly": "pass", "mostly": "warn", "full": "fail", "against": "warn",
                                        "unknown": "info"}[pin["state"]],
         "title": {"fresh": "Not yet in the price", "partly": "Partly in the price", "mostly": "Mostly in the price",
                   "full": "Fully in the price", "against": "Price went the other way", "unknown": "No price yet"}[pin["state"]],
         "detail": pin["text"]},
    ]
    # The signal. Enter only when every step allows it; otherwise say which one does not.
    action, why = "pass", ""
    if not trade["ok"]:
        why = {"closed": "Wait for the market: the stock is priced when trading resumes, and the opening price "
                         "usually holds the news already.",
               "halt_suspected": "Halted: nothing can be traded until it reopens, and the reopening auction sets the "
                                 "price, not the last print.",
               "no_price": "No usable price to enter at.", "stale": "The quote is stale; wait for a trade.",
               "wide_spread": "The spread would eat the move; wait for it to settle or pass.",
               "thin": "Too thin to trade on news without moving the price yourself.",
               "cheap": "Under $5: not traded here."}[trade["state"]]
        if trade["state"] == "closed":
            action = "wait"
    elif not d:
        why = ("The headline gives no direction to trade, so the price must show the way first: Live news calls it "
               "when the move confirms.")
        action = "wait"
    elif cat["persistence"] == "pinned":
        why = ("A takeover target moves to the deal price within a second and stays there: the jump was never "
               "available, and what is left is the deal spread, not a trade on the news.")
    elif pin["state"] == "full":
        why = "More than the usual move is already made: nothing is left to enter for, and an overshoot can snap back."
    elif pin["state"] == "against":
        why = ("The price went against the headline's direction: the market reads the news differently. The price is "
               "what is traded, not the words, so wait for Live news to call the move.")
        action = "wait"
    elif pin["state"] == "mostly" and cat["persistence"] != "drift":
        why = "Most of the usual move is made and this kind of news has not kept going after its first minute."
    elif cat["severity"] < 40:
        why = f"A minor item ({cat['severity']}/100): listed, not traded."
    elif leg["verdict"] == "gone":
        why = leg["text"]
    elif pin["state"] == "fresh" and abs(pin.get("since_pct") or 0.0) < reaction_pct:
        why = (f"Little has moved yet ({pin['move_pct']:+.1f}%): a stock that does not answer major news is telling "
               "you the market read it as minor. Wait for the price to confirm the direction; Live news calls it at "
               f"{reaction_pct:g}%.")
        action = "wait"
    else:
        action = "enter"
        made = {"fresh": "the move is not yet made", "partly": "only part of the usual move is made",
                "mostly": "most of the move is made but this kind keeps going"}[pin["state"]]
        why = f"Tradable, the news should push it {way}, {made}, and {PERSISTENCE_WORDS[cat['persistence']]}."
        if leg["verdict"] == "exploitable":
            why += f" {leg['text']}"
        elif leg["verdict"] == "unproven":
            why += " The second leg is unproven here: size it as a setup, not a sure thing."
    direction = ("LONG" if d > 0 else "SHORT") if action == "enter" else None
    steps.append({"step": "signal", "state": {"enter": "pass", "wait": "warn", "pass": "fail"}[action],
                  "title": {"enter": f"Enter {'long' if d > 0 else 'short'}", "wait": "Wait", "pass": "Pass"}[action],
                  "detail": why})
    return {"catalyst": cat, "tradable": trade, "priced_in": pin, "second_leg": leg, "steps": steps,
            "signal": {"action": action, "direction": direction, "why": why},
            "arrival_delay_s": delay, "in_session_news": in_session_news, "assessed_at": now.isoformat(timespec="seconds")}


__all__ = ["CATALYSTS", "classify", "tradability", "priced_in", "second_leg", "assess", "MAJOR_SEVERITY"]
