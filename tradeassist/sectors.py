"""Which Select Sector SPDR fund to compare a stock with, for relative strength.

A fixed list covers the large companies people trade most; anything else falls back to its
SEC industry code when SEC lookups are switched on, and otherwise has no sector comparison.
The sectors follow the current GICS layout (Alphabet and Meta in communication services,
Visa and Mastercard in financials).
"""

from __future__ import annotations

SECTOR_FUNDS = {
    "XLK": "Technology", "XLC": "Communication services", "XLY": "Consumer discretionary",
    "XLP": "Consumer staples", "XLE": "Energy", "XLF": "Financials", "XLV": "Health care",
    "XLI": "Industrials", "XLB": "Materials", "XLRE": "Real estate", "XLU": "Utilities",
}

_BY_FUND = {
    "XLK": "AAPL MSFT NVDA AVGO ORCL CRM ADBE AMD CSCO ACN IBM INTU QCOM TXN NOW AMAT MU LRCX ADI KLAC PANW "
           "SNPS CDNS ANET CRWD FTNT MRVL INTC PLTR ADSK ROP WDAY APH MSI NXPI ON MCHP HPQ HPE DELL SMCI KEYS "
           "TEL GLW IT CDW ZS SNOW DDOG NET MDB TEAM SHOP APP",
    "XLC": "GOOGL GOOG META NFLX DIS TMUS VZ T CMCSA CHTR EA TTWO WBD OMC LYV PINS SNAP RDDT SPOT ROKU",
    "XLY": "AMZN TSLA HD MCD LOW BKNG TJX NKE SBUX CMG ORLY AZO MAR HLT ROST GM F LULU YUM DHI LEN ABNB "
           "EBAY DECK RCL CCL UBER DASH ULTA BBY",
    "XLP": "WMT COST PG KO PEP PM MO MDLZ CL TGT KMB GIS KHC STZ MNST KDP SYY HSY KR ADM EL DG DLTR",
    "XLE": "XOM CVX COP EOG SLB OXY MPC PSX VLO WMB KMI OKE HAL DVN FANG BKR HES CTRA",
    "XLF": "BRK.B JPM V MA BAC WFC GS MS C AXP SCHW BLK SPGI CB PGR MMC ICE CME USB PNC AON COF TFC MET "
           "AIG PYPL HOOD COIN SOFI KKR BX APO MCO BK TRV ALL AJG",
    "XLV": "LLY UNH JNJ ABBV MRK TMO ABT DHR PFE AMGN ISRG BMY GILD VRTX SYK MDT CVS ELV CI REGN ZTS BSX "
           "HCA MCK BDX HUM MRNA IDXX COR",
    "XLI": "GE CAT RTX HON UNP BA LMT DE UPS ETN ADP GD NOC WM CSX ITW EMR FDX NSC PH CTAS TT JCI PCAR "
           "CARR OTIS DAL UAL LUV AXON MMM RSG LHX TDG GEV HWM",
    "XLB": "LIN SHW APD ECL FCX NEM DOW DD NUE PPG VMC MLM ALB CF MOS",
    "XLRE": "PLD AMT EQIX CCI PSA O SPG WELL DLR VICI AVB EQR",
    "XLU": "NEE SO DUK CEG SRE AEP D EXC XEL PCG ED PEG VST",
}
TICKER_FUND = {t: fund for fund, names in _BY_FUND.items() for t in names.split()}

# SEC standard industrial classification code ranges, mapped to the nearest sector fund.
_SIC_RANGES = [
    (100, 999, "XLP"), (1000, 1499, "XLB"), (1300, 1399, "XLE"), (1500, 1799, "XLI"),
    (2000, 2199, "XLP"), (2200, 2399, "XLY"), (2400, 2799, "XLB"), (2800, 2829, "XLB"),
    (2830, 2839, "XLV"), (2840, 2899, "XLP"), (2900, 2999, "XLE"), (3000, 3569, "XLI"),
    (3570, 3579, "XLK"), (3580, 3659, "XLI"), (3660, 3679, "XLK"), (3680, 3699, "XLI"),
    (3700, 3799, "XLY"), (3800, 3839, "XLI"), (3840, 3859, "XLV"), (3860, 3999, "XLI"),
    (4000, 4799, "XLI"), (4800, 4899, "XLC"), (4900, 4999, "XLU"), (5000, 5199, "XLI"),
    (5200, 5999, "XLY"), (6000, 6499, "XLF"), (6500, 6799, "XLRE"), (7000, 7369, "XLY"),
    (7370, 7379, "XLK"), (7380, 7999, "XLY"), (8000, 8099, "XLV"), (8100, 8999, "XLI"),
]


def sector_fund(symbol: str, sic: int | str | None = None) -> str | None:
    """The sector fund for `symbol`, or None when its sector is not known."""
    symbol = symbol.strip().upper().replace("/", ".")
    if symbol in SECTOR_FUNDS:
        return None                      # a sector fund is not compared with itself
    if symbol in TICKER_FUND:
        return TICKER_FUND[symbol]
    try:
        code = int(sic) if sic not in (None, "") else None
    except (TypeError, ValueError):
        code = None
    if code is None:
        return None
    # The narrow ranges are listed after the wide ones they sit inside, so the last match wins.
    found = None
    for lo, hi, fund in _SIC_RANGES:
        if lo <= code <= hi:
            found = fund
    return found
