"""2.8 Morning plan: movers, setups, the past record, the limit cap, picks, watches and the email.

The rules pinned here: news decides ride against fade; a fade needs a big, overextended move; the limit
is never above the price where target 1 only pays `morning_min_payoff` times what the stop loses; unknown
news is never "no news"; and nothing here places an order.
"""

from __future__ import annotations

import smtplib
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import tradeassist.morning as morning_mod
from tradeassist import mailer
from tradeassist.analysis import Analyst
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.features import FEATURE_DEFAULTS
from tradeassist.morning import (Morning, WatchChecker, add_watch, classify, features_of, floor_tick, limit_prices,
                                 list_watches, movers_universe, overextended, past_record, record_picks,
                                 remove_watch, scorecard, tick_of)
from tradeassist.morning_email import render
from tradeassist.providers.demo import DemoData
from tradeassist.scanner import TOP_200
from tradeassist.service import TradeAssistant


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    yield service
    service.db.close()


def frame(closes, start="2024-01-02") -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(c))
    return pd.DataFrame({"date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": np.full(len(c), 1e6)})


# --- the universe and the arithmetic helpers -----------------------------------------------------------

def test_the_universe_is_the_large_stocks_plus_the_watchlist():
    names = movers_universe(["AAPL", "zzzz ", ""])
    assert set(TOP_200) <= set(names) and "ZZZZ" in names
    assert names == sorted(set(names)) and len(names) < 400          # fixed lists, no small caps


def test_option_ticks_round_down_never_to_zero():
    assert tick_of(2.99) == 0.01 and tick_of(3.0) == 0.05
    assert floor_tick(1.6749) == pytest.approx(1.67) and floor_tick(3.21) == pytest.approx(3.20)
    assert floor_tick(0.003) == 0.01                                  # never rounded to nothing


def test_the_limit_cap_is_where_the_target_pays_the_minimum():
    # V_t = 4, V_s = 1, r = 1: the cap is (4+1)/2 = 2.50, where gain (1.50) equals risk (1.50).
    out = limit_prices(mid=2.00, bid=1.9, value_at_target=4.0, value_at_stop=1.0, min_payoff=1.0)
    assert out["viable"] and out["cap"] == pytest.approx(2.50) and out["suggested"] == pytest.approx(2.00)
    assert out["payoff_at_suggested"] == pytest.approx(2.0)           # (4-2)/(2-1)
    # Paying exactly the cap pays exactly the minimum; any mid above it is refused toward the shares.
    dear = limit_prices(mid=2.60, bid=2.5, value_at_target=4.0, value_at_stop=1.0, min_payoff=1.0)
    assert not dear["viable"] and "shares" in dear["why"].lower()
    # A stricter minimum lowers the cap: r = 1.5 → (4 + 1.5) / 2.5 = 2.20.
    strict = limit_prices(mid=2.00, bid=None, value_at_target=4.0, value_at_stop=1.0, min_payoff=1.5)
    assert strict["cap"] == pytest.approx(2.20)
    # Worth no more at the target than at the stop: no price works.
    dead = limit_prices(mid=1.0, bid=None, value_at_target=0.5, value_at_stop=0.6, min_payoff=1.0)
    assert not dead["viable"] and "no price makes this trade pay" in dead["why"]


def test_payoff_at_or_below_the_cap_is_at_least_the_minimum():
    for mid, vt, vs, r in ((1.2, 3.0, 0.2, 1.0), (0.8, 2.0, 0.0, 1.5), (4.4, 9.0, 1.0, 2.0)):
        out = limit_prices(mid=mid, bid=None, value_at_target=vt, value_at_stop=vs, min_payoff=r)
        if out["viable"]:
            assert out["payoff_at_suggested"] >= r - 1e-6 and out["suggested"] <= out["cap"] + 1e-9


# --- setups ---------------------------------------------------------------------------------------------

def test_news_rides_and_no_news_overextended_fades():
    ride = classify(ranges=2.0, move_pct=5.0, direction=1, news_state="news", overext=False, overext_why=[])
    assert ride["kind"] == "ride" and ride["direction"] == "LONG" and "news" in ride["why"].lower()
    fade = classify(ranges=3.0, move_pct=-8.0, direction=-1, news_state="no_news", overext=True,
                    overext_why=["RSI 22"])
    assert fade["kind"] == "fade" and fade["direction"] == "LONG" and "RSI 22" in fade["why"]
    spike = classify(ranges=3.0, move_pct=8.0, direction=1, news_state="no_news", overext=True, overext_why=["x"])
    assert spike["direction"] == "SHORT"


def test_unknown_news_is_never_read_as_no_news():
    out = classify(ranges=4.0, move_pct=9.0, direction=1, news_state="unchecked", overext=True, overext_why=["x"])
    assert out["kind"] is None and "could not be checked" in out["why"]


def test_small_or_calm_moves_get_no_setup():
    small = classify(ranges=1.2, move_pct=4.0, direction=1, news_state="news", overext=False, overext_why=[])
    assert small["kind"] is None and "ordinary" in small["why"].lower()
    calm = classify(ranges=3.0, move_pct=-6.0, direction=-1, news_state="no_news", overext=False, overext_why=[])
    assert calm["kind"] is None and "not overextended" in calm["why"]
    thin = classify(ranges=2.2, move_pct=-6.0, direction=-1, news_state="no_news", overext=True, overext_why=["x"])
    assert thin["kind"] is None and "not large enough to fade" in thin["why"]


def test_overextension_sees_rsi_stretch_and_runs():
    down = list(np.linspace(100, 70, 12))                            # a hard one-way slide
    f = features_of(frame([100.0] * 60 + down))
    ok, why = overextended(f, len(f["close"]) - 1, -1)
    assert ok and any("RSI" in w for w in why) and any("down days in a row" in w for w in why)
    flat = features_of(frame([100.0] * 80))
    assert overextended(flat, 79, -1) == (False, [])


# --- the past record --------------------------------------------------------------------------------------

def test_the_past_record_counts_what_followed_the_same_pattern():
    # One stock, two engineered 10% drops after slides (overextended); each bounces the next 3 days.
    closes = [100.0] * 60
    for _ in range(2):
        closes += list(np.linspace(closes[-1], closes[-1] * 0.93, 8)) + [closes[-1] * 0.93 * 0.90]
        closes += [closes[-1] * 1.02, closes[-1] * 1.04, closes[-1] * 1.05]
        closes += [closes[-1]] * 30
    f = features_of(frame(closes))
    rec = past_record({"X": f}, move_direction=-1, min_ranges=2.0, need_overext=True, trade_direction=1)
    h3 = rec["horizons"]["3"]
    assert rec["cases"] >= 2 and h3["frequency"] == 1.0 and h3["cases"] >= 2 and not h3["enough"]
    assert "overextended" in rec["what"]
    # The same events traded the other way lose.
    bad = past_record({"X": f}, move_direction=-1, min_ranges=2.0, need_overext=True, trade_direction=-1)
    assert bad["horizons"]["3"]["frequency"] == 0.0


def test_the_past_record_needs_both_the_ranges_and_the_percent():
    # A 2% dip is many "ranges" for a dead-calm series but under the 3% floor: never an event.
    closes = [100.0] * 100 + [98.0] + [100.0] * 10
    f = features_of(frame(closes))
    rec = past_record({"X": f}, move_direction=-1, min_ranges=2.0, need_overext=False, trade_direction=1)
    assert rec["cases"] == 0


# --- the move reading -------------------------------------------------------------------------------------

def test_a_premarket_quote_reads_against_yesterday_and_a_stale_one_against_the_day_before(svc):
    m = Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS))
    f = features_of(frame([100.0] * 59 + [104.0]))
    pre = m._move({"last": 96.2}, f)
    assert pre["reference"] == pytest.approx(104.0) and pre["move_pct"] == pytest.approx(-7.5)
    assert "close" in pre["move_is"]
    stale = m._move({"last": 104.0}, f)                              # the quote still sits on the last close
    assert stale["reference"] == pytest.approx(100.0) and stale["move_pct"] == pytest.approx(4.0)
    assert "close-to-close" in stale["move_is"]
    assert m._move({"last": None}, f) is None


# --- the card ---------------------------------------------------------------------------------------------

def test_the_card_caps_the_limit_and_falls_back_to_shares(svc):
    m = Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS))
    spot = svc.market.spot("AAPL")
    card = m.card(symbol="AAPL", direction="LONG", entry=spot, stop=spot * 0.95, target_1=spot * 1.06,
                  target_2=spot * 1.1, horizon_days=5, setup="fade")
    assert card["order_text"].startswith("Buy") and "Limit" in card["order_text"]
    opt = card["option"]
    if opt and opt["limits"]["viable"]:
        lm = opt["limits"]
        assert lm["suggested"] <= lm["cap"] + 1e-9 and lm["patient"] < lm["suggested"]
        assert opt["cost"] == pytest.approx(opt["contracts"] * lm["suggested"] * 100)
        assert card["order_text"].endswith("Good for day")
    # Levels on the wrong side are refused.
    with pytest.raises(ValueError, match="stop must sit behind"):
        m.card(symbol="AAPL", direction="LONG", entry=spot, stop=spot * 1.1, target_1=spot * 1.2,
               target_2=spot * 1.3, horizon_days=5, setup="fade")


def test_without_a_chain_the_shares_carry_the_plan(svc):
    m = Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS))
    def no_exp(symbol):
        raise morning_mod.DataError("no options listed")
    svc.market.expirations = no_exp
    spot = svc.market.spot("KO")
    card = m.card(symbol="KO", direction="SHORT", entry=spot, stop=spot * 1.05, target_1=spot * 0.9,
                  target_2=spot * 0.85, horizon_days=5, setup="ride")
    assert card["option"] is None and any("No contract" in n for n in card["notes"])
    assert card["order_text"].startswith("Sell short") and card["shares"]["count"] >= 1


def test_a_stricter_minimum_payoff_lowers_the_cap(svc, tmp_path):
    strict = Settings(data_mode="demo", db_path=tmp_path / "s.db", morning_min_payoff=2.0)
    svc2 = TradeAssistant(strict, provider=DemoData(), db=Database(tmp_path / "s.db"))
    try:
        m1 = Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS))
        m2 = Morning(svc2, Analyst(svc2), dict(FEATURE_DEFAULTS))
        spot = svc.market.spot("MSFT")
        kw = dict(symbol="MSFT", direction="LONG", entry=spot, stop=spot * 0.94, target_1=spot * 1.08,
                  target_2=spot * 1.12, horizon_days=5, setup="fade")
        a, b = m1.card(**kw)["option"], m2.card(**kw)["option"]
        if a and b and a["limits"].get("cap") and b["limits"].get("cap"):
            assert b["limits"]["cap"] <= a["limits"]["cap"] + 1e-9
            assert b["limits"]["min_payoff"] == 2.0
    finally:
        svc2.db.close()


# --- picks and the scorecard -------------------------------------------------------------------------------

def test_picks_are_recorded_once_and_graded_by_the_stock_plan(svc):
    plan = {"day": date.today().isoformat(), "cards": [
        {"symbol": "AAPL", "setup": "fade", "direction": "LONG", "entry": 100.0, "stop": 95.0, "target_1": 108.0,
         "target_2": 112.0, "horizon_days": 5, "option": None, "shares": {"count": 4}},
    ]}
    assert record_picks(svc.db, plan, "email") == 1
    assert record_picks(svc.db, plan, "email") == 0                   # the same day and setup is kept once
    old = (date.today() - timedelta(days=6)).isoformat()
    with svc.db.tx() as cur:
        cur.execute("INSERT INTO morning_picks (day, symbol, setup, direction, entry, stop, target_1, target_2, "
                    "horizon_days, instrument, shares, created_at, source) VALUES (?, 'KO', 'ride', 'SHORT', "
                    "1000000.0, 1050000.0, 900000.0, 850000.0, 5, 'PUT', 2, ?, 'email')", (old, old))
    sc = scorecard(svc)
    ko = next(p for p in sc["picks"] if p["symbol"] == "KO")
    # A short entered at an absurd high is deep in profit at any real close; the sign is what matters.
    assert ko["after"]["3"]["move_pct"] > 99.0 and ko["instrument"] == "PUT"
    assert "stock plan" in sc["note"] and sc["graded_3d"] >= 1


# --- watches -----------------------------------------------------------------------------------------------

def test_a_watch_triggers_once_marks_itself_and_survives_no_email(svc, monkeypatch):
    checker = WatchChecker(svc, Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS)))
    spot = svc.market.spot("AAPL")
    up = add_watch(svc.db, symbol="AAPL", direction="LONG", kind="above", level=spot * 0.9, why="test")
    add_watch(svc.db, symbol="AAPL", direction="SHORT", kind="below", level=spot * 0.5, why="far away")
    monkeypatch.setattr(svc.market, "clock", lambda: {"state": "open"})
    assert checker.check() is True
    rows = {w["id"]: w for w in list_watches(svc.db)}
    assert rows[up]["active"] == 0 and rows[up]["triggered_price"] == pytest.approx(spot)
    assert rows[up]["emailed"] == 0                                   # no email set up: screen only
    others = [w for w in rows.values() if w["id"] != up]
    assert others and all(w["active"] == 1 for w in others)
    remove_watch(svc.db, others[0]["id"])
    assert all(not w["active"] for w in list_watches(svc.db))
    assert checker.check() is False                                   # nothing left to watch


def test_the_checker_waits_when_the_market_is_closed(svc, monkeypatch):
    checker = WatchChecker(svc, Morning(svc, Analyst(svc), dict(FEATURE_DEFAULTS)))
    add_watch(svc.db, symbol="AAPL", direction="LONG", kind="above", level=0.01, why="would trigger at once")
    monkeypatch.setattr(svc.market, "clock", lambda: {"state": "closed"})
    assert checker.check() is True
    assert all(w["active"] for w in list_watches(svc.db))             # nothing fired off-hours


# --- the email ----------------------------------------------------------------------------------------------

def plan_stub(**over):
    base = {"day": "2026-10-14", "checked_at": "2026-10-14T09:00:00", "data_source": "demo", "clock": "premarket",
            "price_basis": "Stock prices are pre-market; option prices are the previous close's.",
            "market": {"regime": "Calm bull market", "spy_move_pct": 0.3, "spy_move_is": "vs yesterday's close"},
            "movers": [], "reversal": [], "notes": [], "min_payoff": 1.0,
            "cards": [{"symbol": "NKE", "setup": "fade", "direction": "LONG", "side_word": "buy", "entry": 71.2,
                       "stop": 68.9, "target_1": 75.4, "target_2": 77.2, "horizon_days": 5,
                       "horizon_date": "2026-10-21", "levels_why": "", "news": {"label": "No news found"},
                       "record": {"horizons": {"3": {"cases": 340, "frequency": 0.61, "low": 0.56, "high": 0.66,
                                                     "enough": True, "mean_move_pct": 1.2}}},
                       "shares": {"count": 80, "value": 5696.0, "at_target_1": 336.0, "at_stop": -184.0},
                       "option": {"option_type": "CALL", "strike": 72.0, "expiration": "2026-10-31", "dte": 17,
                                  "contracts": 2, "cost": 324.0, "at_target_1": 406.0, "if_stopped": -184.0,
                                  "limits": {"viable": True, "fair": 1.62, "suggested": 1.62, "patient": 1.56,
                                             "cap": 1.82, "min_payoff": 1.0}, "flags": []},
                       "warnings": [], "notes": [],
                       "order_text": "Buy 2 NKE 2026-10-31 $72 Call · Limit $1.62 · Good for day"}]}
    base.update(over)
    return base


def test_the_email_carries_the_order_the_cap_and_the_honesty_lines():
    subject, text, html = render(plan_stub(), {"picks": [], "graded_3d": 0, "profitable_3d": 0})
    assert "1 trade card" in subject and "(demo)" in subject
    assert "Buy 2 NKE 2026-10-31 $72 Call · Limit $1.62" in text
    assert "never above $1.82" in text and "A frequency, not a forecast" in text
    assert "never places" in text and "previous close" in text
    assert "61% higher 3 days later" in text
    assert "Buy 2 NKE 2026-10-31 $72 Call" in html and "<script" not in html


def test_an_empty_morning_says_not_trading_is_the_plan():
    subject, text, _ = render(plan_stub(cards=[]), {"picks": [], "graded_3d": 0, "profitable_3d": 0})
    assert "0 trade cards" in subject and "Not trading is the plan" in text


def test_email_html_escapes_whatever_lands_in_the_plan():
    hostile = plan_stub()
    hostile["cards"][0]["order_text"] = "<script>alert(1)</script>"
    hostile["market"]["regime"] = "<img onerror=x>"
    _, _, html = render(hostile, {"picks": [], "graded_3d": 0, "profitable_3d": 0})
    assert "<script" not in html and "<img" not in html


# --- the mailer ----------------------------------------------------------------------------------------------

class FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=0):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_the_mailer_sends_both_bodies_and_explains_a_refused_signin(tmp_path, monkeypatch):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", smtp_user="me@example.com",
                 smtp_password="app-password", email_to="you@example.com")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent.clear()
    assert mailer.configured(s) and mailer.to_address(s) == "you@example.com"
    to = mailer.send(s, "Subject line", "plain", "<p>html</p>")
    assert to == "you@example.com"
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "you@example.com" and msg["Subject"] == "Subject line"
    assert msg.get_body(("html",)) is not None and msg.get_body(("plain",)) is not None

    class Refuses(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and password not accepted app-password")
    monkeypatch.setattr(smtplib, "SMTP", Refuses)
    with pytest.raises(mailer.MailError) as err:
        mailer.send(s, "x", "y")
    text = str(err.value)
    assert "app password" in text and "app-password" not in text.replace("[the SMTP password]", "")


def test_unconfigured_email_is_a_plain_refusal(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")
    assert not mailer.configured(s)
    with pytest.raises(mailer.MailError, match="app "):
        mailer.send(s, "x", "y")


# --- the whole plan through the API -----------------------------------------------------------------------

@pytest.fixture
def client(svc):
    with TestClient(create_app(svc.settings, svc)) as c:
        yield c


def test_the_plan_runs_in_the_background_and_cards_follow_the_rules(client, monkeypatch):
    import time as _t
    monkeypatch.setattr(morning_mod, "movers_universe",
                        lambda watchlist: ["AAPL", "MSFT", "KO", "JPM", "NVDA", "CAT", "PG", "XOM", "UNH", "TSLA"])
    st = client.get("/v2/morning").json()
    assert st["job"] == {"state": "idle"} and st["result"] is None and st["email_configured"] is False
    assert client.post("/v2/morning/scan", json={}).json()["job"]["state"] == "running"
    for _ in range(300):
        st = client.get("/v2/morning?brief=true").json()
        if st["job"]["state"] != "running":
            break
        _t.sleep(0.3)
    assert st["job"]["state"] == "done"
    plan = client.get("/v2/morning").json()["result"]
    assert plan["universe_size"] == 10 and plan["rule"].endswith("places an order.")
    for r in plan["movers"]:
        assert abs(r["move_pct"]) >= 3.0 and r["news"]["state"] in ("news", "no_news", "unchecked")
        if r["setup"]["kind"] == "ride":
            assert r["news"]["state"] == "news"
        if r["setup"]["kind"] == "fade":
            assert r["news"]["state"] == "no_news" and r["overextended"]
    for c in plan["cards"]:
        d = 1 if c["direction"] == "LONG" else -1
        assert d * (c["entry"] - c["stop"]) > 0 and d * (c["target_1"] - c["entry"]) > 0
        opt = c.get("option")
        if opt and opt["limits"].get("viable"):
            assert opt["limits"]["suggested"] <= opt["limits"]["cap"] + 1e-9
    # The result survives a fresh Morning (a restart), like the ideas scan.
    svc = client.app.state.assistant
    again = Morning(svc, svc.analyst, dict(FEATURE_DEFAULTS)).status()
    assert again["result"] and again["result"]["checked_at"] == plan["checked_at"]


def test_the_flag_switches_the_morning_off(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "o.db", features={**FEATURE_DEFAULTS, "morning": False})
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "o.db"))
    with TestClient(create_app(s, service)) as c:
        assert c.get("/v2/morning").status_code == 404
        assert c.post("/v2/morning/watch", json={"symbol": "KO", "direction": "LONG", "kind": "above",
                                                 "level": 1}).status_code == 404
    service.db.close()


def test_the_morning_email_run_skips_weekends(tmp_path, monkeypatch, capsys):
    import run as run_mod
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db")

    class Sat(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 17, 9, 0, tzinfo=tz)            # a Saturday
    monkeypatch.setattr("run.datetime", Sat)
    monkeypatch.setattr(morning_mod, "movers_universe", lambda watchlist: ["AAPL"])
    code = run_mod.morning_email(s, send=False)
    out = capsys.readouterr().out
    assert code == 0 and "closed" in out and "no morning email" in out
