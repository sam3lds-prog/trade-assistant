"""What the AI model is asked: market and stock briefs, headline tone, and reading a trade
description the rules could not.

Every prompt is built from figures the app has already worked out (the market's make-up,
indicator readings, the options lab, options activity) and from headlines. The prompt is
returned with each answer, so what was sent can be read on screen. Nothing about the
account (its size, positions or journal) is included.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from .ai import AIClient, AIError, _text, _texts, extract_json
from .optionmath import NY
from .providers.base import DataError

TONES = ("risk-on", "risk-off", "mixed")
SENTIMENTS = ("positive", "negative", "mixed", "unclear")


def _p(v: Any, d: int = 1, signed: bool = True) -> str:
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return "unknown"
    return f"{v:+.{d}f}%" if signed else f"{v:.{d}f}%"


def _n(v: Any, d: int = 2) -> str:
    return f"{v:,.{d}f}" if isinstance(v, (int, float)) and math.isfinite(v) else "unknown"


def _c(v: Any) -> str:
    """A count, or "unknown" when it was not measured."""
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else "unknown"


def _number(v: Any) -> float | None:
    """A finite number from whatever the model wrote, or None (booleans, huge values and text are not)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            return None
    if not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _has_summary(text: str) -> bool:
    j = extract_json(text)
    return isinstance(j, dict) and bool(_text(j.get("summary")))


def _scores(j: Any, count: int) -> dict[int, dict[str, Any]]:
    """Headline number -> tone (clamped to -1..+1) and reason, from the model's reply."""
    out: dict[int, dict[str, Any]] = {}
    rows = j.get("items") if isinstance(j, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        i, tone = _number(row.get("i")), _number(row.get("tone"))
        if i is None or tone is None or i != int(i) or not 1 <= i <= count:
            continue
        out[int(i)] = {"tone": max(-1.0, min(1.0, tone)), "why": _text(row.get("why"), 200)}
    return out


def _headlines(items: list[dict[str, Any]], limit: int = 12) -> str:
    rows = []
    for i, it in enumerate(items[:limit], 1):
        when = (it.get("published") or "")[:16].replace("T", " ")
        when = f"{when} UTC" if len(when) > 10 else when
        rows.append(f"{i}. [{it.get('source') or 'unknown'}{', ' + when if when else ''}] {it['headline']}")
    return "\n".join(rows) if rows else "(none available)"


def makeup_text(m: dict[str, Any]) -> str:
    lines = [f"New York time: {m['checked_at'][:16].replace('T', ' ')}. Data source: {m['data_source']}."]
    if m["data_source"] == "demo":
        lines.append("IMPORTANT: these prices are made-up demo data, not real market prices. Say so in the summary.")
    for i in m["indexes"]:
        lines.append(f"{i['symbol']} ({i['name']}) {_n(i['close'])}: day {_p(i['day_pct'])}, 5 days {_p(i['week_pct'])}, "
                     f"month {_p(i['month_pct'])}, 3 months {_p(i['quarter_pct'])}, {_p(i['vs_200d_pct'])} from its 200-day "
                     f"average{'' if i['bar_finished'] else ' (today, not yet closed)'}.")
    g = m.get("regime") or {}
    v = (g.get("volatility") or {})
    if g.get("regime"):
        lines.append(f"Market regime (this app's trend engine): {g['regime']}. {g.get('text') or ''}")
    if v.get("source") == "VIX" and v.get("vix") is not None:
        lines.append(f"VIX: {_n(v['vix'], 1)}.")
    elif v.get("spy_realized_20d") is not None:
        lines.append(f"VIX not available; SPY 20-day realized volatility {_p(100 * v['spy_realized_20d'], 0, False)}.")
    if m["sectors"]:
        lines.append("Sector funds over a month (and against SPY): " + "; ".join(
            f"{s['symbol']} {s['name']} {_p(s['month_pct'])} ({_p(s['month_vs_spy_pct'])}), day {_p(s['day_pct'])}"
            for s in m["sectors"]) + ".")
    b = m["breadth"]
    if b["stocks"]:
        lines.append(f"Breadth over {b['stocks']} {'large US companies' if b['universe'] == 'large_caps' else 'watchlist stocks'}: "
                     f"{_c(b['advancing'])} up, {_c(b['declining'])} down on the day; {_p(b['above_50d_pct'], 0, False)} "
                     f"above their 50-day average and {_p(b['above_200d_pct'], 0, False)} above their 200-day; "
                     f"{_c(b['new_highs'])} new 52-week highs and {_c(b['new_lows'])} new lows"
                     + (" (52-week range not measurable: too little history)." if b.get("new_highs") is None else "."))
    for o in m["options"]:
        lines.append(f"{o['symbol']} options (nearest expirations): put/call volume {_n(o['put_call_volume'])}, put/call "
                     f"open interest {_n(o['put_call_open_interest'])}.")
    return "\n".join(lines)


class Briefs:
    def __init__(self, ai: AIClient, svc: Any, news: Any, makeup: Any, lab: Any | None = None, analyst: Any | None = None):
        self.ai, self.svc, self.news, self.makeup, self.lab, self.analyst = ai, svc, news, makeup, lab, analyst

    def _finish(self, kind: str, answer: dict[str, Any], prompt: str, fields: dict[str, Any] | None,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"kind": kind, "brief": fields, "text": None if fields else answer["text"][:4000],
                "sources": answer["sources"][:12], "searches": answer["searches"], "usage": answer["usage"],
                "model": answer["model"], "web_search": answer["web_search"], "at": answer["at"],
                "cached": answer["cached"], "sent": prompt, **(extra or {}),
                "note": "Written by an AI model from the figures shown and the sources listed. It can be wrong: check "
                        "the sources. It is not advice, and options-implied figures are not forecasts."}

    # --- the market ---------------------------------------------------------------------------
    def market(self, *, web_search: bool = True, refresh: bool = False) -> dict[str, Any]:
        m = self.makeup.build()
        heads = self.news.market()["items"] if self.news else []
        prompt = (makeup_text(m) + "\n\nRecent market headlines:\n" + _headlines(heads) + "\n\n"
                  + ("Search the web for today's most important market news (economic data, central banks, big earnings, "
                     "large moves) from reputable outlets, and use it alongside the figures. If the figures and the news "
                     "disagree, say so.\n" if web_search else "Use only the figures and headlines above.\n")
                  + 'Write today\'s market brief. Reply with only a JSON object: {"headline": "one line", "summary": '
                    '"three to five sentences", "drivers": ["up to five things moving markets"], "risks": ["up to four"], '
                    '"watch": ["up to four upcoming events, with dates when known"], "tone": "risk-on, risk-off or mixed"}')
        hour = datetime.now(NY).strftime("%Y-%m-%d %H")
        answer = self.ai.ask(purpose="market_brief", prompt=prompt, web_search=web_search, max_tokens=1800,
                             cache_key=f"market|{hour}|{m['data_source']}", max_age=1800, refresh=refresh,
                             accept=_has_summary)
        j = extract_json(answer["text"])
        fields = None
        if isinstance(j, dict) and _text(j.get("summary")):
            fields = {"headline": _text(j.get("headline"), 200), "summary": _text(j.get("summary"), 2000),
                      "drivers": _texts(j.get("drivers"), 5), "risks": _texts(j.get("risks"), 4),
                      "watch": _texts(j.get("watch"), 4),
                      "tone": j.get("tone") if j.get("tone") in TONES else None}
        return self._finish("market", answer, prompt, fields, {"makeup_checked_at": m["checked_at"]})

    # --- one stock -----------------------------------------------------------------------------
    def _stock_context(self, symbol: str) -> tuple[str, dict[str, Any]]:
        from .scanner import flow_expirations, flow_summary, readings
        from .patterns import recent_events
        market = self.svc.market
        lines = [f"Stock: {symbol}. New York time: {datetime.now(NY).strftime('%Y-%m-%d %H:%M')}. "
                 f"Data source: {market.info.mode}."]
        if market.info.mode == "demo":
            lines.append("IMPORTANT: these prices are made-up demo data, not real prices. Say so in the summary.")
        ctx: dict[str, Any] = {}
        try:
            df = market.enriched(symbol, 2)
            r = readings(df)
            ctx["readings"] = r
            lines.append(f"Price {_n(r['close'])} ({_p(r['change_pct'])} on the day{'' if r['bar_finished'] else ', not yet closed'}); "
                         f"RSI {_n(r['rsi'], 0)}; MACD {'above' if r['macd'] == 'ABOVE_SIGNAL' else 'below'} its signal line; "
                         f"price {'above both' if r['averages'] == 'ABOVE_BOTH' else 'below both' if r['averages'] == 'BELOW_BOTH' else 'between'} "
                         f"its 50- and 200-day averages; {_p(r['range_52w_pct'], 0, False)} of the way up its 52-week range; "
                         f"volume {_n(r['volume_ratio'], 1)} times its 20-day average.")
            events = recent_events(df, lookback=10)
            if events:
                lines.append("Chart patterns in the last 10 days: " + "; ".join(
                    f"{e['label']} ({e['bars_ago']} bars ago)" for e in events[:6]) + ".")
        except (DataError, ValueError) as exc:
            lines.append(f"Price history unavailable ({exc}).")
        if self.analyst is not None:
            try:
                t = self.analyst.trend(symbol)
                day = t.get("provisional") or t.get("confirmed") or {}
                if day.get("state"):
                    lines.append(f"This app's trend engine: {day['state']} (net evidence score {_n(day.get('net_score'), 0)}; "
                                 "evidence points, not probabilities).")
            except Exception:                           # noqa: BLE001 - the brief is still useful without it
                pass
        if self.lab is not None:
            try:
                v = self.lab.volatility(symbol, 6)
                ctx["volatility"] = {k: v.get(k) for k in ("iv30", "iv_premium_points", "event", "earnings")}
                rv = v.get("realized") or {}
                front = next((x for x in v["expirations"] if x["dte"] >= 25), v["expirations"][-1])
                lines.append(f"Options: 30-day implied volatility {_p(100 * v['iv30'], 1, False) if v.get('iv30') else 'unknown'}, "
                             f"20-day realized {_p(100 * rv['hv20'], 1, False) if rv.get('hv20') else 'unknown'}; term structure "
                             f"{(v.get('shape') or {}).get('shape') or 'unknown'}; 25-delta skew "
                             f"{_n((v.get('skew') or {}).get('points'), 1)} points; expected move by {front['expiration']} "
                             f"about ±{_p(front['one_sd_pct'], 1, False)} (one standard deviation).")
                e, ev = v.get("earnings") or {}, v.get("event") or {}
                if e.get("date"):
                    lines.append(f"Next earnings: {e['date']}{' (estimated from past reports)' if e.get('estimated') else ''}"
                                 + (f"; options imply a move of about ±{ev['move_pct']:.1f}% on the report." if ev.get("available") else "."))
            except Exception:                           # noqa: BLE001
                pass
        try:
            picked = flow_expirations(market.expirations(symbol), datetime.now(NY).date(), 45, 2)
            if picked:
                f = flow_summary([self.svc._chain_for(symbol, e) for e in picked])
                ctx["flow"] = {k: f[k] for k in ("put_call_volume", "put_call_open_interest", "lean")}
                lines.append(f"Options activity (nearest expirations): put/call volume {_n(f['put_call_volume'])}, "
                             f"open interest {_n(f['put_call_open_interest'])}, "
                             f"{len(f['unusual'])} contracts trading at least their open interest.")
        except (DataError, ValueError):
            pass
        heads = self.news.company(symbol)["items"] if self.news else []
        ctx["headlines"] = len(heads)
        lines.append("Recent headlines and filings:\n" + _headlines(heads))
        return "\n".join(lines), ctx

    def symbol(self, symbol: str, *, web_search: bool = True, refresh: bool = False) -> dict[str, Any]:
        symbol = self.svc._symbol(symbol)
        context, ctx = self._stock_context(symbol)
        prompt = (context + "\n\n"
                  + (f"Search the web for the latest news on {symbol} (results, guidance, analyst moves, products, legal "
                     "or regulatory news) from reputable outlets. If you find discussion on social media or forums, you "
                     "may summarise it in the social field, clearly as unverified chatter.\n" if web_search else
                     "Use only the figures and headlines above.\n")
                  + f'Write a brief on {symbol}. Reply with only a JSON object: {{"headline": "one line", "summary": '
                    '"three to five sentences", "news": ["up to five recent items that matter"], "options_view": "what '
                    'the options figures imply, in one or two sentences", "bull_points": ["up to three"], "bear_points": '
                    '["up to three"], "events": ["upcoming dates that matter"], "social": "a sentence, or null", '
                    '"sentiment": "positive, negative, mixed or unclear"}')
        hour = datetime.now(NY).strftime("%Y-%m-%d %H")
        answer = self.ai.ask(purpose="stock_brief", prompt=prompt, web_search=web_search, max_tokens=1800,
                             cache_key=f"stock|{symbol}|{hour}|{self.svc.market.info.mode}", max_age=1800, refresh=refresh,
                             accept=_has_summary)
        j = extract_json(answer["text"])
        fields = None
        if isinstance(j, dict) and _text(j.get("summary")):
            fields = {"headline": _text(j.get("headline"), 200), "summary": _text(j.get("summary"), 2000),
                      "news": _texts(j.get("news"), 5), "options_view": _text(j.get("options_view"), 600),
                      "bull_points": _texts(j.get("bull_points"), 3), "bear_points": _texts(j.get("bear_points"), 3),
                      "events": _texts(j.get("events"), 4),
                      "social": _text(j.get("social"), 500) if j.get("social") not in (None, "null") else None,
                      "sentiment": j.get("sentiment") if j.get("sentiment") in SENTIMENTS else None}
        return self._finish("stock", answer, prompt, fields, {"symbol": symbol, "context": ctx})

    # --- headline tone -----------------------------------------------------------------------
    def headline_tone(self, symbol: str | None) -> dict[str, Any]:
        """A tone from -1 to +1 for each headline, with a reason. Filings are left out."""
        data = self.news.company(self.svc._symbol(symbol)) if symbol else self.news.market()
        items = [i for i in data["items"] if i["kind"] == "news"][:20]
        if not items:
            raise AIError("There are no headlines to read.")
        prompt = ("Rate the tone of each headline for the stock or market it is about, from -1 (clearly bad news) to +1 "
                  "(clearly good news), 0 for neutral or unclear. Judge only what the headline says.\n"
                  + (f"Stock: {data['symbol']}\n" if symbol else "Subject: the US stock market\n")
                  + _headlines(items, 20)
                  + '\nReply with only a JSON object: {"items": [{"i": 1, "tone": 0.5, "why": "a few words"}], '
                    '"summary": "one sentence on the overall tone"}')
        ids = ",".join(i["id"] for i in items)
        answer = self.ai.ask(purpose="headline_tone", prompt=prompt, max_tokens=1500, cache_key=f"tone|{ids}",
                             max_age=6 * 3600, accept=lambda t: bool(_scores(extract_json(t), len(items))))
        j = extract_json(answer["text"])
        by_i = _scores(j, len(items))
        scored = [{**it, "tone": by_i[n]["tone"], "why": by_i[n]["why"], "tone_method": "ai"}
                  for n, it in enumerate(items, 1) if n in by_i]
        if not scored:
            raise AIError("The model's answer could not be read as scores. Try again.")
        from .news import summarize_tone
        return {"symbol": data.get("symbol"), "items": scored, "tone": summarize_tone(scored),
                "summary": _text(j.get("summary"), 300) if isinstance(j, dict) else None,
                "model": answer["model"], "usage": answer["usage"],
                "cached": answer["cached"], "at": answer["at"],
                "note": "Tone of the headlines as an AI model reads them, not a forecast."}

    # --- reading a trade description ------------------------------------------------------------
    def read_strategy(self, text: str) -> dict[str, Any] | None:
        """The template and settings an AI model reads from a description, checked for type and range;
        None when it cannot tell. The strategy builder then checks it against the chain."""
        from .options.strategies import TEMPLATES
        if not self.ai.configured:
            return None
        menu = "\n".join(f"- {t.key}: {t.label}. {t.description}" for t in TEMPLATES.values())
        prompt = ("Map this options trade description to one of the strategy templates below. If it does not describe "
                  "one of them, use null for strategy. Do not guess a ticker that is not in the text, but map a company "
                  "name to its US ticker.\nTemplates:\n" + menu + "\n\nDescription: " + text[:500]
                  + '\nReply with only a JSON object: {"strategy": "key or null", "symbol": "ticker or null", "dte": '
                    'days to expiration or null, "delta": 0.16 or null, "width": dollars between strikes or null, '
                    '"contracts": number or null, "center": middle strike or null, "strikes": [numbers] or null, '
                    '"after_earnings": true or false}')
        try:
            answer = self.ai.ask(purpose="strategy_reader", prompt=prompt, max_tokens=300,
                                 cache_key=f"strategy|{text.strip().lower()}", max_age=86400,
                                 accept=lambda t: _usable_reading(extract_json(t), TEMPLATES))
            return _strategy_spec(extract_json(answer["text"]), TEMPLATES)
        except Exception:                               # noqa: BLE001 - an unreadable answer means "cannot tell"
            return None


def _usable_reading(j: Any, templates: dict[str, Any]) -> bool:
    """Worth keeping: a valid template, or a plain "cannot tell" (null)."""
    return isinstance(j, dict) and (j.get("strategy") is None or _strategy_spec(j, templates) is not None)


def _strategy_spec(j: Any, templates: dict[str, Any]) -> dict[str, Any] | None:
    """The settings the model gave, each checked for type and range; None unless it names a template."""
    if not isinstance(j, dict) or not isinstance(j.get("strategy"), str) or j["strategy"] not in templates:
        return None
    out: dict[str, Any] = {"strategy": j["strategy"]}
    sym = j.get("symbol")
    if isinstance(sym, str) and re.fullmatch(r"[A-Za-z]{1,5}(\.[A-Za-z])?", sym.strip()):
        out["symbol"] = sym.strip().upper()
    for name, lo, hi, whole in (("dte", 1, 1000, True), ("delta", 0.02, 0.98, False), ("width", 0.01, 10_000, False),
                                ("contracts", 1, 100, True), ("center", 0.01, 1_000_000, False)):
        v = _number(j.get(name))
        if v is None or not lo <= v <= hi or (whole and v != int(v)):
            continue
        out[name] = int(v) if whole else v
    ks = j.get("strikes")
    if isinstance(ks, list) and 1 <= len(ks) <= 4:
        vals = [_number(k) for k in ks]
        if all(v is not None and 0 < v < 1_000_000 for v in vals):
            out["strikes"] = vals
    if j.get("after_earnings") is True:
        out["after_earnings"] = True
    return out


__all__ = ["Briefs", "makeup_text"]
