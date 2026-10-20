"""The Live news digest (2.15): every so often, the best opportunities the news has made, ranked and ready to blend.

Live news (livenews.py) judges each item as it arrives: it watches the price and, when the move confirms the news,
turns it into a trade card. The digest steps back from the stream. Every 15, 30 or 60 minutes (or over the whole
session so far) it gathers what confirmed in that window, plus the stocks that moved with news found behind them
("Why is it moving?"), and ranks them as day trades, each a 0-100 score with the points behind it:

    the reaction       how far the stock has moved since the news, in its own daily ranges (30)
    speed              how soon the news reached the app after it was published (10)
    tone               whether the headline's tone agrees with the move (+10) or argues with it (-10)
    still in play      a confirmed move still inside its first hour (10)
    recency            newer inside the window (10)
    volume             the stock trading faster than its normal pace for the time of day (10)
    the record         the rest-of-the-day record after a move of this size from the open (opportunities.py),
                       during the session only: no record exists for news outside trading hours (+/-20)

The best ones get a day-trade card (out by 15:55) with its contract and limit. Like the rest of the app this only
ranks and explains: nothing here places an order.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any

import numpy as np

from . import marketdays
from .opportunities import (Runs, atr_by_day_from_frame, cost_pct, day_levels, edge_from, intraday_cases,
                            intraday_record_from, make, option_of, part, total_score)
from .optionmath import NY

log = logging.getLogger(__name__)

DIGEST_CARDS = 8
DIGEST_KEEP = 25
CHOICES = {"15": 15, "30": 30, "60": 60, "session": None}


def _parse(ts: Any) -> datetime | None:
    """A time as Live news stores it (ISO in UTC); None when unreadable."""
    from .livenews import parse_time
    return parse_time(ts)


def window_for(choice: str, now: datetime | None = None) -> tuple[datetime, str]:
    """Where a digest's window starts, and its name. "session" is everything since 9:30 today (since 4:00, before the
    open; the last session's, outside trading days and before 4:00, so the window never starts after now)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    minutes = CHOICES.get(str(choice))
    if str(choice) not in CHOICES:
        raise ValueError("The digest must be every 15, 30 or 60 minutes, or session")
    if minutes is not None:
        return now - timedelta(minutes=minutes), f"the last {minutes} minutes"
    early = now < datetime.combine(now.date(), dtime(4, 0), NY)
    day = now.date() if marketdays.is_trading_day(now.date()) and not early else marketdays.previous_trading_day(now.date())
    s = marketdays.session(day)
    start = s[0] if now >= s[0] else s[0].replace(hour=4, minute=0)
    return start, f"the session since {start:%H:%M}"


class NewsDigest:
    def __init__(self, svc: Any, feed: Any, morning: Any):
        self.svc = svc
        self.feed = feed
        self.morning = morning
        self.runs = Runs(svc.db)
        self._latest: dict[str, Any] | None = None

    def _key(self) -> str:
        return f"live:digest:{self.svc.market.info.mode}"

    def latest(self) -> dict[str, Any] | None:
        if self._latest is None:
            try:
                self._latest = self.svc.db.cache_get(self._key())
            except Exception:                         # noqa: BLE001
                self._latest = None
        return self._latest

    # --- the candidates ---------------------------------------------------------------------------------------
    def _signals(self, since: datetime) -> list[dict[str, Any]]:
        from .livenews import iso, item_row, signal_row
        rows = [signal_row(r) for r in self.svc.db.query(
            "SELECT * FROM live_signals WHERE state IN ('opportunity', 'done') AND direction IS NOT NULL "
            "AND opportunity_at >= ? ORDER BY id DESC LIMIT 200", (iso(since),))]
        ids = sorted({s["news_id"] for s in rows})
        heads = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.svc.db.query(f"SELECT * FROM live_news WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                heads[r["id"]] = item_row(r)
        for s in rows:
            s["item"] = heads.get(s["news_id"]) or {}
        # One per stock: the most recent confirmation.
        seen, out = set(), []
        for s in rows:
            if s["symbol"] in seen:
                continue
            seen.add(s["symbol"])
            out.append(s)
        return out

    def _movers(self, since: datetime, taken: set[str]) -> list[dict[str, Any]]:
        out = []
        for m in (self.feed.movers() if self.feed is not None else []):
            when = _parse(m.get("updated_at"))
            if not m.get("found") or m["symbol"] in taken or when is None or when < since:
                continue
            out.append(m)
        return out

    def _quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        market = self.svc.market
        for i in range(0, len(symbols), 50):
            chunk = symbols[i:i + 50]
            try:
                out.update(market._call("digest quotes", lambda c=chunk: market.provider.quotes(c)))
            except Exception as exc:                  # noqa: BLE001 - a stock without a quote is left out
                log.info("Digest quotes failed: %s", exc)
        return out

    # --- the digest -------------------------------------------------------------------------------------------
    def build(self, choice: str = "15") -> dict[str, Any]:
        now = datetime.now(NY)
        since, words = window_for(choice, now)
        notes: list[str] = []
        phase = marketdays.phase(now)
        clock = (self.svc.market.clock() or {}).get("state")
        in_session = clock == "open"
        signals = self._signals(since)
        movers = self._movers(since, {s["symbol"] for s in signals})
        symbols = list(dict.fromkeys([s["symbol"] for s in signals] + [m["symbol"] for m in movers]))
        quotes = self._quotes(symbols) if symbols else {}
        minute = max(5, min(390, int((now - now.replace(hour=9, minute=30, second=0, microsecond=0)).total_seconds() // 60)))
        cases = None
        if in_session and symbols:
            pool = list(dict.fromkeys(symbols + self._morning_movers()))[:30]
            bars5, atr_of = {}, {}
            for sym in pool:
                try:
                    df = self.morning._intraday_store().bars(sym, "5m", 30)
                    from .morning import features_of
                    f = features_of(self.svc.market.bars(sym, 0.5, complete=True))
                except Exception as exc:              # noqa: BLE001 - left out of the record
                    log.info("Digest: no bars for %s: %s", sym, exc)
                    continue
                if df is not None and not df.empty and f is not None:
                    bars5[sym], atr_of[sym] = df, atr_by_day_from_frame(f)
            cases = intraday_cases(bars5, atr_of, minute=minute, skip_day=now.date()) if bars5 else None
        elif symbols:
            notes.append("Outside the session there is no record for news reactions, so no edge is assumed.")
        cpct = cost_pct(self.svc.settings)
        opps: list[dict[str, Any]] = []
        window_seconds = max((now - since).total_seconds(), 60.0)
        for s in signals:
            try:
                opps.append(self._from_signal(s, quotes.get(s["symbol"]) or {}, now, window_seconds, cases, cpct, minute))
            except (ValueError, KeyError, TypeError) as exc:
                log.info("Digest: %s left out: %s", s.get("symbol"), exc)
        for m in movers:
            try:
                opps.append(self._from_mover(m, quotes.get(m["symbol"]) or {}, now, window_seconds, cases, cpct, minute))
            except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
                log.info("Digest: mover %s left out: %s", m.get("symbol"), exc)
        opps.sort(key=lambda o: (-o["score"], o["symbol"]))
        for i, o in enumerate(opps, 1):
            o["rank"] = i
        cards = []
        for o in opps[:DIGEST_CARDS]:
            row = {"symbol": o["symbol"], "atr": o["atr"], "reference": o.get("reference") or o["entry"]}
            card = self.morning._card_for(o, row, "day")
            if card is None:
                continue
            card["key"] = f"{o['symbol']}:news:{'L' if o['direction'] == 'LONG' else 'S'}"
            card["score"], card["opp_key"], card["setup_label"], card["edge"] = o["score"], o["key"], o["setup_label"], o["edge"]
            card["news"] = {"state": "news", "label": o.get("headline") or "News", "items": []}
            o["card_key"], o["option"] = card["key"], option_of(card)
            cards.append(card)
        if not opps:
            notes.append(f"Nothing confirmed in {words}." + ("" if self.feed is not None and self.feed.running
                                                              else " Live news is not listening: press Start listening."))
        out = {"interval": str(choice), "window": words, "window_start": since.isoformat(timespec="seconds"),
               "built_at": now.isoformat(timespec="seconds"), "phase": phase, "opportunities": opps, "cards": cards,
               "notes": notes, "count": len(opps), "data_source": self.svc.market.info.mode,
               "record": ({"stocks": cases["stocks"], "minute": cases["minute"], "cases": int(len(cases["move"]))}
                          if cases else None)}
        if opps:
            run_cards = {c["key"]: c for c in cards}
            out["run_id"] = self.runs.save("live", [{**o, "card": run_cards.get(o.get("card_key"))} for o in opps[:DIGEST_KEEP]],
                                           {"label": f"News digest {now:%b %d %H:%M} ({words})", "interval": str(choice)})
        try:
            from .service import clean
            self.svc.db.cache_put(self._key(), clean(out))
        except Exception as exc:                      # noqa: BLE001
            log.warning("Digest not saved: %s", exc)
        self._latest = out
        return out

    def _morning_movers(self) -> list[str]:
        r = (self.morning.status(brief=False) or {}).get("result") if self.morning is not None else None
        if not r:
            return []
        return [o["symbol"] for o in (r.get("opportunities") or [])[:30]]

    # --- one opportunity --------------------------------------------------------------------------------------
    def _edge(self, d: int, q: dict[str, Any], atr: float, cases: dict[str, Any] | None, cpct: float) -> dict[str, Any]:
        op, last = q.get("open"), q.get("last")
        if cases is None or not len(cases["move"]) or not (op and last and atr):
            return edge_from(None, 0, cost_pct=cpct, basis="No rest-of-the-day record for this moment (outside the session, "
                                                          "or no bars within the day could be read)")
        so = (float(last) - float(op)) / atr
        sd = 1 if so >= 0 else -1
        size = min(2.5, max(0.5, int(abs(so) * 2) / 2))
        rec = intraday_record_from(cases, min_ranges=size, move_direction=sd, trade_direction=d)
        return edge_from(rec.get("mean_move_pct"), rec.get("cases"), cost_pct=cpct, frequency=rec.get("frequency"),
                         low=rec.get("low"), high=rec.get("high"),
                         basis=f"5-minute bars of {cases['stocks']} large stocks over the last 30 days: after {rec['what']}, "
                               "the trade's way")

    @staticmethod
    def _common_parts(d: int, q: dict[str, Any], age_s: float, window_s: float, edge: dict[str, Any],
                      minute: int) -> list[dict[str, Any]]:
        parts = [part("recency", "Recency", float(np.clip(1 - age_s / window_s, 0, 1)) * 10, 10,
                      f"{age_s / 60:.0f} minutes ago")]
        vol, avg = q.get("volume"), q.get("average_volume")
        if vol and avg and avg > 0:
            pace = (float(vol) / float(avg)) / max(minute / 390.0, 0.1)
            parts.append(part("volume", "Volume", float(np.clip((pace - 1) / 2, 0, 1)) * 10, 10,
                              f"trading at {pace:.1f}× its normal pace for this time of day"))
        if edge["known"]:
            parts.append(part("edge", "Past record", float(np.clip(edge["expected_pct"] / 0.5, -1, 1)) * 20, 20,
                              f"{edge['expected_pct']:+.2f}% expected after costs and the pull toward zero ({edge['cases']} cases)"))
        else:
            parts.append(part("edge", "Past record", 0, 20, "no record"))
        return parts

    def _from_signal(self, s: dict[str, Any], q: dict[str, Any], now: datetime, window_s: float,
                     cases: dict[str, Any] | None, cpct: float, minute: int) -> dict[str, Any]:
        d = 1 if s["direction"] == "LONG" else -1
        atr = float(s["atr"])
        price = float(q.get("last") or s.get("last_price") or s["ref_price"])
        r_ranges = (price - float(s["ref_price"])) / atr
        item = s.get("item") or {}
        delay = item.get("delay_seconds")
        tone = item.get("tone")
        edge = self._edge(d, q, atr, cases, cpct)
        at = _parse(s.get("opportunity_at")) or now
        parts = [part("reaction", "Reaction to the news", min(abs(r_ranges) / 2.0, 1.0) * 30, 30,
                      f"{r_ranges:+.2f} daily ranges since the news"),
                 part("speed", "Speed", 10 if delay is not None and delay <= 60 else 5 if delay is not None and delay <= 300 else 0, 10,
                      "received within a minute of publishing" if delay is not None and delay <= 60 else
                      f"received {delay / 60:.0f} minutes after publishing" if delay is not None else "publishing time unknown")]
        if tone is not None:
            agree = float(tone) * d
            parts.append(part("tone", "Headline tone", 10 if agree > 0.2 else -10 if agree < -0.2 else 0, 10,
                              "the tone agrees with the move" if agree > 0.2 else "the tone argues with the move"
                              if agree < -0.2 else "a neutral tone"))
        parts.append(part("in_play", "Still in play", 10 if s["state"] == "opportunity" else 0, 10,
                          "inside its first hour" if s["state"] == "opportunity" else "past its first hour"))
        parts += self._common_parts(d, q, (now - at).total_seconds(), window_s, edge, minute)
        stop, t1, t2, why = day_levels(entry=price, atr=atr, direction=d, setup="news")
        return make(source="live", key=f"live:{s['id']}:{'L' if d > 0 else 'S'}", symbol=s["symbol"], direction=s["direction"],
                    setup="news", setup_label=f"{'Buy' if d > 0 else 'Short'} on the news", horizon="day", horizon_days=1,
                    entry=price, stop=stop, target_1=t1, target_2=t2, atr=atr, score=total_score(parts), score_parts=parts,
                    edge=edge, why=[x for x in [s.get("reason")] if x], price=price, reference=float(s.get("prev_close") or price),
                    headline=item.get("headline"), url=item.get("url"), news_source=item.get("source"), signal_id=s["id"],
                    signal_state=s["state"], opportunity_at=s.get("opportunity_at"), levels_why=why, tradable=True,
                    move_pct=100 * (price / float(s["prev_close"]) - 1) if s.get("prev_close") else None, card_key=None)

    def _from_mover(self, m: dict[str, Any], q: dict[str, Any], now: datetime, window_s: float,
                    cases: dict[str, Any] | None, cpct: float, minute: int) -> dict[str, Any]:
        d = 1 if m["move_pct"] > 0 else -1
        atr = abs(float(m["price"]) - float(m["prev_close"])) / abs(float(m["ranges"]))
        price = float(q.get("last") or m["price"])
        day_ranges = (price - float(m["prev_close"])) / atr
        edge = self._edge(d, q, atr, cases, cpct)
        at = _parse(m.get("updated_at")) or now
        parts = [part("reaction", "The day's move", min(abs(day_ranges) / 3.0, 1.0) * 30, 30,
                      f"{day_ranges:+.2f} daily ranges since the close, with news found behind it"),
                 part("speed", "Speed", 0, 10, "found by searching after the move (search comes after the news)")]
        parts += self._common_parts(d, q, (now - at).total_seconds(), window_s, edge, minute)
        stop, t1, t2, why = day_levels(entry=price, atr=atr, direction=d, setup="news")
        return make(source="live", key=f"live:mover:{m['symbol']}:{now.date()}:{'L' if d > 0 else 'S'}", symbol=m["symbol"],
                    direction="LONG" if d > 0 else "SHORT", setup="news", setup_label=f"{'Buy' if d > 0 else 'Short'}: moving on news found",
                    horizon="day", horizon_days=1, entry=price, stop=stop, target_1=t1, target_2=t2, atr=atr,
                    score=total_score(parts), score_parts=parts, edge=edge, why=[f"News found: {m.get('headline')}"] if m.get("headline") else [],
                    price=price, reference=float(m["prev_close"]), headline=m.get("headline"), url=m.get("url"),
                    news_source=m.get("via"), levels_why=why, tradable=True, move_pct=100 * (price / float(m["prev_close"]) - 1),
                    card_key=None)


__all__ = ["NewsDigest", "window_for", "CHOICES"]
