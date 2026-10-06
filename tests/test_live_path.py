"""The whole app running through the real Tradier client, with the network replaced by a
stand-in that answers in Tradier's documented wire format. This is the closest the test
suite gets to a live run; it does not prove the real API behaves as documented."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.providers.tradier import TradierData
from tradeassist.service import TradeAssistant


def epoch_ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class Reply:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, ""
        self.headers = {"X-Ratelimit-Available": "100", "X-Ratelimit-Expiry": "1700000000000"}

    def json(self):
        return self._body


class WireSession:
    """Answers GET requests the way Tradier's reference documents them, from demo prices."""
    def __init__(self, with_greeks, state="open"):
        self.demo = DemoData(with_greeks=with_greeks)
        self.state = state
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("/v1")[1]
        self.calls.append(path)
        if path == "/markets/clock":
            return Reply({"clock": {"date": "2026-10-05", "description": "Market is open from 09:30 to 16:00",
                                    "state": self.state, "timestamp": 1, "next_change": "16:00",
                                    "next_state": "postmarket"}})
        if path == "/markets/quotes":
            symbols = params["symbols"].split(",")
            assert all("." not in s for s in symbols)                          # share classes arrive as BRK/B
            quotes = []
            for symbol, q in self.demo.quotes(symbols).items():
                if q["type"] == "option":
                    quotes.append({"symbol": symbol, "description": q["description"], "type": "option",
                                   "last": q["last"], "bid": q["bid"], "ask": q["ask"], "bidsize": int(q["bidsize"]),
                                   "asksize": int(q["asksize"]), "volume": int(q["volume"]),
                                   "bid_date": 1791217927412, "ask_date": 1791217927398,   # epoch milliseconds
                                   "open_interest": 10, "contract_size": 100, "option_type": "call"})
                    continue
                quotes.append({"symbol": symbol.replace(".", "/"), "description": q["description"], "type": "stock",
                               "last": q["last"], "bid": q["bid"], "ask": q["ask"], "bidsize": 3, "asksize": 3,
                               "prevclose": q["prevclose"], "close": None, "volume": int(q["volume"]),
                               "week_52_high": q["week_52_high"], "week_52_low": q["week_52_low"],
                               "bid_date": epoch_ms(q["bid_time"]), "ask_date": epoch_ms(q["ask_time"])})
            if not quotes:
                return Reply({"quotes": {"unmatched_symbols": {"symbol": params["symbols"]}}})
            return Reply({"quotes": {"quote": quotes[0] if len(quotes) == 1 else quotes}})
        if path == "/markets/history":
            bars = self.demo.history(params["symbol"], params["start"], params["end"])
            days = [{"date": d.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c, "volume": int(v)}
                    for d, o, h, l, c, v in bars.itertuples(index=False, name=None)]
            return Reply({"history": {"day": days}})
        if path == "/markets/options/expirations":
            return Reply({"expirations": {"date": self.demo.expirations(params["symbol"])}})
        if path == "/markets/options/chains":
            assert params["greeks"] == "true"
            options = []
            for c in self.demo.chain(params["symbol"], params["expiration"]):
                raw = {"symbol": c["symbol"], "description": c["description"], "type": "option", "last": c["last"],
                       "volume": int(c["volume"]), "bid": c["bid"], "ask": c["ask"], "underlying": c["underlying"],
                       "strike": c["strike"], "bidsize": int(c["bidsize"]), "asksize": int(c["asksize"]),
                       "bid_date": epoch_ms(c["bid_time"]), "ask_date": epoch_ms(c["ask_time"]),
                       "open_interest": int(c["open_interest"]), "contract_size": 100,
                       "expiration_date": c["expiration"], "expiration_type": "standard",
                       "option_type": c["option_type"], "root_symbol": c["underlying"]}
                if c["greeks"]:
                    raw["greeks"] = {**{k: c["greeks"][k] for k in ("delta", "gamma", "theta", "vega", "mid_iv", "smv_vol")},
                                     "rho": 0.01, "phi": -0.01, "bid_iv": 0.0, "ask_iv": 0.0,
                                     "updated_at": "2026-10-05 15:00:07"}
                options.append(raw)
            return Reply({"options": {"option": options}})
        return Reply({"fault": "unknown path"}, status=404)


@pytest.mark.parametrize("sandbox", [False, True])
def test_app_end_to_end_through_the_tradier_client(sandbox):
    session = WireSession(with_greeks=not sandbox)
    provider = TradierData("token", sandbox=sandbox, session=session)
    settings = Settings(account_size=50_000, watchlist=["AAPL", "BRK.B", "KO", "MSFT", "JPM"])
    assistant = TradeAssistant(settings, provider=provider, db=Database(":memory:"))
    assistant._traded_today = lambda: True
    client = TestClient(create_app(settings, assistant))

    status = client.get("/api/status").json()
    assert status["data"]["mode"] == ("sandbox" if sandbox else "live") and status["data"]["realtime"] is not sandbox
    assert status["clock"]["state"] == "open"

    exps = client.get("/api/expirations", params={"symbol": "brk.b"}).json()
    exp = next(e["date"] for e in exps["expirations"] if e["dte"] >= 25)
    chain = client.get("/api/chain", params={"symbol": "BRK.B", "expiration": exp}).json()
    row = min((c for c in chain["contracts"] if c["option_type"] == "call"), key=lambda c: abs(c["strike"] - chain["spot"]))
    assert row["greeks_source"] == ("computed" if sandbox else "provider")
    assert row["model_source"] == ("smile fitted across strikes" if sandbox else "provider smoothed volatility")

    priced = client.post("/api/price", json={"symbol": "BRK.B", "expiration": exp, "option_type": "call",
                                             "strike": row["strike"]}).json()
    assert priced["ladder"]["ok"] and priced["scorecard"]["checks"]
    assert priced["data"]["quote_age_seconds"] is not None and priced["data"]["quote_age_seconds"] < 120
    delayed = [w for w in priced["warnings"] if "delayed 15 minutes" in w["text"]]
    assert bool(delayed) is sandbox                                             # only sandbox quotes get the warning
    assert not any("made up" in w["text"] for w in priced["warnings"])
    assert priced["earnings"] is None                                           # no SEC contact configured
    earnings = next(c for c in priced["scorecard"]["checks"] if c["key"] == "earnings")
    assert earnings["status"] == "warn" and "unknown" in earnings["value"]
    assert assistant.iv_rank("BRK.B")["days"] == 1

    # Live update: one quotes request carrying the stock and the option, timestamps kept to the millisecond.
    calls_before = len(session.calls)
    live = client.post("/api/live", json={"symbol": "BRK.B", "expiration": exp, "option_type": "call",
                                          "strike": row["strike"]}).json()
    assert session.calls[calls_before:] == ["/markets/quotes"]
    assert live["bid_time"].endswith(".412000+00:00") and live["ask_time"].endswith(".398000+00:00")
    assert live["poll_seconds"] == (3.0 if sandbox else 1.0) and live["ladder"]["ok"]
    assert live["bid"] == priced["contract"]["bid"] and live["spot"] == pytest.approx(priced["underlying"]["spot"])

    assert client.get("/api/research/KO").json()["company"]["available"] is False
    study = client.post("/api/patterns/run", json={"years": 4, "horizons": [10]}).json()
    assert study["verdicts_given"] and study["data_source"] == ("sandbox" if sandbox else "live")
    bt = client.post("/api/backtest", json={"years": 3}).json()
    assert bt["num_trades"] > 0 and sorted(bt["symbols"]) == ["AAPL", "BRK.B", "JPM", "KO", "MSFT"]
    assert set(session.calls) <= {"/markets/clock", "/markets/quotes", "/markets/history",
                                  "/markets/options/expirations", "/markets/options/chains"}


def test_after_hours_spot_uses_the_close_not_an_after_hours_quote():
    session = WireSession(with_greeks=True, state="closed")
    provider = TradierData("token", sandbox=False, session=session)
    assistant = TradeAssistant(Settings(), provider=provider, db=Database(":memory:"))
    real_get = session.get

    def get(url, params=None, timeout=None):
        reply = real_get(url, params, timeout)
        if url.endswith("/markets/quotes"):
            quote = reply._body["quotes"]["quote"]
            quote.update({"close": 200.0, "last": 207.5, "bid": 207.4, "ask": 207.6})   # after-hours move
        return reply

    session.get = get
    assert assistant.market.spot("AAPL") == 200.0
    session.state = "open"
    assistant.market._clock = None
    assert assistant.market.spot("AAPL") == pytest.approx(207.5)
