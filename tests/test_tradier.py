import pytest
import requests

from tradeassist.providers.base import DataError, normalize_contract
from tradeassist.providers.tradier import LIVE_URL, SANDBOX_URL, TradierData


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Stands in for requests.Session. It has no post/put/delete, so any write would raise."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(replies, sandbox=False):
    session = FakeSession(replies)
    return TradierData("token-123", sandbox=sandbox, session=session), session


# The response bodies below follow the examples in Tradier's API reference.
CHAIN_BODY = {"options": {"option": [
    {"symbol": "AAPL210416C00125000", "description": "AAPL Apr 16 2021 $125.00 Call", "type": "option",
     "last": 3.45, "volume": 1105, "bid": 3.4, "ask": 3.45, "underlying": "AAPL", "strike": 125,
     "bidsize": 36, "asksize": 75, "bid_date": 1612196200000, "ask_date": 1612196200000,
     "open_interest": 8249, "contract_size": 100, "expiration_date": "2021-04-16", "option_type": "call",
     "root_symbol": "AAPL",
     "greeks": {"delta": 0.52, "gamma": 0.03, "theta": -0.04, "vega": 0.21, "rho": 0.1, "phi": -0.1,
                "bid_iv": 0.30, "mid_iv": 0.31, "ask_iv": 0.32, "smv_vol": 0.305,
                "updated_at": "2021-02-02 12:04:22"}},
    {"symbol": "AAPL1210416C00125000", "bid": 1.0, "ask": 1.2, "strike": 125, "contract_size": 150,
     "expiration_date": "2021-04-16", "option_type": "call", "root_symbol": "AAPL1", "greeks": None},
    {"symbol": "AAPL210416P00125000", "bid": "NaN", "ask": None, "strike": 125, "contract_size": 100,
     "expiration_date": "2021-04-16", "option_type": "put", "root_symbol": "AAPL"},
]}}


def test_hosts_and_auth_header():
    live, session = client([])
    assert live.base_url == LIVE_URL and live.info.realtime and live.info.has_greeks
    assert session.headers["Authorization"] == "Bearer token-123"
    sandbox, _ = client([], sandbox=True)
    assert sandbox.base_url == SANDBOX_URL and not sandbox.info.realtime and not sandbox.info.has_greeks
    with pytest.raises(ValueError):
        TradierData("", sandbox=True)


def test_chain_is_normalised_and_nonstandard_contracts_dropped():
    c, session = client([FakeResponse(body=CHAIN_BODY)])
    chain = c.chain("AAPL", "2021-04-16")
    assert session.calls[0] == (f"{LIVE_URL}/markets/options/chains",
                                {"symbol": "AAPL", "expiration": "2021-04-16", "greeks": "true"})
    assert [x["symbol"] for x in chain] == ["AAPL210416C00125000", "AAPL210416P00125000"]   # 150-share one removed
    call, put = chain
    assert call["bid"] == 3.4 and call["ask"] == 3.45 and call["open_interest"] == 8249
    assert call["greeks"]["smv_vol"] == 0.305 and call["greeks"]["delta"] == 0.52
    assert call["bid_time"].startswith("2021-02-01T16:16:40")
    assert put["bid"] is None and put["ask"] is None and put["greeks"] is None


def test_single_object_list_and_null_shapes():
    one = {"quotes": {"quote": {"symbol": "AAPL", "type": "stock", "last": 273.47, "bid": 273.53, "ask": 273.59}}}
    two = {"quotes": {"quote": [{"symbol": "AAPL", "last": 1.0}, {"symbol": "BRK/B", "last": 2.0}]}}
    none = {"quotes": {"unmatched_symbols": {"symbol": "NOPE"}}}
    c, session = client([FakeResponse(body=one), FakeResponse(body=two), FakeResponse(body=none)])
    assert c.quotes(["AAPL"])["AAPL"]["last"] == 273.47
    both = c.quotes(["AAPL", "BRK.B"])
    assert set(both) == {"AAPL", "BRK.B"}
    assert session.calls[1][1]["symbols"] == "AAPL,BRK/B"                 # share classes use a slash on the wire
    assert c.quotes(["NOPE"]) == {}

    c, _ = client([FakeResponse(body={"expirations": {"date": "2021-02-05"}}),
                   FakeResponse(body={"expirations": {"date": ["2021-03-19", "2021-02-12"]}}),
                   FakeResponse(body={"expirations": None})])
    assert c.expirations("AAPL") == ["2021-02-05"]
    assert c.expirations("AAPL") == ["2021-02-12", "2021-03-19"]
    assert c.expirations("AAPL") == []


def test_history_parsing_and_empty_history():
    body = {"history": {"day": [{"date": "2020-01-03", "open": 74.3, "high": 75.1, "low": 74.1, "close": 74.4, "volume": 1},
                                {"date": "2020-01-02", "open": 76.84, "high": 77.15, "low": 76.19, "close": 77.05, "volume": 2},
                                {"date": "2020-01-06", "open": "NaN", "high": 1, "low": 1, "close": 1, "volume": 3}]}}
    c, session = client([FakeResponse(body=body), FakeResponse(body={"history": None})])
    bars = c.history("BRK.B", "2020-01-01", "2020-01-31")
    assert session.calls[0][1]["symbol"] == "BRK/B"
    assert list(bars["close"]) == [77.05, 74.4]                           # sorted, bad row dropped
    assert c.history("AAPL", "2020-01-01", "2020-01-31").empty


def test_errors_are_explained_and_transient_failures_retried(monkeypatch):
    monkeypatch.setattr("tradeassist.providers.tradier.time.sleep", lambda s: None)
    c, _ = client([FakeResponse(status=401, text="Invalid token")])
    with pytest.raises(DataError, match="rejected the API token"):
        c.clock()
    ok = FakeResponse(body={"clock": {"state": "open", "description": "Market is open"}})
    c, session = client([FakeResponse(status=503), requests.ConnectionError("boom"), ok])
    assert c.clock()["state"] == "open" and len(session.calls) == 3
    c, session = client([FakeResponse(status=429)] * 3)
    with pytest.raises(DataError, match="after 3 tries"):
        c.clock()
    c, _ = client([FakeResponse(status=400, text="bad expiration")])
    with pytest.raises(DataError, match="HTTP 400"):
        c.chain("AAPL", "nope")
    c, _ = client([FakeResponse(status=200, body=None)])
    with pytest.raises(DataError, match="non-JSON"):
        c.clock()


@pytest.mark.parametrize("expiry_header", ["1700000012000", "1700000012"])   # milliseconds or seconds
def test_rate_limit_headers_cause_a_wait(monkeypatch, expiry_header):
    sleeps = []
    monkeypatch.setattr("tradeassist.providers.tradier.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("tradeassist.providers.tradier.time.time", lambda: 1_700_000_000.0)
    headers = {"X-Ratelimit-Available": "0", "X-Ratelimit-Expiry": expiry_header}
    body = {"clock": {"state": "open"}}
    c, _ = client([FakeResponse(body=body, headers=headers), FakeResponse(body=body)])
    c.clock()
    assert sleeps == []
    c.clock()
    assert sleeps == [pytest.approx(12.0)]


def test_client_only_reads_market_data():
    """No order, account or write capability exists on the client."""
    c, session = client([FakeResponse(body=CHAIN_BODY)])
    c.chain("AAPL", "2021-04-16")
    assert all("/markets/" in url for url, _ in session.calls)
    public = [name for name in dir(TradierData) if not name.startswith("_")]
    # 2.12 adds two reads of /markets/timesales (bars within the day). Still no order, account or write method.
    assert sorted(public) == ["chain", "clock", "expirations", "history", "intraday", "intraday_reach", "quotes"]


def test_normalize_contract_handles_seconds_timestamps():
    raw = {"symbol": "X", "strike": 10, "option_type": "CALL", "bid_date": 1612196200, "expiration_date": "2021-04-16"}
    out = normalize_contract(raw)
    assert out["bid_time"].startswith("2021-02-01") and out["option_type"] == "call" and out["contract_size"] == 100


def test_odd_reply_shapes_do_not_crash():
    weird = [FakeResponse(body={"quotes": "null"}), FakeResponse(body={"quotes": {"quote": [None, {"symbol": "AAPL", "last": "5.5"}]}}),
             FakeResponse(body={"clock": "x"}), FakeResponse(body=[1, 2]),
             FakeResponse(body={"options": {"option": [None, "junk", {"symbol": "Z", "strike": 0, "option_type": "call",
                                                                     "contract_size": 100, "expiration_date": "2030-01-18"}]}}),
             FakeResponse(body={"history": {"day": [None, {"date": "2020-01-02", "open": 1, "high": 2, "low": 1, "close": 2, "volume": None}]}}),
             FakeResponse(body={"expirations": {"date": [None, "2030-01-18", 20300125]}})]
    c, _ = client(weird)
    assert c.quotes(["AAPL"]) == {}
    assert c.quotes(["AAPL"])["AAPL"]["last"] == 5.5                         # numbers sent as text still parse
    assert c.clock()["state"] == "unknown"
    assert c.clock()["state"] == "unknown"
    assert c.chain("Z", "2030-01-18") == []                                  # a zero strike is not a contract
    bars = c.history("AAPL", "2020-01-01", "2020-01-31")
    assert len(bars) == 1 and bars["volume"].iloc[0] == 0
    assert c.expirations("AAPL") == ["2030-01-18"]
