"""News-to-trade intelligence (2.16): what a headline is, whether the stock is tradable now, the effect, how much
is in the price, and the signal; and the five steps on the live feed's signals."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from tradeassist import livenews as L
from tradeassist import newsintel as N

NOW = datetime(2026, 10, 19, 14, 30, tzinfo=timezone.utc)   # 10:30 New York, a Monday
DAILY = {"prev_close": 100.0, "atr": 3.0, "dollar_volume": 400e6, "bars": 300}


def _quote(**kw):
    q = {"last": 100.0, "bid": 99.98, "ask": 100.02, "bidsize": 5, "asksize": 5, "trade_time": NOW.isoformat(),
         "bid_time": NOW.isoformat(), "ask_time": NOW.isoformat(), "prevclose": 100.0}
    q.update(kw)
    return q


# --- what the headline is -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("headline, kind", [
    ("Acme Corp to be acquired by Globex for $40 per share in cash", "acquisition_target"),
    ("Globex agrees to acquire Acme Corp for $4 billion", "acquirer"),
    ("Acme reports third quarter results; beats estimates", "earnings"),
    ("Acme cuts full-year guidance, citing weaker demand", "guidance_cut"),
    ("Acme raises 2026 outlook after record quarter", "guidance_raise"),
    ("Acme receives complete response letter from FDA for its lead drug", "fda_negative"),
    ("Acme receives FDA approval for its lead treatment", "fda_positive"),
    ("Acme prices $300 million public offering of common stock", "offering"),
    ("Acme announces $2 billion share repurchase program", "buyback_dividend"),
    ("Acme awarded multi-year contract by the US Navy", "contract"),
    ("Acme chief executive to step down", "executive"),
    ("Acme files for Chapter 11 bankruptcy protection", "bankruptcy"),
    ("Acme to host third quarter results conference call on October 30", "scheduling"),
    ("Nasdaq halts trading in Acme pending news", "halt"),
    ("Acme discloses SEC investigation into sales practices", "legal_regulatory"),
    ("Acme announces something about its new campus", "other"),
])
def test_headlines_are_sorted_into_kinds(headline, kind):
    assert N.classify(headline)["catalyst"] == kind


def test_the_sources_own_words_come_before_the_headline():
    filing = N.classify("8-K: Acme — Results of operations", kind="filing", extra={"items": ["2.02", "9.01"]})
    assert filing["catalyst"] == "earnings" and filing["via"] == "8-K item 2.02"
    assert N.classify("8-K: Acme", kind="filing", extra={"items": ["1.03"]})["catalyst"] == "bankruptcy"
    rtpr = N.classify("Acme news", extra={"impact": {"event_type": "fda_approval", "score": 90, "direction": "bullish"}})
    assert rtpr["catalyst"] == "fda_positive" and rtpr["via"] == "RTPR event type" and rtpr["direction"] == 1
    # RTPR's direction wins over the kind's usual one: it read the whole release.
    bear = N.classify("Acme raises outlook", extra={"impact": {"direction": "bearish", "score": 80}})
    assert bear["direction"] == -1 and bear["major"]


def test_direction_and_severity():
    beat = N.classify("Acme reports second quarter results; tops estimates and raises outlook")
    assert beat["direction"] == 1 and beat["persistence"] == "drift"
    miss = N.classify("Acme reports second quarter results; misses expectations", tone=-1.0)
    assert miss["direction"] == -1
    plain = N.classify("Acme reports second quarter results")
    assert plain["direction"] == 0 and plain["severity"] < N.CATALYSTS["earnings"]["severity"]   # nothing to trade on
    target = N.classify("Acme to be acquired for $40 a share, a 30% premium")
    assert target["severity"] == 100 and target["major"] and target["persistence"] == "pinned"
    call = N.classify("Acme schedules quarterly results call")
    assert not call["major"] and call["severity"] < 10
    blended = N.classify("Acme wins contract", extra={"impact": {"score": 20}})
    assert blended["severity"] == round(0.5 * N.CATALYSTS["contract"]["severity"] + 10)


# --- tradable now? ---------------------------------------------------------------------------------------------------

def test_tradability_reads_the_quote():
    ok = N.tradability(_quote(), "open", DAILY, now=NOW)
    assert ok["ok"] and ok["state"] == "ok" and ok["spread_bps"] == pytest.approx(4.0, abs=0.1) and ok["price"] == 100.0
    assert N.tradability(_quote(), "closed", DAILY, now=NOW)["state"] == "closed"
    stale = _quote(trade_time=(NOW - timedelta(minutes=3)).isoformat())
    assert N.tradability(stale, "open", DAILY, now=NOW)["state"] == "halt_suspected"       # a liquid stock, no print in 3 min
    assert N.tradability(stale, "open", {**DAILY, "dollar_volume": 5e6}, now=NOW)["state"] == "thin"   # an illiquid one: just thin
    assert N.tradability(_quote(bid=None, ask=None), "open", DAILY, now=NOW)["state"] == "halt_suspected"
    assert N.tradability(_quote(bid=99.0, ask=101.0), "open", DAILY, now=NOW)["state"] == "wide_spread"
    assert N.tradability(_quote(bid=99.0, ask=101.0), "extended", DAILY, now=NOW)["state"] == "wide_spread"
    assert N.tradability(_quote(bid=99.5, ask=100.5), "extended", DAILY, now=NOW)["state"] == "ok"   # 100 bps is fine after hours
    cheap = _quote(last=3.0, bid=2.99, ask=3.01)
    assert N.tradability(cheap, "open", DAILY, now=NOW)["state"] == "cheap"
    old = _quote(trade_time=(NOW - timedelta(hours=1)).isoformat())
    assert N.tradability(old, "extended", DAILY, now=NOW)["state"] == "stale"
    assert N.tradability({}, "open", DAILY, now=NOW)["state"] == "no_price"
    held = N.tradability(_quote(), "open", DAILY, now=NOW, holdings=[{"what": "long 50 shares"}])
    assert any("You hold long 50 shares" in n for n in held["notes"])


# --- how much is in the price -----------------------------------------------------------------------------------------

def test_priced_in_measures_the_move_against_the_usual_one():
    cat = N.classify("Acme raises full-year outlook")                    # +1, usually about 5%
    fresh = N.priced_in(ref_price=100, price=100.3, prev_close=99, atr=3, catalyst=cat)
    assert fresh["state"] == "fresh" and fresh["basis"].startswith("since the app")
    partly = N.priced_in(ref_price=100, price=102.5, prev_close=99, atr=3, catalyst=cat)
    assert partly["state"] == "partly" and partly["ratio"] == pytest.approx(0.5)
    mostly = N.priced_in(ref_price=100, price=104.5, prev_close=99, atr=3, catalyst=cat)
    assert mostly["state"] == "mostly"
    full = N.priced_in(ref_price=100, price=108, prev_close=99, atr=3, catalyst=cat)
    assert full["state"] == "full" and "snap back" in full["text"]
    against = N.priced_in(ref_price=100, price=97, prev_close=99, atr=3, catalyst=cat)
    assert against["state"] == "against"
    # Outside the session the day's move is the reaction, measured from the last close.
    overnight = N.priced_in(ref_price=104, price=104, prev_close=100, atr=3, catalyst=cat, in_session=False)
    assert overnight["move_pct"] == pytest.approx(4.0) and overnight["state"] == "mostly"
    # With a record of replays, the usual move is the record's.
    record = {"cases": 12, "median_jump_pct": 10.0}
    with_record = N.priced_in(ref_price=100, price=103.0, prev_close=99, atr=3, catalyst=cat, record=record)
    assert with_record["usual_pct"] == 10.0 and with_record["state"] == "partly" and "12 replays" in with_record["usual_from"]
    assert N.priced_in(ref_price=None, price=None, prev_close=99, atr=3, catalyst=cat)["state"] == "unknown"


def test_the_second_leg_verdicts():
    assert N.second_leg(None)["verdict"] == "unproven"
    assert N.second_leg({"cases": 3, "median_net_pct": 1.0, "hit_rate": 1.0})["verdict"] == "unproven"
    good = N.second_leg({"cases": 20, "median_net_pct": 0.4, "hit_rate": 0.6, "label": "guidance raised"})
    assert good["verdict"] == "exploitable" and "guidance raised" in good["text"]
    gone = N.second_leg({"cases": 20, "median_net_pct": -0.2, "hit_rate": 0.4})
    assert gone["verdict"] == "gone"


# --- the five steps together ------------------------------------------------------------------------------------------

def _item(headline, **kw):
    it = {"headline": headline, "summary": "", "kind": "press_release", "extra": {}, "tone": None,
          "published_at": (NOW - timedelta(seconds=1)).isoformat(), "received_at": NOW.isoformat()}
    it.update(kw)
    return it


def test_a_takeover_target_is_passed_and_a_guidance_raise_entered():
    out = N.assess(_item("Acme to be acquired by Globex for $40 per share"), _quote(last=138, bid=137.9, ask=138.1), "open",
                   DAILY, ref_price=138.0, prev_close=100.0, now=NOW)
    assert [s["step"] for s in out["steps"]] == ["detected", "tradable", "effect", "priced_in", "signal"]
    assert out["signal"]["action"] == "pass" and "deal price" in out["signal"]["why"] and out["signal"]["direction"] is None
    raise_ = N.assess(_item("Acme raises full-year outlook"), _quote(last=102, bid=101.98, ask=102.02), "open", DAILY,
                      ref_price=100.0, prev_close=99.0, now=NOW)
    assert raise_["signal"] == {"action": "enter", "direction": "LONG", "why": raise_["signal"]["why"]}
    assert "unproven" in raise_["signal"]["why"]
    assert raise_["steps"][-1]["state"] == "pass" and raise_["priced_in"]["state"] == "partly"
    # With a record that says the second leg is there, the signal says so.
    record = {"cases": 20, "median_net_pct": 0.5, "hit_rate": 0.6, "label": "guidance raised", "median_jump_pct": 4.0}
    proven = N.assess(_item("Acme raises full-year outlook"), _quote(last=102, bid=101.98, ask=102.02), "open", DAILY,
                      ref_price=100.0, prev_close=99.0, now=NOW, record=record)
    assert proven["signal"]["action"] == "enter" and "second leg has been there" in proven["signal"]["why"]


def test_waiting_and_passing_for_the_right_reasons():
    # The market is closed: wait.
    closed = N.assess(_item("Acme raises full-year outlook"), _quote(), "closed", DAILY, ref_price=None, prev_close=100.0, now=NOW)
    assert closed["signal"]["action"] == "wait" and closed["steps"][1]["state"] == "fail"
    # Results with no word on how they compared: no direction, wait for the price.
    plain = N.assess(_item("Acme reports third quarter results"), _quote(), "open", DAILY, ref_price=100.0, prev_close=99.0, now=NOW)
    assert plain["signal"]["action"] == "wait" and "no direction" in plain["signal"]["why"]
    # A halt: pass.
    halted = N.assess(_item("Acme receives FDA approval"), _quote(trade_time=(NOW - timedelta(minutes=5)).isoformat()), "open",
                      DAILY, ref_price=100.0, prev_close=99.0, now=NOW)
    assert halted["signal"]["action"] == "pass" and "Halted" in halted["signal"]["why"]
    # Fully priced: pass.
    full = N.assess(_item("Acme raises full-year outlook"), _quote(last=110, bid=109.9, ask=110.1), "open", DAILY,
                    ref_price=100.0, prev_close=99.0, now=NOW)
    assert full["signal"]["action"] == "pass" and "More than the usual move" in full["signal"]["why"]
    # Nothing has moved yet on major news: wait for the price to confirm.
    quiet = N.assess(_item("Acme raises full-year outlook"), _quote(last=100.2, bid=100.18, ask=100.22), "open", DAILY,
                     ref_price=100.0, prev_close=99.0, now=NOW)
    assert quiet["signal"]["action"] == "wait" and "Wait for the price" in quiet["signal"]["why"]
    # The record says the move was over: pass.
    gone = {"cases": 20, "median_net_pct": -0.3, "hit_rate": 0.4, "label": "contract wins"}
    over = N.assess(_item("Acme awarded multi-year contract"), _quote(last=101.5, bid=101.48, ask=101.52), "open", DAILY,
                    ref_price=100.0, prev_close=99.0, now=NOW, record=gone)
    assert over["signal"]["action"] == "pass" and "over before" in over["signal"]["why"]
    # A minor item is listed, not traded.
    minor = N.assess(_item("Acme to host results call on October 30"), _quote(), "open", DAILY, ref_price=100.0,
                     prev_close=99.0, now=NOW)
    assert minor["signal"]["action"] == "wait"                                   # no direction comes first
    other = N.assess(_item("Acme opens a new campus, strong growth expected"), _quote(last=100.5, bid=100.48, ask=100.52),
                     "open", DAILY, ref_price=100.0, prev_close=99.0, now=NOW)
    assert other["signal"]["action"] == "pass" and "minor" in other["signal"]["why"]


# --- on the live feed ----------------------------------------------------------------------------------------------------

@pytest.fixture
def feed(tmp_path):
    from tradeassist.analysis import Analyst
    from tradeassist.config import Settings
    from tradeassist.db import Database
    from tradeassist.morning import Morning
    from tradeassist.providers.demo import DemoData
    from tradeassist.service import TradeAssistant
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", live_mover_sweep_minutes=0)
    svc = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    analyst = Analyst(svc)
    return L.LiveFeed(svc, Morning(svc, analyst, s.features), analyst)


def test_each_signal_carries_the_five_steps_and_each_item_its_kind(feed):
    item = L.make_item("Demo", "i1", ["NVDA"], "NVDA raises full-year outlook after record quarter", published=L.now_utc(),
                       kind="press_release")
    item["extra"]["demo_move"] = 0.05
    feed.ingest(item)
    (sig,) = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals")]
    quote = feed.svc.market.provider.quotes(["NVDA"])["NVDA"]
    feed.check(sig, quote, "open")
    (sig,) = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals")]
    intel = sig["intel"]
    assert intel and [s["step"] for s in intel["steps"]] == ["detected", "tradable", "effect", "priced_in", "signal"]
    assert intel["catalyst"]["catalyst"] == "guidance_raise" and intel["tradable"]["ok"]
    assert intel["signal"]["action"] == "wait"                                   # the price has not answered yet
    feed._demo_runs[sig["id"]] = (time.time() - 130, 0.05)
    feed.check(sig, quote, "open")
    (sig,) = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals")]
    assert sig["state"] == "opportunity" and sig["intel"]["signal"]["action"] == "enter"
    assert sig["intel"]["signal"]["direction"] == "LONG" and sig["intel"]["priced_in"]["state"] in ("mostly", "full")
    snap = feed.snapshot()
    assert snap["items"][0]["catalyst"]["catalyst"] == "guidance_raise" and snap["signals"][0]["intel"]["steps"]
    assert json.loads(feed.db.one("SELECT intel_json FROM live_signals")["intel_json"])["signal"]["action"] == "enter"


def test_a_failed_assessment_never_stops_a_price_check(feed, monkeypatch):
    import tradeassist.newsintel as intel_mod
    monkeypatch.setattr(intel_mod, "assess", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    feed.ingest(L.make_item("Demo", "i2", ["KO"], "KO raises its outlook", published=L.now_utc()))
    (sig,) = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals")]
    feed.check(sig, feed.svc.market.provider.quotes(["KO"])["KO"], "open")
    (sig,) = [L.signal_row(r) for r in feed.db.query("SELECT * FROM live_signals")]
    assert sig["state"] == "watching" and sig["intel"] is None
