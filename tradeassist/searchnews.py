"""News found by searching (2.11): the answer to "why is this stock moving?", for $0.

Two search sources, used on demand rather than streamed:

    Google News   the search feed behind news.google.com (RSS). Free, no key, no card; an unofficial feed for
                  personal reading. It has no published rate limit, so requests are spaced out (one every two
                  seconds, shared by every copy of the app using this database) and the source pauses for 15
                  minutes after a refusal. On by default (GOOGLE_NEWS=0 switches it off).
    Brave         the Brave Search API's news endpoint (BRAVE_API_KEY). Brave's free tier ended in February 2026:
                  a key now comes with $5 of credit a month (about 1,000 searches at $5 per 1,000) and a card that
                  is charged automatically past it. So the app keeps a hard cap of its own: BRAVE_MONTHLY_LIMIT
                  (900) searches in any 31 days, so no billing month can pass it, and BRAVE_DAILY_LIMIT (40) a
                  day, counted in the database before each request. Brave is asked only when nothing else found
                  news, so the credit lasts.

A search engine indexes news minutes after it is published, not seconds: this is for knowing why a stock moved
(and that nothing was found when nothing was), not for being first. The fastest source is still RTPR.

Search results carry no ticker, so a result counts for a stock only when its headline names the company the
way news writes it (NEWS_NAMES for the large stocks the app follows; the SEC's or the quote's name otherwise),
or the ticker when the ticker cannot be mistaken for a word. Results the search returned that could not be
tied to the stock are counted (`unmatched`), so a caller can tell "nothing came back" from "something came
back that might be about it".
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import logging
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from .events import emit, redact
from .news import word_tone

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
BRAVE_NEWS = "https://api.search.brave.com/res/v1/news/search"
GOOGLE_GAP_SECONDS = 2.0          # between two Google News requests, from any copy of the app on this database
GOOGLE_BACKOFF_SECONDS = 900      # after Google refuses (403, 429, 503 or a page that is not a feed)
GOOGLE_MAX_WAIT_SECONDS = 30.0    # a search that would wait longer than this for its turn is not made
BRAVE_GAP_SECONDS = 1.1           # Brave allows about one request a second on the smallest plans
BRAVE_WINDOW_DAYS = 31            # BRAVE_MONTHLY_LIMIT counts the searches in any 31 days: no billing month passes it
NAME_DAYS = 30
MAX_RESULTS = 20
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) TradeAssistant/2.11 (personal news reader)"

# Tickers that are also words or common abbreviations: never matched as a bare ticker, only by the company's
# name, "$XX" or "(NYSE: XX)". Every one- and two-letter ticker is treated the same way.
COMMON_WORDS = {"ALL", "AMT", "APP", "ARE", "BIG", "CAN", "CAR", "CAT", "COP", "CRM", "DAY", "DOW", "EAT", "FAST",
                "FOR", "FUN", "GIS", "GOOD", "HAS", "HOLD", "HOME", "ICE", "KEY", "LOW", "MAN", "MET", "NET", "NEW",
                "NOW", "OPEN", "OUT", "PAY", "PEG", "PLAY", "PSA", "RTX", "RUN", "SAVE", "SEE", "SNAP", "TEAM", "TECH",
                "TRUE", "TWO", "USB", "VERY", "WELL", "YOU", "ONE", "AI", "FANG", "COST", "SHOP", "SNOW", "SPOT",
                "HOOD", "PINS", "DASH", "DECK", "KEYS", "COIN", "HUM", "MAR", "PEP", "HAL", "AXON", "MAG"}
NAME_SUFFIXES = re.compile(
    r"(,?\s+(inc\.?|incorporated|corp\.?|corporation|co\.?|cos\.?|company|companies|ltd\.?|limited|plc|n\.?v\.?|"
    r"s\.?a\.?|ag|se|holdings?|group|class [a-c]|cl [a-c]|common stock|ordinary shares|shares|adr|the|"
    r"\(the\)|\(new\)|new|de|hldgs?)|\s*/[a-z]{2,4}/?|\s*\\[a-z]{2,4}\\?)+\s*$", re.I)
# One-word names that are also ordinary words: they count only next to a business word ("Target shares",
# "Gap Inc", "Visa (V)"), so "Southern California" or "student visa" is not news about a stock.
AMBIGUOUS_NAMES = {"target", "progressive", "southern", "dow", "visa", "gap", "block", "match", "news", "booking",
                   "ball", "ford", "delta", "carrier", "apollo", "snap", "monster", "mosaic", "travelers", "williams",
                   "vertex", "cadence", "charter", "otis", "emerson", "diamondback", "motorola", "microchip",
                   "southwest", "carnival", "berkshire", "abbott", "gallagher", "arista", "axon", "pinnacle",
                   "summit", "frontier", "spirit", "liberty", "patriot", "pioneer", "eagle", "phoenix", "atlas",
                   "titan", "genesis", "unity", "compass", "premier", "prime", "alliance", "equity", "fidelity",
                   "guardian", "horizon", "crown", "sterling", "diamond", "apex", "core", "signal", "square", "shift",
                   "nasdaq", "general", "united", "first", "global", "american", "national", "dollar", "public",
                   "progress", "service", "republic", "constellation", "corning", "hess", "lennar", "marathon",
                   "oracle", "palantir", "dominion", "duke", "aon"}
# First words of a longer name that say nothing on their own (people, places, plain words): "Charles" is not
# Charles Schwab, "Taiwan" is not Taiwan Semiconductor.
GENERIC_FIRST_WORDS = AMBIGUOUS_NAMES | {
    "the", "bank", "international", "northern", "western", "eastern", "central", "pacific", "atlantic",
    "capital", "texas", "california", "florida", "york", "london", "china", "japan", "india", "canada", "america",
    "royal", "advanced", "applied", "consolidated", "digital", "energy", "charles", "morgan", "boston", "king",
    "taiwan", "johnson", "ross", "william", "thomas", "james", "john", "george", "martin", "lowe", "intercontinental", "illinois", "union", "simon", "parker", "warner", "electronic", "analog", "air", "live",
    "realty", "waste", "equinix", "keurig", "kinder", "philip", "phillips", "palo", "super", "take", "thermo",
    "norfolk", "northrop", "hewlett", "hilton", "lam", "becton", "bristol", "colgate", "crown"}

# How news names the large stocks the app follows (the Morning plan's and Live news's ~290), first the name a
# headline most often uses. "Name+" counts only next to a business word; "=XX" is matched exactly as written;
# "re:" is a regular expression, matched as written but not searched for; "q:" is searched for but not matched
# (a regular expression does the matching).
NEWS_NAMES: dict[str, tuple[str, ...]] = {
    "AAPL": ("Apple",), "ABBV": ("AbbVie",), "ABNB": ("Airbnb",), "ABT": ("Abbott Laboratories", "Abbott Labs", "Abbott+"),
    "ACN": ("Accenture",), "ADBE": ("Adobe",), "ADI": ("Analog Devices",), "ADM": ("Archer-Daniels-Midland", "Archer Daniels"),
    "ADP": ("Automatic Data Processing",), "ADSK": ("Autodesk",), "AEP": ("American Electric Power",),
    "AIG": ("American International Group",), "AJG": ("Arthur J. Gallagher", "Gallagher+"), "ALB": ("Albemarle",),
    "ALL": ("Allstate",), "AMAT": ("Applied Materials",), "AMD": ("Advanced Micro Devices",), "AMGN": ("Amgen",),
    "AMT": ("American Tower",), "AMZN": ("Amazon", "Amazon.com"),
    "ANET": ("Arista Networks", "Arista+"), "AON": ("Aon+",), "APD": ("Air Products",), "APH": ("Amphenol",),
    "APO": ("Apollo Global", "Apollo+"), "APP": ("AppLovin",), "AVB": ("AvalonBay",), "AVGO": ("Broadcom",),
    "AXON": ("Axon Enterprise", "Axon+"), "AXP": ("American Express", "Amex"), "AZO": ("AutoZone",),
    "BA": ("Boeing",), "BAC": ("Bank of America", "BofA"), "BBY": ("Best Buy",), "BDX": ("Becton Dickinson", "Becton, Dickinson"),
    "BK": ("BNY Mellon", "Bank of New York Mellon", "=BNY"), "BKNG": ("Booking Holdings", "Booking.com"),
    "BKR": ("Baker Hughes",), "BLK": ("BlackRock",), "BMY": ("Bristol Myers", "Bristol-Myers"),
    "BRK.B": ("Berkshire Hathaway", "Berkshire+"), "BSX": ("Boston Scientific",), "BX": ("Blackstone",),
    "C": ("Citigroup", "Citi"), "CARR": ("Carrier Global", "Carrier+"), "CAT": ("Caterpillar",), "CB": ("Chubb",),
    "CCI": ("Crown Castle",), "CCL": ("Carnival Corp", "Carnival Cruise", "Carnival+"), "CDNS": ("Cadence Design", "Cadence+"),
    "CDW": ("CDW",), "CEG": ("Constellation Energy",), "CF": ("CF Industries",), "CHTR": ("Charter Communications", "Charter+"),
    "CI": ("Cigna",), "CL": ("Colgate-Palmolive", "Colgate"), "CMCSA": ("Comcast",), "CME": ("CME Group",),
    "CMG": ("Chipotle",), "COF": ("Capital One",), "COIN": ("Coinbase",), "COP": ("ConocoPhillips",), "COR": ("Cencora",),
    "COST": ("Costco",), "CRM": ("Salesforce",), "CRWD": ("CrowdStrike",), "CSCO": ("Cisco",), "CSX": ("CSX",),
    "CTAS": ("Cintas",), "CTRA": ("Coterra",), "CVS": ("CVS",), "CVX": ("Chevron",), "D": ("Dominion Energy", "Dominion+"),
    "DAL": ("Delta Air Lines", "Delta Air", "Delta+"), "DASH": ("DoorDash",), "DD": ("DuPont",), "DDOG": ("Datadog",),
    "DE": ("Deere",), "DECK": ("Deckers",), "DELL": ("Dell",), "DG": ("Dollar General",), "DHI": ("D.R. Horton", "DR Horton"),
    "DHR": ("Danaher",), "DIS": ("Disney",), "DLR": ("Digital Realty",), "DLTR": ("Dollar Tree",), "DOW": ("Dow Inc", "Dow+"),
    "DUK": ("Duke Energy",), "DVN": ("Devon Energy",), "EA": ("Electronic Arts", "=EA+"), "EBAY": ("eBay",),
    "ECL": ("Ecolab",), "ED": ("Con Edison", "Consolidated Edison", "ConEd"), "EL": ("Estee Lauder", "Estée Lauder"),
    "ELV": ("Elevance",), "EMR": ("Emerson Electric", "Emerson+"), "EOG": ("EOG Resources",), "EQIX": ("Equinix",),
    "EQR": ("Equity Residential",), "ETN": ("Eaton",), "EXC": ("Exelon",), "F": ("Ford Motor", "Ford+"),
    "FANG": ("Diamondback Energy", "Diamondback+"), "FCX": ("Freeport-McMoRan", "Freeport McMoRan"), "FDX": ("FedEx",),
    "FTNT": ("Fortinet",), "GD": ("General Dynamics",), "GE": ("GE Aerospace", "General Electric", "=GE"),
    "GEV": ("GE Vernova",), "GILD": ("Gilead",), "GIS": ("General Mills",), "GLW": ("Corning+",), "GM": ("General Motors", "=GM+"),
    "GOOG": ("Alphabet", "Google"), "GOOGL": ("Alphabet", "Google"), "GS": ("Goldman Sachs", "Goldman"), "HAL": ("Halliburton",),
    "HCA": ("HCA Healthcare",), "HD": ("Home Depot",), "HES": ("Hess Corp", "Hess+"), "HLT": ("Hilton",), "HON": ("Honeywell",),
    "HOOD": ("Robinhood",), "HPE": ("Hewlett Packard Enterprise",), "HPQ": ("HP Inc", "q:HP", r"re:\bHP\b(?= Inc|'s|’s| shares| stock| earnings)"),
    "HSY": ("Hershey",), "HUM": ("Humana",), "HWM": ("Howmet",), "IBM": ("IBM",), "ICE": ("Intercontinental Exchange",),
    "IDXX": ("Idexx",), "INTC": ("Intel",), "INTU": ("Intuit",), "ISRG": ("Intuitive Surgical",), "IT": ("Gartner",),
    "ITW": ("Illinois Tool Works",), "JCI": ("Johnson Controls",), "JNJ": ("Johnson & Johnson", "=J&J"),
    "JPM": ("JPMorgan", "JP Morgan", "J.P. Morgan"), "KDP": ("Keurig Dr Pepper", "Keurig"), "KEYS": ("Keysight",),
    "KHC": ("Kraft Heinz",), "KKR": ("KKR",), "KLAC": ("=KLA",), "KMB": ("Kimberly-Clark",), "KMI": ("Kinder Morgan",),
    "KO": ("Coca-Cola",), "KR": ("Kroger",), "LEN": ("Lennar+",), "LHX": ("L3Harris",), "LIN": ("Linde",),
    "LLY": ("Eli Lilly", "Lilly+"), "LMT": ("Lockheed Martin", "Lockheed"), "LOW": ("Lowe's", "Lowe’s", "Lowes"),
    "LRCX": ("Lam Research",), "LULU": ("Lululemon",), "LUV": ("Southwest Airlines", "Southwest+"), "LYV": ("Live Nation",),
    "MA": ("Mastercard",), "MAR": ("Marriott",), "MCD": ("McDonald's", "McDonald’s", "McDonalds"),
    "MCHP": ("Microchip Technology", "Microchip+"), "MCK": ("McKesson",), "MCO": ("Moody's", "Moody’s"), "MDB": ("MongoDB",),
    "MDLZ": ("Mondelez",), "MDT": ("Medtronic",), "MET": ("MetLife",), "META": ("Meta Platforms", "Meta"),
    "MLM": ("Martin Marietta",), "MMC": ("Marsh McLennan", "Marsh & McLennan"), "MMM": ("=3M",),
    "MNST": ("Monster Beverage", "Monster+"), "MO": ("Altria",), "MOS": ("Mosaic+",), "MPC": ("Marathon Petroleum",),
    "MRK": ("Merck",), "MRNA": ("Moderna",), "MRVL": ("Marvell",), "MS": ("Morgan Stanley",), "MSFT": ("Microsoft",),
    "MSI": ("Motorola Solutions", "Motorola+"), "MU": ("Micron",), "NEE": ("NextEra",), "NEM": ("Newmont",),
    "NET": ("Cloudflare",), "NFLX": ("Netflix",), "NKE": ("Nike",), "NOC": ("Northrop Grumman", "Northrop"),
    "NOW": ("ServiceNow",), "NSC": ("Norfolk Southern",), "NUE": ("Nucor",), "NVDA": ("Nvidia",), "NXPI": ("NXP",),
    "O": ("Realty Income",), "OKE": ("Oneok",), "OMC": ("Omnicom",), "ON": ("onsemi", "ON Semiconductor"),
    "ORCL": ("Oracle+",), "ORLY": ("O'Reilly Automotive", "O’Reilly Automotive", "O'Reilly+"), "OTIS": ("Otis Worldwide", "Otis+"),
    "OXY": ("Occidental",), "PANW": ("Palo Alto Networks",), "PCAR": ("Paccar",), "PCG": ("PG&E",),
    "PEG": ("PSEG", "Public Service Enterprise"), "PEP": ("PepsiCo", "Pepsi"), "PFE": ("Pfizer",),
    "PG": ("Procter & Gamble", "=P&G"), "PGR": ("Progressive Corp", "Progressive+"), "PH": ("Parker Hannifin", "Parker-Hannifin"),
    "PINS": ("Pinterest",), "PLD": ("Prologis",), "PLTR": ("Palantir",), "PM": ("Philip Morris",), "PNC": ("PNC",),
    "PPG": ("PPG",), "PSA": ("Public Storage",), "PSX": ("Phillips 66",), "PYPL": ("PayPal",), "QCOM": ("Qualcomm",),
    "RCL": ("Royal Caribbean",), "RDDT": ("Reddit",), "REGN": ("Regeneron",), "ROKU": ("Roku",), "ROP": ("Roper Technologies",),
    "ROST": ("Ross Stores",), "RSG": ("Republic Services",), "RTX": ("RTX Corp", "Raytheon"), "SBUX": ("Starbucks",),
    "SCHW": ("Charles Schwab", "Schwab"), "SHOP": ("Shopify",), "SHW": ("Sherwin-Williams",), "SLB": ("SLB", "Schlumberger"),
    "SMCI": ("Super Micro", "Supermicro"), "SNAP": ("Snap Inc", "Snapchat", "Snap+"), "SNOW": ("Snowflake",),
    "SNPS": ("Synopsys",), "SO": ("Southern Company", "Southern Co"), "SOFI": ("SoFi",), "SPG": ("Simon Property",),
    "SPGI": ("S&P Global",), "SPOT": ("Spotify",), "SRE": ("Sempra",), "STZ": ("Constellation Brands",), "SYK": ("Stryker",),
    "SYY": ("Sysco",), "T": ("AT&T",), "TDG": ("TransDigm",), "TEAM": ("Atlassian",), "TEL": ("TE Connectivity",),
    "TFC": ("Truist",), "TGT": ("Target Corp", "Target+"), "TJX": ("TJX",), "TMO": ("Thermo Fisher",), "TMUS": ("T-Mobile",),
    "TRV": ("Travelers Companies", "Travelers+"), "TSLA": ("Tesla",), "TSM": ("TSMC", "Taiwan Semiconductor"), "TT": ("Trane",), "TTWO": ("Take-Two",),
    "TXN": ("Texas Instruments",), "UAL": ("United Airlines",), "UBER": ("Uber",), "ULTA": ("Ulta",), "UNH": ("UnitedHealth",),
    "UNP": ("Union Pacific",), "UPS": ("United Parcel", "=UPS"), "USB": ("U.S. Bancorp", "US Bancorp"), "V": ("Visa Inc", "Visa+"),
    "VICI": ("VICI",), "VLO": ("Valero",), "VMC": ("Vulcan Materials",), "VRTX": ("Vertex Pharmaceuticals", "Vertex+"),
    "VST": ("Vistra",), "VZ": ("Verizon",), "WBD": ("Warner Bros. Discovery", "Warner Bros Discovery", "Warner Bros"),
    "WDAY": ("Workday",), "WELL": ("Welltower",), "WFC": ("Wells Fargo",), "WM": ("Waste Management",),
    "WMB": ("Williams Companies", "Williams+"), "WMT": ("Walmart",), "XEL": ("Xcel Energy", "Xcel"),
    "XOM": ("Exxon", "ExxonMobil"), "YUM": ("Yum Brands", "Yum! Brands"), "ZS": ("Zscaler",), "ZTS": ("Zoetis",),
}

# A word that makes an ambiguous name a company: right after it ("Target shares", "Gap Inc", "Visa (V)", "Oracle
# signs"), after its possessive ("Lilly's obesity pill", "Ford's EV losses"), or just before it ("shares of Target").
_BUSINESS_STRICT = (r"Inc\b|Corp\b|Co\b|Cos\b|Group\b|Holdings\b|shares?\b|stock\b|earnings\b|profits?\b|"
                    r"revenue\b|sales\b|quarter|Q[1-4]\b|CEO\b|CFO\b|dividend\b|buyback\b|price target\b|investors\b|"
                    r"analysts?\b")
_BUSINESS_AFTER = (_BUSINESS_STRICT + r"|beats?\b|tops?\b|miss(?:es)?\b|reports?\b|posts?\b|raises?\b|cuts?\b|"
                   r"lifts?\b|lowers?\b|CEO\b|CFO\b|chief\b|to (?:buy|acquire|sell|cut|lay off|close|spin|invest|"
                   r"open|build|pay|settle|merge)\b|acquires?\b|buys?\b|sells?\b|invests?\b|agrees?\b|signs?\b|wins?\b|"
                   r"announces?\b|unveils?\b|launches?\b|recalls?\b|cancels?\b|delays?\b|halts?\b|warns?\b|files?\b|"
                   r"sues?\b|settles?\b|hires?\b|appoints?\b|names (?:new|a)\b|expands?\b|confirms?\b|completes?\b|"
                   r"shuts?\b|jumps?\b|soars?\b|surges?\b|rall(?:y|ies)\b|climbs?\b|rises?\b|gains?\b|falls?\b|drops?\b|"
                   r"slides?\b|sinks?\b|slumps?\b|tumbles?\b|plunges?\b|guidance\b|outlook\b|forecasts?\b|upgraded\b|"
                   r"downgraded\b|layoffs?\b|stores?\b")
_BUSINESS_NOUNS = (r"revenue|profits?|sales|shares|stock|earnings|CEO|chief|deal|bid|merger|acquisition|takeover|guidance|"
                   r"outlook|forecast|results|quarter|margins?|growth|dividend|buyback|layoffs?|stores?|plants?|"
                   r"factor(?:y|ies)|cloud|chips?|drugs?|pill|trial|EVs?|deliveries|orders|loss(?:es)?|board|investors|"
                   r"bonds?|debt|valuation|market value|stake|unit|division|business|cash|sensors?|devices?|"
                   r"products?|portfolio")
def _after(words: str) -> str:
    return (r"(?=,\s*(?i:Inc|Corp|Co|Ltd)\b|\s*\((?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX)?:?\s*[A-Z.]{1,6}\)|\s+(?i:"
            + words + r")|['’]s\s+(?:[\w-]+\s+){0,3}(?i:" + _BUSINESS_NOUNS + r")\b)")


_AFTER = _after(_BUSINESS_AFTER)
# In a headline in Title Case every word is capitalised, verbs included ("Visa Cuts For Students", "Mississippi
# Delta Reports Record Flooding"), so there only a business noun next to the name counts.
_AFTER_STRICT = _after(_BUSINESS_STRICT)
_BEFORE = r"(?:shares of|stock of|stake in|analysts? (?:on|at)|downgrades?|upgrades?)\s+"

# What a name means elsewhere, taken out of a headline before it is matched: people, places, the Dow the index,
# and banks and raters writing about the market rather than being written about.
_RESEARCH = (r"(?:\s+(?:strategists?|analysts?|economists?|research|Research|Securities|Global Research|Asset Management|"
             r"Wealth Management|Investment Institute|upgrades|downgrades|initiates|reiterates|sees|expects|predicts|"
             r"forecasts)\b|\s+(?:raises|cuts|lifts|lowers|boosts|trims)\s+(?:its\s+|the\s+)?[^,;:]{0,30}?"
             r"(?:target|forecast|outlook|view)s?\b)")
EXCLUDE: dict[str, str] = {
    "HLT": r"(?:Paris|Perez) Hilton", "SCHW": r"Klaus Schwab", "C": r"Citi (?:Field|Bike|Open)\b",
    "COF": r"Capital One (?:Arena|Field|Center|Hall|Cup|Orange Bowl)", "MRK": r"Merck (?:KGaA|Group)",
    "CVX": r"(?i:Chevron (?:deference|doctrine|ruling|precedent|decision))",
    "INTC": r"(?i:\bintel(?:ligence)?\s+(?:chiefs?|officials?|agenc(?:y|ies)|community|committees?|reports?|sources|"
            r"assessments?|services?|briefings?|operations?|gathering|sharing)\b)",
    "META": r"(?i:\bmeta-?(?:analys[ie]s|data|verse)\b)",
    "AMZN": r"(?i:\bAmazon(?:ian|\s+(?:rainforest|river|basin|jungle|forest|deforestation|tribes?|region|fires?)\b))",
    "GE": r"GE (?:Vernova|HealthCare|Healthcare|Appliances)", "GM": r"\b(?:assistant|interim|new|former) GM\b",
    "DOW": r"(?i:\bDow\s+(?:Jones|futures|stock futures|industrials|average|index|transports|utilities|points?|[+-]?\d)|"
           r"\bthe Dow\b)",
    "MMM": r"(?i:\b3M\s+(?:users|views|downloads|subscribers|people|fans|followers|members|customers|homes|"
           r"vehicles|barrels|jobs|visitors|streams|copies)\b)",
    "MCO": r"Moody['’]s(?:\s+Analytics)?(?:\s+(?:downgrades|upgrades|cuts|affirms|rates|warns|says|economists?)\b|['’]\s+\w+)",
    "SPGI": r"S&P Global(?:\s+(?:Ratings|Market Intelligence|Mobility|Commodity Insights))?\s+(?:(?:\w+\s+){0,2}PMI|"
            r"downgrades|upgrades|cuts|affirms|says|warns|survey)",
}
for _sym in ("GS", "JPM", "MS", "BAC", "C", "WFC"):
    _forms = "|".join(re.escape(f.lstrip("=").rstrip("+")) for f in NEWS_NAMES[_sym] if not f.startswith(("re:", "q:")))
    EXCLUDE[_sym] = "|".join(x for x in (EXCLUDE.get(_sym), r"(?:" + _forms + r")" + _RESEARCH) if x)

# Headlines that only describe the price ("Why Oracle stock is soaring today", "Tesla shares fall 8%"): they say
# the stock moved, not why, so they are not news behind a move.
_MOVE = (r"(?:up|down|higher|lower|soar\w*|jump\w*|surg\w*|rall\w*|climb\w*|ris(?:e|es|ing)|rose|gain\w*|spik\w*|"
         r"pop\w*|fall\w*|fell|drop\w*|slid\w*|slip\w*|sink\w*|sank|slump\w*|tumbl\w*|plung\w*|crash\w*|tank\w*)")
_WHY = re.compile(r"^\s*(?:why|here['’]?s why|what['’]?s going on with|what happened (?:to|with)|is it time to)\b", re.I)
_PRICE_MOVE = re.compile(r"\b(?:shares?|stock)\b[^.:;]{0,30}?\b" + _MOVE + r"\b", re.I)
_CATALYST = re.compile(r"\b(?:after|as|following|amid|despite|due to|because of|thanks to|over|post-|ahead of|on "
                       r"(?!(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|today\b|the day\b|heavy\b|high\b|volume\b))"
                       r"|[:—–]", re.I)


def move_only(title: str) -> bool:
    """Whether a headline only says the stock moved."""
    if _WHY.search(title):
        return True
    return bool(_PRICE_MOVE.search(title)) and not _CATALYST.search(title)


def clean_name(raw: str | None) -> str | None:
    """A company's name as news writes it: "NVIDIA Corp" -> "NVIDIA", "Coca-Cola Co (The)" -> "Coca-Cola",
    "WELLS FARGO & COMPANY/MN" -> "WELLS FARGO". None when nothing usable is left."""
    if not raw or not isinstance(raw, str) or "(demo)" in raw.lower():
        return None
    name = re.sub(r"\s*\((the)\)\s*$", "", raw.strip(), flags=re.I)
    for _ in range(5):
        trimmed = NAME_SUFFIXES.sub("", name).strip(" ,.-")
        trimmed = re.sub(r"\s+(&|and)$", "", trimmed, flags=re.I).strip(" ,.-")
        if trimmed == name:
            break
        name = trimmed
    name = re.sub(r"^the\s+", "", name, flags=re.I).strip()
    return name if len(name) >= 2 else None


def _plain(form: str) -> str:
    """A name form as searched for: without its marks."""
    return form[2:] if form.startswith("q:") else form[1:] if form.startswith("=") else form.rstrip("+")


def _word(text: str) -> str:
    """A name as a pattern. Each word starts with its capital ("Best Buy" the company, not "the best buy";
    "Booking", not "booking" a flight), the rest of it in any case, since news writes "Nvidia" for "NVIDIA".
    A word that starts in lower case ("eBay", "onsemi", "of") is matched in any case."""
    def one(w: str) -> str:
        if w[:1].isupper():
            return re.escape(w[0]) + ("(?i:" + re.escape(w[1:]) + ")" if len(w) > 1 else "")
        return "(?i:" + re.escape(w) + ")"
    body = r"\s+".join(one(w) for w in text.split())
    return r"(?<![\w&$€£])" + body + r"(?![\w&])"


def name_forms(symbol: str, name: str | None) -> tuple[str, ...]:
    """The forms of a stock's name to look for: NEWS_NAMES for the stocks the app follows, else the cleaned name
    (needing a business word next to it when it is an ordinary word) and, for a longer name, its first word
    next to a business word."""
    sym = symbol.upper()
    if sym in NEWS_NAMES:
        return NEWS_NAMES[sym]
    if not name:
        return ()
    forms = [name + "+" if " " not in name and name.lower() in AMBIGUOUS_NAMES else name]
    words = [w.strip(",.") for w in name.split()]
    first = words[0]
    if len(words) > 1 and len(first) >= 4 and first.lower() not in GENERIC_FIRST_WORDS and first[:1].isupper():
        forms.append(first + "+")
    if len(words) >= 3 and len(words[1]) >= 4 and words[1].lower() not in {"and", "the", "with", "from"}:
        forms.append(f"{words[0]} {words[1]}")        # "Taiwan Semiconductor" for Taiwan Semiconductor Manufacturing
    return tuple(forms)


def title_case(text: str) -> bool:
    """Whether a headline capitalises (nearly) every word."""
    words = [w for w in re.findall(r"[A-Za-z][\w'’-]*", text or "") if len(w) >= 4]
    return len(words) >= 3 and sum(w[0].isupper() for w in words) >= 0.8 * len(words)


def name_patterns(symbol: str, name: str | None, strict: bool = False) -> list[re.Pattern]:
    """What a headline must contain to be about this stock (see name_forms), or its ticker: as written in
    capitals for a ticker of three letters or more that is not a word, and only as "$XX" or "(NYSE: XX)" for
    any other. `strict` (for a headline in Title Case): a name that needs a business word needs a noun."""
    after = _AFTER_STRICT if strict else _AFTER
    pats: list[re.Pattern] = []
    for form in name_forms(symbol, name):
        if form.startswith("q:"):
            continue
        if form.startswith("re:"):
            pats.append(re.compile(form[3:]))
        elif form.startswith("=") and form.endswith("+"):
            pats.append(re.compile(r"(?<![\w&$])" + re.escape(form[1:-1]) + r"(?![\w&])" + after))
        elif form.startswith("="):
            pats.append(re.compile(r"(?<![\w&$€£])" + re.escape(form[1:]) + r"(?![\w&])"))
        elif form.endswith("+"):
            w = _word(form[:-1])
            pats.append(re.compile(w + after))
            pats.append(re.compile(r"(?i:" + _BEFORE + r")" + w))
        else:
            pats.append(re.compile(_word(form)))
    sym = symbol.upper()
    tick = r"[.\-/]?".join(re.escape(p) for p in re.split(r"[.\-/]", sym))           # BRK.B, BRK-B, BRK/B
    letters = re.sub(r"[^A-Z]", "", sym)
    pats.append(re.compile(r"(?:\$" + tick + r"|\((?:NYSE|NASDAQ|Nasdaq|NYSEARCA|AMEX|Cboe):\s*" + tick + r"\))(?![A-Za-z0-9])"))
    if len(letters) >= 3 and sym not in COMMON_WORDS:
        pats.append(re.compile(r"(?<![A-Za-z0-9$])" + tick + r"(?![A-Za-z0-9])"))
    return pats


def mentions(text: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(text or "") for p in patterns)


def _cleared(symbol: str, title: str) -> str:
    """The headline with this stock's other meanings (EXCLUDE) taken out."""
    ex = EXCLUDE.get(symbol.upper())
    return re.sub(ex, " ", title) if ex else title


def loose_patterns(symbol: str, name: str | None) -> list[re.Pattern]:
    """Anything that might be the stock: a name form in any case, or a ticker that counts on its own in any case.
    A headline that has one of these but not a match is one the search could not tie to the stock."""
    pats = [re.compile(r"(?<![\w&])" + re.escape(_plain(f)) + r"(?![\w&])", re.I)
            for f in name_forms(symbol, name) if not f.startswith("re:")]
    sym = symbol.upper()
    if len(re.sub(r"[^A-Z]", "", sym)) >= 3 and sym not in COMMON_WORDS:
        pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(sym) + r"(?![A-Za-z0-9])", re.I))
    return pats


def about(symbol: str, name: str | None, title: str) -> str | None:
    """How a headline reads for a stock: "news" (it names the stock and is not only about the price), "move"
    (it names the stock but only says it moved), "maybe" (it might be about the stock but cannot be tied to
    it), or None."""
    text = _cleared(symbol, title or "")
    if mentions(text, name_patterns(symbol, name, strict=title_case(text))):
        return "move" if move_only(text) else "news"
    return "maybe" if mentions(text, loose_patterns(symbol, name)) else None


def _safe_xml(text: str) -> ET.Element:
    """Parse a feed, refusing anything with a document type or entity declaration anywhere in it. Raises
    ValueError when the feed is refused or does not read."""
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        raise ValueError("the feed carries a document type or entity declaration")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"the feed does not read ({exc})") from None


def parse_google_news(text: str) -> list[dict[str, Any]]:
    """Items of a Google News RSS search feed: headline (without the trailing " - Publisher"), link, publisher,
    publication time. Items that do not read are skipped; a feed that does not read raises ValueError."""
    root = _safe_xml(text)
    if root.tag != "rss" and root.find("channel") is None:
        raise ValueError("the reply is not an RSS feed")
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = it.find("source")
        publisher = (src.text or "").strip() if src is not None and src.text else ""
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        try:
            published = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "")
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError):
            published = None
        if not title or not re.match(r"https?://", link, re.I):
            continue
        # The description repeats the headline and adds the publisher; it is kept for reading, never matched on.
        desc = html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))
        out.append({"title": title, "url": link, "publisher": publisher, "published": published,
                    "summary": " ".join(desc.split())[:600], "id": (it.findtext("guid") or link)[:200]})
    return out


def parse_brave(body: Any) -> list[dict[str, Any]]:
    """Results of Brave's news search: title, url, description, page_age (ISO, read as UTC when it has no zone)."""
    rows = body.get("results") if isinstance(body, dict) else None
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("title"), str) or not isinstance(r.get("url"), str):
            continue
        if not re.match(r"https?://", r["url"], re.I):
            continue
        published = None
        if isinstance(r.get("page_age"), str):
            try:
                published = datetime.fromisoformat(r["page_age"].replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        meta = r.get("meta_url") if isinstance(r.get("meta_url"), dict) else {}
        profile = r.get("profile") if isinstance(r.get("profile"), dict) else {}
        publisher = profile.get("name") or meta.get("hostname") or ""
        desc = html.unescape(re.sub(r"<[^>]+>", " ", r.get("description") or ""))
        out.append({"title": html.unescape(re.sub(r"<[^>]+>", "", r["title"])).strip(), "url": r["url"],
                    "publisher": str(publisher)[:80], "published": published,
                    "summary": " ".join(desc.split())[:600], "id": r["url"][:200]})
    return out


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


class Refused(RuntimeError):
    """A search not made because a limit said no (Brave's caps, Google's pause): not a failure of the search."""


class SearchNews:
    """Searches news for one stock at a time. Shared by the Live news feed, the news panels and the Morning plan
    (one per app: `for_service`); safe to call from several threads, and from several copies of the app on one
    database (the Brave count, Google's spacing and its pause are kept in the database)."""

    def __init__(self, settings: Any, db: Any, *, sec: Any = None, quote: Callable[[str], dict] | None = None,
                 demo: bool = False, session: requests.Session | None = None):
        self.settings = settings
        self.db = db
        self.sec = sec
        self.quote = quote
        self.demo = demo
        self.session = session or requests.Session()
        self._lock = threading.Lock()
        self._google_next = 0.0
        self._google_blocked_until = 0.0
        self._brave_next = 0.0
        self.last_error: dict[str, Any] | None = None
        self.last_ok: dict[str, str] = {}
        self.brave_header: str | None = None

    # --- what is switched on --------------------------------------------------------------------------------
    @property
    def google_on(self) -> bool:
        return bool(getattr(self.settings, "google_news", True)) and not self.demo

    @property
    def brave_on(self) -> bool:
        return bool(getattr(self.settings, "brave_api_key", "")) and not self.demo

    @property
    def available(self) -> bool:
        return self.demo or self.google_on or self.brave_on

    def sources(self) -> list[str]:
        if self.demo:
            return ["Demo search"]
        return [n for n, on in (("Google News", self.google_on), ("Brave", self.brave_on)) if on]

    # --- company names --------------------------------------------------------------------------------------
    def company_name(self, symbol: str, hint: str | None = None) -> str | None:
        """The company's name: the SEC's ticker list, else `hint` or the quote's description. Kept 30 days, and
        never on demo data (whose descriptions are made up)."""
        symbol = symbol.upper()
        if symbol in NEWS_NAMES:
            return _plain(next((f for f in NEWS_NAMES[symbol] if not f.startswith("re:")), NEWS_NAMES[symbol][0]))
        key = f"search:name:{symbol}"
        try:
            hit = self.db.cache_get(key, NAME_DAYS * 86400)
        except Exception:                             # noqa: BLE001 - no cache: look it up
            hit = None
        if hit and hit.get("name"):
            return hit["name"]

        def usable(raw: Any) -> str | None:
            name = clean_name(raw) if isinstance(raw, str) else None
            return name if name and name.upper().replace(" ", "") != symbol.replace(".", "") else None
        name = None
        if self.sec is not None and getattr(self.sec, "enabled", False):
            try:
                name = usable(self.sec.cik_for(symbol)[1])
            except Exception:                         # noqa: BLE001 - funds and foreign names are not listed
                name = None
        name = name or usable(hint)
        if not name and self.quote is not None:
            try:
                name = usable((self.quote(symbol) or {}).get("description"))
            except Exception:                         # noqa: BLE001 - no quote: search by ticker only
                name = None
        if name and not self.demo:
            try:
                self.db.cache_put(key, {"name": name})
            except Exception:                         # noqa: BLE001
                pass
        return name

    # --- the shared counters ---------------------------------------------------------------------------------
    def _locked(self, work: Callable[[sqlite3.Cursor], Any]) -> Any:
        """Run `work` in one write transaction (BEGIN IMMEDIATE), so two copies of the app cannot both read a
        count and both add to it."""
        with self.db.tx() as cur:
            if not cur.connection.in_transaction:
                cur.execute("BEGIN IMMEDIATE")
            return work(cur)

    @staticmethod
    def _put(cur: sqlite3.Cursor, key: str, value: Any) -> None:
        cur.execute("INSERT INTO kv_cache (key, value, fetched_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, fetched_at = excluded.fetched_at", (key, json.dumps(value), time.time()))

    @staticmethod
    def _get(cur: sqlite3.Cursor, key: str) -> Any:
        row = cur.execute("SELECT value FROM kv_cache WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _brave_counts(cur: sqlite3.Cursor, now: datetime) -> tuple[int, int]:
        """(searches in the last 31 days, searches today), UTC days."""
        first = _day(now - timedelta(days=BRAVE_WINDOW_DAYS - 1))
        total = today = 0
        for key, value in cur.execute("SELECT key, value FROM kv_cache WHERE key >= ? AND key <= ?",
                                      (f"brave:day:{first}", f"brave:day:{_day(now)}")).fetchall():
            n = int((json.loads(value) or {}).get("n", 0))
            total += n
            if key == f"brave:day:{_day(now)}":
                today = n
        return total, today

    def brave_usage(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        with self.db.tx() as cur:
            window, today = self._brave_counts(cur, now)
        return {"month": window, "month_limit": self.settings.brave_monthly_limit, "window_days": BRAVE_WINDOW_DAYS,
                "day": today, "day_limit": self.settings.brave_daily_limit, "header": self.brave_header}

    def _brave_claim(self, now: datetime, reserve: int = 0) -> str | None:
        """Count one Brave request before it is sent; None when it may go, else why not. `reserve` keeps that many
        of the day's searches for other callers (the Morning plan's check)."""
        limit_m, limit_d = int(self.settings.brave_monthly_limit), int(self.settings.brave_daily_limit)

        def work(cur: sqlite3.Cursor) -> str | None:
            window, today = self._brave_counts(cur, now)
            if window >= limit_m:
                return (f"Brave's limit of {limit_m} searches in {BRAVE_WINDOW_DAYS} days (BRAVE_MONTHLY_LIMIT) is "
                        "used up; it keeps the $5 monthly credit from being exceeded")
            if today >= limit_d - max(0, reserve):
                return (f"Brave's daily limit ({limit_d}, BRAVE_DAILY_LIMIT) is used up" if not reserve or today >= limit_d
                        else f"the rest of Brave's daily limit is kept for the Morning plan ({today} of {limit_d} used)")
            self._put(cur, f"brave:day:{_day(now)}", {"n": today + 1})
            return None
        # The database transaction alone orders the claims (BEGIN IMMEDIATE under the database's own lock); a
        # lock of this object's around it could be taken in the other order by a caller already in a transaction.
        try:
            return self._locked(work)
        except sqlite3.Error as exc:
            return f"Brave's count could not be kept ({exc.__class__.__name__}), so Brave was not asked"

    def _google_pause(self) -> float:
        """Until when Google News is paused (its refusal is shared through the database)."""
        until = self._google_blocked_until
        try:
            until = max(until, float((self.db.cache_get("search:google_paused") or {}).get("until", 0)))
        except Exception:                             # noqa: BLE001
            pass
        return until

    def _google_refused(self) -> None:
        until = time.time() + GOOGLE_BACKOFF_SECONDS
        self._google_blocked_until = until
        try:
            self.db.cache_put("search:google_paused", {"until": until})
        except Exception:                             # noqa: BLE001
            pass

    def _google_turn(self) -> None:
        """Wait for this request's turn: GOOGLE_GAP_SECONDS after the last one from any copy of the app. A turn
        further off than GOOGLE_MAX_WAIT_SECONDS is not taken (Refused)."""
        def work(cur: sqlite3.Cursor) -> float:
            now = time.time()
            nxt = max(float((self._get(cur, "search:google_next") or {}).get("at", 0)), self._google_next)
            if nxt - now > GOOGLE_MAX_WAIT_SECONDS:
                raise Refused("Google News searches are queued too deep right now; this one was skipped")
            slot = max(now, nxt)
            self._put(cur, "search:google_next", {"at": slot + GOOGLE_GAP_SECONDS})
            return slot
        try:
            slot = self._locked(work)
        except sqlite3.Error:
            slot = max(time.time(), self._google_next)
        self._google_next = slot + GOOGLE_GAP_SECONDS
        if slot > time.time():
            time.sleep(slot - time.time())

    # --- the searches ---------------------------------------------------------------------------------------
    def _fail(self, source: str, error: str, quiet: bool = False) -> str:
        msg = redact(error)
        key = getattr(self.settings, "brave_api_key", "")
        if key:
            msg = msg.replace(key, "[redacted]")
        if not quiet:
            self.last_error = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": source,
                               "error": msg[:300]}
            emit("news_failed", logging.WARNING, what=f"search {source}", error=msg[:300])
        return msg

    @property
    def google_paused(self) -> bool:
        return time.time() < self._google_pause()

    def google(self, query: str, hours: int) -> list[dict[str, Any]]:
        def paused() -> None:
            left = self._google_pause() - time.time()
            if left > 0:
                raise Refused(f"Google News refused requests; paused for {int(left) // 60 + 1} more minutes")
        paused()
        # Google's window is "when:<n>h" up to a day and "when:<n>d" beyond it.
        window = f"when:{max(1, int(hours))}h" if hours <= 24 else f"when:{max(1, -(-int(hours) // 24))}d"
        params = {"q": f"{query} {window}", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        self._google_turn()
        paused()                                      # another request may have been refused while this one waited
        try:
            r = self.session.get(f"{GOOGLE_NEWS_RSS}?{urlencode(params)}", headers={"User-Agent": USER_AGENT},
                                 timeout=(5, 15))
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Google News ({exc.__class__.__name__})") from None
        if r.status_code in (403, 429, 503) or (r.status_code == 200 and "<rss" not in r.text[:2000].lower()):
            self._google_refused()
            raise RuntimeError(f"Google News refused the request ({r.status_code}); pausing it for 15 minutes")
        if r.status_code != 200:
            raise RuntimeError(f"Google News returned {r.status_code}")
        try:
            rows = parse_google_news(r.text)
        except ValueError as exc:
            raise RuntimeError(f"Google News sent a feed this app will not read: {exc}") from None
        self.last_ok["Google News"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return rows

    def brave(self, query: str, hours: int, reserve: int = 0) -> list[dict[str, Any]]:
        why = self._brave_claim(datetime.now(timezone.utc), reserve)
        if why:
            raise Refused(why)
        with self._lock:
            wait = self._brave_next - time.time()
            self._brave_next = max(time.time(), self._brave_next) + BRAVE_GAP_SECONDS
        if wait > 0:
            time.sleep(wait)
        params = {"q": query, "count": MAX_RESULTS, "freshness": "pd" if hours <= 24 else "pw", "country": "US",
                  "search_lang": "en", "spellcheck": 0, "safesearch": "off"}
        try:
            # No redirects: the key travels in a header, which requests would carry to another host.
            r = self.session.get(BRAVE_NEWS, params=params, timeout=(5, 15), allow_redirects=False,
                                 headers={"X-Subscription-Token": self.settings.brave_api_key,
                                          "Accept": "application/json"})
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not reach Brave ({exc.__class__.__name__})") from None
        remaining = r.headers.get("X-RateLimit-Remaining") if hasattr(r, "headers") else None
        if remaining:
            self.brave_header = str(remaining)[:60]
        if r.status_code in (401, 403):
            raise RuntimeError("Brave refused the key: check BRAVE_API_KEY in .env (and that the plan includes news)")
        if r.status_code == 429:
            raise RuntimeError("Brave's rate limit or monthly credit was reached")
        if r.status_code != 200:
            raise RuntimeError(f"Brave returned {r.status_code}")
        try:
            body = r.json()
        except ValueError:
            raise RuntimeError("Brave sent a reply this app could not read") from None
        self.last_ok["Brave"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return parse_brave(body)

    def search(self, symbol: str, *, hours: int = 24, name_hint: str | None = None, brave: str = "fallback",
               have: int = 0, brave_reserve: int = 0, brave_if_unmatched: bool = True) -> dict[str, Any]:
        """Recent news about one stock. `brave`: "never", "fallback" (only when nothing was found: not by Google
        News, and not the `have` items the caller already found elsewhere) or "always"; `brave_reserve` leaves
        that many of Brave's daily searches to other callers.

        `brave_if_unmatched=False` skips Brave when Google returned headlines it could not tie to the stock.

        Returns {"items", "checked", "errors", "skipped", "unmatched", "move_only", "name"}: items in the shared
        headline shape; the sources that answered; failures; searches a limit did not allow (not failures); how
        many results inside the window might be about the stock but could not be tied to it, or only say that it
        moved (`move_only` of them); those are not news, and not "no news" either."""
        symbol = symbol.upper()
        if self.demo:
            return self._demo(symbol, hours)
        name = self.company_name(symbol, name_hint)
        forms = [_plain(f) for f in name_forms(symbol, name) if not f.startswith("re:")]
        if not forms and (len(re.sub(r"[^A-Z]", "", symbol)) < 3 or symbol in COMMON_WORDS):
            return {"items": [], "checked": [], "skipped": [], "unmatched": 0, "move_only": 0, "name": name,
                    "errors": [f"No company name is known for {symbol}, and its ticker can be mistaken for a word, so "
                               "news about it cannot be told apart from other news. Set SEC_USER_AGENT for names."]}
        terms = [f'"{f}"' for f in list(dict.fromkeys(forms))[:3]]
        if terms and len(re.sub(r"[^A-Z]", "", symbol)) >= 3 and symbol not in COMMON_WORDS:
            terms.append(f'"{symbol}"')
        query = " OR ".join(terms) if terms else f'"{symbol}" stock'
        checked, errors, skipped, found = [], [], [], []
        unmatched = moves = 0
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        def keep(rows: list[dict[str, Any]], via: str) -> None:
            nonlocal unmatched, moves
            for row in rows:
                if row["published"] is not None and row["published"] < cutoff:
                    continue
                verdict = about(symbol, name, row["title"])
                if verdict == "news":
                    found.append({**row, "via": via})
                elif verdict:
                    unmatched += 1
                    moves += verdict == "move"
        if self.google_on:
            try:
                keep(self.google(query, hours), "Google News")
                checked.append("Google News")
            except Refused as exc:
                skipped.append(self._fail("Google News", str(exc), quiet=True))
            except RuntimeError as exc:
                errors.append(self._fail("Google News", str(exc)))
        if self.brave_on and (brave == "always" or (brave == "fallback" and not found and not have
                                                    and (brave_if_unmatched or not unmatched))):
            try:
                keep(self.brave(f"{forms[0] if forms else symbol} stock", hours, brave_reserve), "Brave")
                checked.append("Brave")
            except Refused as exc:
                skipped.append(self._fail("Brave", str(exc), quiet=True))
            except RuntimeError as exc:
                errors.append(self._fail("Brave", str(exc)))
        seen, items = set(), []
        for row in sorted(found, key=lambda r: r["published"] or datetime.min.replace(tzinfo=timezone.utc),
                          reverse=True):
            k = re.sub(r"\W+", " ", row["title"].lower()).strip()[:90]
            if k in seen:
                continue
            seen.add(k)
            items.append(self._as_item(symbol, row))
        if not checked and skipped and not errors:
            errors.append(skipped[0])                 # nothing at all was asked: the caller must not read "no news"
        return {"items": items[:MAX_RESULTS], "checked": checked, "errors": errors, "skipped": skipped,
                "unmatched": unmatched, "move_only": moves, "name": name}

    @staticmethod
    def _as_item(symbol: str, row: dict[str, Any]) -> dict[str, Any]:
        ident = hashlib.sha1(f"{row['via']}|{row['id']}".encode()).hexdigest()[:16]
        return {"id": ident, "headline": row["title"][:300], "summary": row["summary"], "url": row["url"],
                "source": row["publisher"] or row["via"], "via": row["via"],
                "published": row["published"].isoformat(timespec="seconds") if row["published"] else None,
                "kind": "news", "related": symbol, "tone": word_tone(row["title"]), "tone_method": "words"}

    def _demo(self, symbol: str, hours: int) -> dict[str, Any]:
        """Made-up results on demo data, the same for a stock and day: two stocks in three have news."""
        now = datetime.now(timezone.utc)
        seed = int(hashlib.sha256(f"{symbol}|{now.date()}".encode()).hexdigest()[:6], 16)
        base = {"checked": ["Demo search"], "errors": [], "skipped": [], "unmatched": 0, "move_only": 0, "name": None}
        if seed % 3 == 0:
            return {**base, "items": []}
        row = {"title": f"{symbol} shares move after an analyst note (made up)",
               "url": f"https://example.com/demo/{symbol.lower()}", "publisher": "Demo",
               "published": now - timedelta(minutes=20 + seed % 90), "summary": "A demo search result.",
               "id": f"{symbol}-{now.date()}", "via": "Demo search"}
        return {**base, "items": [self._as_item(symbol, row)]}

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"sources": self.sources(), "google_news": self.google_on, "brave": self.brave_on,
                               "last_ok": dict(self.last_ok), "last_error": self.last_error}
        if self.brave_on:
            try:
                out["brave_usage"] = self.brave_usage()
            except Exception:                         # noqa: BLE001
                out["brave_usage"] = None
        until = self._google_pause()
        if time.time() < until:
            out["google_paused_until"] = datetime.fromtimestamp(until, timezone.utc).isoformat(timespec="seconds")
        return out


def for_service(svc: Any) -> SearchNews:
    """The app's one SearchNews, made on first use."""
    found = getattr(svc, "search_news", None)
    if found is None:
        def quote(symbol: str) -> dict:
            return svc.market.quote(symbol)
        found = SearchNews(svc.settings, svc.db, sec=getattr(svc, "sec", None), quote=quote,
                           demo=svc.market.info.mode == "demo")
        svc.search_news = found
    return found


__all__ = ["SearchNews", "Refused", "for_service", "parse_google_news", "parse_brave", "clean_name", "name_forms",
           "name_patterns", "mentions", "about", "move_only", "NEWS_NAMES"]
