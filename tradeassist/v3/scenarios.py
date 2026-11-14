"""Catalyst Scenarios (4.1): Central, Bullish and Bearish reactions to one event, inside a saved Research study.

The workflow, in the study's Forecast section:

1. **Export evidence.** A *seed* freezes what the study knew into a small, shareable packet: the anchor price and when
   it was read, the exact exchange sessions of the forecast, the catalyst (real or hypothetical) and the session its
   reaction lands on, the base forecast, the comparable situations and their sample sizes, the factor fit and the
   volatility inputs, and the sources with stable ids (S1, S2 ...). It holds nothing about the account: no broker
   token, balance, order, position or budget. The packet is downloaded as Markdown for a person to upload (to the
   MiroFish dashboard, say), and identical evidence gives the same seed again.
2. **Import cases.** The provider's answer is a fixed JSON schema (contracts.ScenarioImport): for each case its
   trigger, the channels that could move the stock, what may be priced in, the cited source ids and what would
   invalidate it. It is validated whole, against the seed: the symbol, horizon, sessions and data mode, unique case
   keys, known factor names and units, sessions inside the forecast, citations that exist. A claim without a citation
   is kept, labelled a hypothesis. A result for another study, stock or horizon is refused, so a late answer cannot
   land on the wrong study. The provider never writes a price or a probability.
3. **Apply assumptions.** The sizes are a separate, reviewed object (contracts.CaseAssumptions): a stock-specific jump
   for the catalyst, factor shocks in the What-If factors' own units, and optional extra event uncertainty, each with
   its source. A *version* computes every case's path from the study's own base forecast:

       case_return_pct[t] = base_return_pct[t]
                          + (shocked_whatif_pct[t] - reference_whatif_pct[t])      the factor shocks, incremental
                          + issuer_residual_pct[t]                                 the jump, from its session on
       case_price[t]      = anchor_price * (1 + case_return_pct[t] / 100)

   The factor term reuses the What-If arithmetic (factors.terms_from_fit) on a fit frozen at the study's cutoff, so a
   sector shock counts only the part of its move the market does not already explain, and the reference (no further
   move in any factor) is priced by the same fit, so a zero shock gives the base exactly. The band carries the base's
   own log-return dispersion at each session, plus any extra event uncertainty in quadrature; it is the model-implied
   range, not a calibrated one, unless the base's calibration passed. Edits make a new version; none is changed.

Every path is in fixed coordinates: the seed's anchor price and its session dates. A chart that refreshes to a new
close, or a study reopened weeks later, draws the same points at the same dates. Nothing here places an order, and
nothing leaves the app: the packet goes where the user takes it.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse
from pydantic import Field, ValidationError

from .. import contracts as C
from .. import factors as Fx
from .. import forecast as F
from ..marketdays import is_trading_day, next_trading_day, previous_trading_day, session as market_session
from ..optionmath import NY
from ..providers.base import DataError
from ..sectors import SECTOR_FUNDS, sector_fund
from ..service import RequestError

SEED_VERSION = "catalyst-seed-1"
PROMPT_VERSION = "catalyst-prompt-1"
CALC_VERSION = "catalyst-calc-1"
SCENARIO_HORIZONS = (1, 5)          # the pilot's horizons, one per study (the plan's "one or five trading sessions")
MAX_RESULT_BYTES = 64_000
MAX_EXCERPT = 8_000
MAX_SOURCES = 6
FACTOR_LIMITS = {"%": 50.0, "bp": 200.0}
FACTOR_LIMIT_OVERRIDE = {"volatility": 300.0}      # the VIX can double in a day
IMPLIED_TOLERANCE = 0.01
MIN_RETURN_PCT = -95.0
Z80 = F.Z80
OFF = "Catalyst Scenarios are switched off (FEATURE_CATALYST_SCENARIOS=0 in .env)."

QUESTION = ("Use only the attached evidence available at its cutoff. Explore how investors could react to the stated catalyst "
            "over the study's trading sessions. Return Central, Bullish and Bearish cases. For each, state the observable "
            "trigger, timing, issuer and market channels, what could already be priced in, source IDs, and what would "
            "invalidate the case. Mark unsupported hypotheses and missing evidence. Use the output template in the packet. "
            "Leave the dollar paths and probabilities for Trade Assistant.")
OUTPUT_PROMPT = ("Convert the scenario report below into one JSON object that follows the template exactly, and nothing else. "
                 "Use only what the report says. Cite the packet's source IDs (S1, S2 ...) where the report relies on them; "
                 "leave source_ids empty for a claim it does not support, and list it under hypotheses. Do not add prices, "
                 "price targets, paths or probabilities. Keep each sentence short.")


class SeedIn(C.Strict):
    catalyst: C.CatalystSpec
    sources: list[C.SourceExcerptIn] = Field(default_factory=list, max_length=MAX_SOURCES)


class RunIn(C.Strict):
    provider: str = "manual"
    seed_id: str = Field(min_length=1, max_length=64)
    result: Any = None                 # the provider's JSON (an object, or the text of one), validated here
    report_excerpt: str | None = Field(default=None, max_length=MAX_EXCERPT)


# --- small helpers ----------------------------------------------------------------------------------------------------

def _finite(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _utc(ts: Any) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize(NY).tz_convert("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").isoformat(timespec="seconds").replace("+00:00", "Z")


def _ny_date(unix: float) -> date:
    return datetime.fromtimestamp(float(unix), timezone.utc).astimezone(NY).date()


def _close_at(day: date) -> str:
    s = market_session(day)
    close = s[1] if s else datetime.combine(day, datetime.min.time(), NY).replace(hour=16)
    return _iso(pd.Timestamp(close))


def study_frame(created_at: str, view: dict[str, Any]) -> dict[str, Any] | None:
    """A study's fixed coordinates: the anchor price (and when it was read) and its forecast sessions by date. Studies
    saved before 4.1 have no last bar date: it is the trading day before the first session, as the study built it."""
    last = _finite(view.get("last"))
    times = [t for t in view.get("future_times") or [] if _finite(t) is not None]
    if not last or last <= 0 or not times:
        return None
    days = [_ny_date(t) for t in times]
    bar = view.get("last_bar_date")
    bar_date = date.fromisoformat(str(bar)[:10]) if bar else previous_trading_day(days[0])
    return {"anchor": {"price": float(last), "at": _iso(_utc(created_at)), "basis": "study_quote", "bar_date": bar_date.isoformat()},
            "sessions": [{"index": i + 1, "date": d.isoformat(), "close_at": _close_at(d)} for i, d in enumerate(days)]}


def first_session(cat: C.CatalystSpec, sessions: list[dict[str, Any]]) -> tuple[int | None, str]:
    """The forecast session the catalyst's reaction first trades in, and how it was placed. Refused when it falls
    before the window (the anchor already has it) or after the horizon."""
    if not cat.event_date:
        return 1, ("No date for the catalyst: its reaction is placed on the first forecast session." if cat.hypothetical
                   else "The catalyst has no date: its reaction is placed on the first forecast session.")
    day = date.fromisoformat(cat.event_date)
    if cat.event_time == "after_close":
        react, how = next_trading_day(day), "after the close, so the next session"
    else:
        react = day if is_trading_day(day) else next_trading_day(day)
        how = {"before_open": "before the open, so that day's session", "during_session": "during the session, so that day's session",
               "unknown": "at an unknown time, assumed that day's session"}[cat.event_time]
        if not is_trading_day(day):
            how = "on a day without a session, so the next session"
    dates = [s["date"] for s in sessions]
    first, last = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
    if react < first:
        raise RequestError(f"The catalyst's first session ({react.isoformat()}) is before the study's first forecast session "
                           f"({dates[0]}): its reaction is already in the anchor price. Pick a later event, or run a new study.")
    if react > last:
        raise RequestError(f"The catalyst's first session ({react.isoformat()}) is after the study's horizon (last session "
                           f"{dates[-1]}). Run a study whose horizon reaches it.")
    i = dates.index(react.isoformat()) + 1
    return i, f"{cat.event_date} {how}: session {i} ({react.isoformat()})."


def base_dispersion(low_pct: list[float], high_pct: list[float]) -> list[float]:
    """The base forecast's log-return standard deviation at each session, read back from its band (forecast.forecast
    draws low and high at the median's log, minus and plus Z80 times it)."""
    return [(math.log1p(h / 100.0) - math.log1p(lo / 100.0)) / (2 * Z80) for lo, h in zip(low_pct, high_pct)]


def factor_context(svc: Any, symbol: str, bar_date: date) -> dict[str, Any]:
    """The What-If factors' joint fit on daily closes up to the study's last bar (none later), frozen for the seed.
    The foreign-peers basket is left out: it is a list the user names in the What-If widget, not part of a study."""
    rows = [dict(f) for f in Fx.FACTORS if f["key"] != "peers"]
    fund = sector_fund(symbol)
    for r in rows:
        if r["key"] == "sector":
            r["proxy"] = fund
            r["label"] = f"Sector fund ({fund}, {SECTOR_FUNDS.get(fund, '')})" if fund else "Sector fund (none known)"
    years = 1.3 + max(0, (datetime.now(NY).date() - bar_date).days) / 365.25
    cut = pd.Timestamp(bar_date)

    def bounded(sym: str) -> pd.DataFrame:
        b = svc.market.bars(sym, years, complete=True)
        return b[pd.to_datetime(b["date"]).dt.tz_localize(None).dt.normalize() <= cut]

    unavailable: list[dict[str, str]] = []
    try:
        stock = bounded(symbol)
    except DataError as exc:
        return {"basis": f"no daily bars for {symbol}: {exc}", "fit_days": 0, "rows": [], "reference": {},
                "unavailable": [{"key": r["key"], "label": r["label"], "reason": "no history for the stock"} for r in rows]}
    frames: dict[str, pd.DataFrame] = {}
    for r in rows:
        r.update(beta=None, correlation=None, beta_days=None)
        if not r.get("proxy"):
            unavailable.append({"key": r["key"], "label": r["label"], "reason": "no proxy fund for this stock"})
            continue
        try:
            px = bounded(r["proxy"])
        except DataError as exc:
            unavailable.append({"key": r["key"], "label": r["label"], "reason": f"no history for {r['proxy']}: {exc}"[:200]})
            continue
        b = Fx.beta_of(stock, px)
        if not b:
            unavailable.append({"key": r["key"], "label": r["label"], "reason": f"too few days shared with {r['proxy']}"})
            continue
        r.update(beta=b["beta"], correlation=b["correlation"], beta_days=b["days"])
        frames[r["key"]] = px
    returns = Fx.aligned_returns(stock, frames)
    if not returns:
        return {"basis": f"daily closes through {bar_date.isoformat()}: too few shared days for a joint fit", "fit_days": 0,
                "rows": [], "reference": {}, "unavailable": unavailable + [
                    {"key": r["key"], "label": r["label"], "reason": "too few shared days for the joint fit"} for r in rows if r["key"] in frames]}
    for k, n in (returns.get("dropped") or {}).items():
        r = next(x for x in rows if x["key"] == k)
        unavailable.append({"key": k, "label": r["label"], "reason": f"only {n} days shared with the stock: left out of the joint fit"})
    order = [r["key"] for r in rows if r["key"] in returns["proxies"]]
    fit = Fx.joint_fit(returns["stock"], returns["proxies"], order)
    import numpy as np
    sd_daily = {k: float(100.0 * np.std(v, ddof=1)) for k, v in returns["proxies"].items() if len(v) > 2}
    out_rows = []
    for r in rows:
        if r["key"] not in fit:
            continue
        f = fit[r["key"]]
        out_rows.append({"key": r["key"], "label": r["label"], "unit": r["unit"], "group": r["group"], "proxy": r["proxy"],
                         "inverse": bool(r.get("inverse")), "scale": r.get("scale"), "index_scale": r.get("index_scale"),
                         "note": r.get("note", ""), "beta": float(r["beta"]), "correlation": float(r["correlation"]),
                         "beta_days": int(r["beta_days"]), "beta_unique": float(f["beta_unique"]), "explained": float(f["explained"]),
                         "redundant": bool(f["redundant"]), "loadings": {k: float(v) for k, v in f["loadings"].items()},
                         "sd_daily_pct": sd_daily.get(r["key"])})
    return {"basis": f"daily closes through {bar_date.isoformat()} ({returns['days']} shared days), fitted in this order: "
                     + ", ".join(order), "fit_days": int(returns["days"]), "rows": out_rows,
            "reference": {}, "unavailable": unavailable,
            "note": "Each factor counts only the part of its move the factors above it do not already explain. The reference is "
                    "no factor shock from the cutoff; a shock's effect is its difference from the reference. A factor left "
                    "unshocked has no move of its own: it moves only as the shocked factors above it imply."}


def _fit_of(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    active = [{**{k: v for k, v in r.items() if v is not None}, "on": True} for r in rows]     # unset scales mean the default
    fit = {r["key"]: {"beta_unique": r["beta_unique"], "loadings": r["loadings"], "explained": r["explained"], "redundant": r["redundant"]}
           for r in rows}
    return active, fit


def factor_effect(fx: dict[str, Any], shocks: list[dict[str, Any]], horizon: int) -> dict[str, Any]:
    """The factor shocks' effect on the stock at each session, in percentage points: the What-If terms under the
    shocked values less the same terms under the reference. A shock lands on its session and stays (as in the What-If
    model, where overnight moves are priced at the open); the factors shocked by a session are residualised together."""
    rows = fx.get("rows") or []
    if not rows or not shocks:
        return {"pct": [0.0] * horizon, "terms": []}
    active, fit = _fit_of(rows)
    reference = dict(fx.get("reference") or {})
    ref_terms = {t["key"]: t for t in Fx.terms_from_fit(active, fit, reference, fx.get("fit_days"))}
    ref_total = float(sum(t["contribution_pct"] for t in ref_terms.values()))
    pct = []
    for t in range(1, horizon + 1):
        scen = dict(reference)
        for s in shocks:
            if s["session"] <= t:
                scen[s["key"]] = float(s["value"])
        pct.append(float(sum(x["contribution_pct"] for x in Fx.terms_from_fit(active, fit, scen, fx.get("fit_days"))) - ref_total))
    full = dict(reference)
    for s in shocks:
        full[s["key"]] = float(s["value"])
    sessions = {s["key"]: s["session"] for s in shocks}
    terms = []
    for t in Fx.terms_from_fit(active, fit, full, fx.get("fit_days")):
        ref = ref_terms.get(t["key"], {"contribution_pct": 0.0})
        delta = t["contribution_pct"] - ref["contribution_pct"]
        if t["key"] in sessions or abs(delta) > 1e-12:
            terms.append({"key": t["key"], "label": t["label"], "value": t["value"], "unit": t["unit"], "session": sessions.get(t["key"]),
                          "proxy_move_pct": t["proxy_move_pct"], "unique_move_pct": t["unique_move_pct"], "shared_pct": t["shared_pct"],
                          "beta_unique": t["beta_unique"], "contribution_pp": float(delta)})
    return {"pct": pct, "terms": terms}


def case_paths(seed: dict[str, Any], a: dict[str, Any]) -> dict[str, Any]:
    """One case's cumulative return, band and prices at each session, from the seed's base and the case's reviewed
    assumptions (a CaseAssumptions as a dict)."""
    h = seed["horizon_sessions"]
    base = seed["base"]
    bp = [float(x) for x in base["return_pct"]]
    blo, bhi = [float(x) for x in base["low_pct"]], [float(x) for x in base["high_pct"]]
    fe = factor_effect(seed.get("factors") or {}, a.get("factors") or [], h)
    iss = a.get("issuer")
    issuer = [float(iss["value_pct"]) if iss and t >= iss["session"] else 0.0 for t in range(1, h + 1)]
    disp = a.get("dispersion")
    extra = [float(disp["value_pct"]) / 100.0 if disp and t >= disp["session"] else 0.0 for t in range(1, h + 1)]
    ret, sd, lo, hi = [], [], [], []
    for t in range(h):
        r = bp[t] + fe["pct"][t] + issuer[t]
        if not math.isfinite(r) or r <= MIN_RETURN_PCT:
            raise RequestError(f"The {C.CASE_LABELS[a['key']].lower()} case's assumptions give a move of {r:+.1f}% by session "
                               f"{t + 1}: not a usable price path. Use smaller shocks.")
        # The base's band, carried in log space around the case's median (its own shape, so a zero shock is the base),
        # widened in quadrature by any extra event uncertainty assumed from its session.
        cb = math.log1p(bp[t] / 100.0)
        down, up = cb - math.log1p(blo[t] / 100.0), math.log1p(bhi[t] / 100.0) - cb
        if extra[t]:
            down, up = math.hypot(down, Z80 * extra[t]), math.hypot(up, Z80 * extra[t])
        c = math.log1p(r / 100.0)
        ret.append(r)
        sd.append((down + up) / (2 * Z80))
        lo.append(100.0 * (math.exp(c - down) - 1.0))
        hi.append(100.0 * (math.exp(c + up) - 1.0))
    anchor = float(seed["anchor"]["price"])
    price = lambda xs: [anchor * (1 + x / 100.0) for x in xs]       # noqa: E731
    if not all(math.isfinite(v) for v in ret + lo + hi + sd):
        raise RequestError("A case's path is not a finite number at every session; nothing was saved.")
    return {"return_pct": ret, "low_pct": lo, "high_pct": hi, "log_sd": sd, "price": price(ret), "low_price": price(lo), "high_price": price(hi),
            "attribution": {"factor_pct": fe["pct"], "issuer_pct": issuer, "extra_sd_pct": [100.0 * e for e in extra], "terms": fe["terms"]}}


RULE_TOLERANCE = 0.01
MIN_ANALOG_TAIL = 20                 # independent cases the analogs' tail needs before it sizes a move


def rule_value(seed: dict[str, Any], source: str, direction: str, factor: str | None = None) -> float | None:
    """The size a deterministic rule gives a move (4.3), signed by `direction` (up or down), or None when the seed does
    not carry what the rule needs. The one-click pipeline sizes cases only with these; a provider's citation alone
    never sizes anything.

    - options_implied_move: the move options price for the earnings release (percentage points);
    - analog_tail: the comparable situations' 90th (up) or 10th (down) percentile less their median, with at least
      20 independent cases;
    - implied_sensitivity: one standard deviation of the move options imply to the horizon;
    - factor_volatility: one standard deviation of the factor's own move over the horizon, from its daily moves up
      to the cutoff, in the factor's unit (% or bp)."""
    sign = 1.0 if direction == "up" else -1.0 if direction == "down" else 0.0
    if not sign:
        return None
    vol = seed.get("volatility") or {}
    if source == "options_implied_move":
        v = _finite(vol.get("event_move_pct"))
        return sign * v if v else None
    if source == "implied_sensitivity":
        v = _finite(vol.get("implied_sigma_pct"))
        return sign * v if v else None
    if source == "analog_tail":
        an = seed.get("analogs") or {}
        if (an.get("independent") or 0) < MIN_ANALOG_TAIL:
            return None
        med, lo, hi = (_finite(an.get(k)) for k in ("median_pct", "p10_pct", "p90_pct"))
        if med is None or lo is None or hi is None:
            return None
        v = (hi - med) if sign > 0 else (lo - med)
        return v if v * sign > 0 else None
    if source == "factor_volatility":
        row = next((r for r in (seed.get("factors") or {}).get("rows") or [] if r["key"] == factor), None)
        sd = _finite((row or {}).get("sd_daily_pct"))
        if not row or not sd:
            return None
        move = sd * math.sqrt(int(seed["horizon_sessions"]))                  # the proxy's move, percent
        conv = float(row.get("scale") or 1.0) if row["unit"] == "bp" else float(row.get("index_scale") or 1.0)
        return round(sign * move / conv, 4) if conv else None
    return None


def analog_source_ids(seed: dict[str, Any]) -> list[str]:
    return [s["id"] for s in seed.get("sources") or [] if s.get("kind") == "analog"][:1]


def markdown(seed: dict[str, Any], output_prompt: str, template: dict[str, Any]) -> str:
    """The packet a person uploads to a scenario provider. Plain text and figures; nothing about the account."""
    s, cat, b = seed, seed["catalyst"], seed["base"]
    when = (f"{cat['event_date']} ({cat['event_time'].replace('_', ' ')})" if cat.get("event_date") else "no date")
    lines = [f"# Catalyst scenario evidence: {s['symbol']}", "",
             f"Seed `{s['seed_id']}` · hash `{s['seed_hash'][:16]}` · schema {s['schema_version']} · data {s['data_source']} ({s['data_mode']})",
             f"Evidence cutoff (UTC): {s['cutoff']}", "",
             "## The question", "", s["question"], "",
             "## The catalyst", "",
             f"- {cat['title']} ({cat['kind'].replace('_', ' ')}{', hypothetical' if cat.get('hypothetical') else ''}), {when}.",
             *([f"- {cat['description']}"] if cat.get("description") else []),
             f"- {s['catalyst_timing_note']}", "",
             "## The stock and the horizon", "",
             f"- Anchor: ${s['anchor']['price']:.2f}, read {s['anchor']['at']} (last complete daily bar {s['anchor']['bar_date']}).",
             f"- {s['horizon_sessions']} trading session{'s' if s['horizon_sessions'] != 1 else ''}: "
             + ", ".join(f"{x['index']}: {x['date']}" for x in s["sessions"]) + ".", "",
             "## Trade Assistant's base forecast (computed by the app)", "",
             "- Median path, % from the anchor: " + ", ".join(f"{v:+.2f}" for v in b["return_pct"]) + ".",
             f"- Range at the horizon: {b['low_pct'][-1]:+.1f}% to {b['high_pct'][-1]:+.1f}% ({b['range_words']}).",
             f"- Calibration: {str(b['calibration_status']).replace('_', ' ')}. {b.get('calibration_text') or ''}".rstrip(), ""]
    an = s.get("analogs") or {}
    lines += ["## Comparable situations", ""]
    if an.get("independent"):
        lines.append(f"- {an['independent']} independent cases of {an.get('kind_label') or an.get('kind')} ({an.get('own', 0)} on "
                     f"{s['symbol']}, the rest on peers): median {an['median_pct']:+.2f}%, middle 80% {an['p10_pct']:+.1f}% to "
                     f"{an['p90_pct']:+.1f}% after {s['horizon_sessions']} sessions.")
    else:
        lines.append("- None with a finished forward window.")
    fx = s.get("factors") or {}
    lines += ["", "## Market, sector and macro factors", "", f"- Fit: {fx.get('basis', 'none')}."]
    for r in fx.get("rows") or []:
        lines.append(f"- `{r['key']}` {r['label']} (unit {r['unit']}): beta alone {r['beta']:+.2f} (correlation {r['correlation']:+.2f}); "
                     f"beta to its own part {r['beta_unique']:+.2f}, {100 * r['explained']:.0f}% explained by the factors above"
                     + (" (redundant)" if r["redundant"] else "") + ".")
    for u in fx.get("unavailable") or []:
        lines.append(f"- `{u['key']}` {u['label']}: unavailable ({u['reason']}).")
    v = s.get("volatility") or {}
    lines += ["", "## Options and volatility", ""]
    lines.append(f"- 30-day implied volatility: {100 * v['iv30']:.0f}%." if v.get("iv30") else "- 30-day implied volatility: not available.")
    if v.get("implied_sigma_pct"):
        lines.append(f"- Implied standard deviation to the horizon: {v['implied_sigma_pct']:.1f}%.")
    if v.get("event_move_pct"):
        lines.append(f"- Earnings in {v.get('event_in_days')} trading days; options price a move of ±{v['event_move_pct']:.1f}% for it.")
    lines.append(f"- Options data as of {v.get('as_of') or s['cutoff']}.")
    if s.get("strategy_context"):
        lines += ["", "## What the strategies being compared depend on", ""] + [f"- {x}" for x in s["strategy_context"]]
    lines += ["", "## Sources", ""]
    for src in s["sources"]:
        stamp = f"published {src['published_at']}, " if src.get("published_at") else ""
        lines.append(f"- **{src['id']}** {src['title']} ({src['kind']}; {stamp}available {src['available_at']}): {src['summary']}"
                     + (f" <{src['url']}>" if src.get("url") else ""))
    if s.get("missing"):
        lines += ["", "## Missing or not yet trusted", ""] + [f"- {m}" for m in s["missing"]]
    lines += ["", "## Output template", "", "Answer with one JSON object in this shape (three cases: central, bullish, bearish):", "",
              "```json", json.dumps(template, indent=2), "```", "",
              "Allowed values: `type` one of issuer_event, market, sector, macro, positioning, sentiment, other; `direction` up, "
              "down or flat; `timing` before_event, first_session_after_release, spread_over_horizon, by_horizon or unknown; "
              "`session` 1 to the horizon, optional; `factor` one of the factor keys above, for market, sector and macro drivers; "
              "`proposed_value` with `proposed_unit` (% or bp for a factor, pp for the issuer's own move) only when the evidence "
              "gives a size. No prices, price paths or probabilities.", "",
              "## If the report is text only", "", output_prompt, ""]
    return "\n".join(lines)


def template_for(seed: dict[str, Any]) -> dict[str, Any]:
    def case(key: str, direction: str) -> dict[str, Any]:
        return {"key": key, "trigger": "<the observable event that would start this case>", "stock_direction": direction,
                "drivers": [{"type": "issuer_event", "direction": direction if direction != "mixed" else "flat",
                             "timing": "first_session_after_release", "reason": "<why it could move the stock>", "source_ids": ["S1"]}],
                "priced_in": "<what may already be in the price>", "invalidation": "<what would show this case is wrong>",
                "missing_evidence": [], "hypotheses": []}
    return {"schema_version": C.SCENARIO_SCHEMA_VERSION, "seed_id": seed["seed_id"], "symbol": seed["symbol"],
            "horizon_sessions": seed["horizon_sessions"], "cases": [case("central", "flat"), case("bullish", "up"), case("bearish", "down")]}


def _issues(exc: ValidationError) -> list[str]:
    out = []
    for e in exc.errors()[:12]:
        where = ".".join(str(x) for x in e.get("loc", [])) or "result"
        out.append(f"{where}: {e.get('msg', 'invalid')}")
    return out


class Scenarios:
    def __init__(self, svc: Any):
        self.svc = svc

    # --- guards and reads --------------------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool((getattr(self.svc.settings, "features", None) or {}).get("catalyst_scenarios", True))

    def _on(self) -> None:
        if not self.enabled():
            raise RequestError(OFF)

    def _study(self, study_id: str) -> dict[str, Any]:
        r = self.svc.db.one("SELECT * FROM studies WHERE id = ?", (study_id,))
        if r is None:
            raise RequestError(f"No study {study_id}")
        if r["status"] not in ("complete", "partial") or not r["forecast_json"]:
            raise RequestError("Catalyst Scenarios need a finished study with a forecast: run the study first.")
        return {"row": r, "forecast": json.loads(r["forecast_json"]), "evidence": json.loads(r["evidence_json"]),
                "view": json.loads(r["view_json"] or "{}")}

    def _seed(self, seed_id: str) -> dict[str, Any]:
        r = self.svc.db.one("SELECT * FROM scenario_seeds WHERE id = ?", (seed_id,))
        if r is None:
            raise RequestError(f"No evidence packet {seed_id}")
        return json.loads(r["packet_json"])

    def _run_row(self, run_id: str):
        r = self.svc.db.one("SELECT * FROM scenario_runs WHERE id = ?", (run_id,))
        if r is None:
            raise RequestError(f"No scenario run {run_id}")
        return r

    # --- 1. the seed ---------------------------------------------------------------------------------------------------
    def create_seed(self, study_id: str, body: SeedIn, strategy_context: list[str] | None = None) -> dict[str, Any]:
        """`strategy_context` (4.3, the one-click pipeline): the kinds of strategy the study compares and what moves
        matter to them, in words; never the contracts, the quantities, the budget or the account."""
        self._on()
        st = self._study(study_id)
        row, fc, ev, view = st["row"], st["forecast"], st["evidence"], st["view"]
        h = int(row["horizon_days"])
        if h not in SCENARIO_HORIZONS:
            raise RequestError(f"Catalyst Scenarios use a {' or '.join(map(str, SCENARIO_HORIZONS))}-session study in this release; "
                               f"this one is {h} sessions. Run a study with one of those horizons.")
        if row["mode"] != self.svc.market.info.mode:
            raise RequestError(f"This study was made on {row['mode']} data and the app is on {self.svc.market.info.mode} data now: "
                               "switch back, or run a fresh study.")
        frame = study_frame(row["created_at"], view)
        if frame is None or len(frame["sessions"]) < h:
            raise RequestError("This study has no anchor price or forecast sessions saved; run a fresh study.")
        frame["sessions"] = frame["sessions"][:h]
        cutoff = _utc(row["created_at"])
        cat_session, timing_note = first_session(body.catalyst, frame["sessions"])
        # The sources: the study's evidence, then the user's excerpts, each with a stable id and its timestamps.
        sources: list[dict[str, Any]] = []
        for it in ev.get("items") or []:
            sources.append({"id": f"S{len(sources) + 1}", "title": it["label"], "kind": it["kind"], "summary": str(it["summary"])[:600],
                            "published_at": _iso(_utc(it["as_of"])) if it.get("as_of") else None, "available_at": _iso(cutoff),
                            "url": None, "origin": "study"})
        for ex in body.sources:
            try:
                pub = _utc(ex.published_at)
            except (ValueError, TypeError) as exc:
                raise RequestError(f"The source '{ex.title}' has a publication time that cannot be read: {ex.published_at}") from exc
            if pub > cutoff:
                raise RequestError(f"The source '{ex.title}' was published ({_iso(pub)}) after this study's evidence cutoff "
                                   f"({_iso(cutoff)}). Run a fresh study to use it.")
            sources.append({"id": f"S{len(sources) + 1}", "title": ex.title, "kind": "public source", "summary": ex.excerpt,
                            "published_at": _iso(pub), "available_at": _iso(pub), "url": ex.url, "origin": "user"})
        # The base, its dispersion, and the figures the provider may cite.
        b = fc["base"]
        cal = fc.get("calibration") or {}
        calibrated = cal.get("status") == "calibrated"
        base = {"model_version": fc.get("model_version"), "forecast_hash": C.digest(fc), "return_pct": b["path_pct"][:h],
                "low_pct": b["low_pct"][:h], "high_pct": b["high_pct"][:h],
                "log_sd": base_dispersion(b["low_pct"][:h], b["high_pct"][:h]), "calibration_status": cal.get("status"),
                "calibration_text": cal.get("text", ""), "calibrated": calibrated,
                "range_words": F.RANGE_WORDS["calibrated" if calibrated else "unvalidated"]}
        an = view.get("analogs") or {}
        hs = ((an.get("summary") or {}).get("by_horizon") or {}).get(str(h)) or {}
        analogs = {"kind": an.get("kind"), "kind_label": an.get("kind_label"), "independent": hs.get("independent"),
                   "own": (an.get("summary") or {}).get("own"), "median_pct": hs.get("median_pct"), "p10_pct": hs.get("p10_pct"),
                   "p90_pct": hs.get("p90_pct"), "up_frequency": hs.get("up_frequency")} if hs else {}
        opt = next((i for i in ev.get("items") or [] if i.get("key") == "options"), None) or {}
        od = opt.get("data") or {}
        event = od.get("event") or {}
        volatility = {"iv30": _finite(od.get("iv30")), "implied_sigma_pct": _finite(od.get("implied_sigma_pct")),
                      "event_move_pct": _finite(event.get("move_pct")), "event_in_days": event.get("in_days"),
                      "base_sigma": _finite((view.get("forecast") or {}).get("sigma")),
                      "band_k": _finite((view.get("forecast") or {}).get("band_k")), "as_of": _iso(cutoff) if opt else None}
        fx = factor_context(self.svc, row["symbol"], date.fromisoformat(frame["anchor"]["bar_date"]))
        missing = list(ev.get("missing") or [])
        missing += [f"Factor {u['label']}: {u['reason']}" for u in fx.get("unavailable") or []]
        if not volatility["event_move_pct"]:
            missing.append("An options-implied move for the catalyst (no earnings move priced inside the horizon).")
        cat = body.catalyst.model_dump()
        q = (f"Stock: {row['symbol']}. Catalyst: {cat['title']}"
             + (f" ({cat['event_date']}, {cat['event_time'].replace('_', ' ')})" if cat.get("event_date") else " (no date)")
             + f". Horizon: {h} trading session{'s' if h != 1 else ''} ({frame['sessions'][0]['date']} to {frame['sessions'][-1]['date']}). "
             + QUESTION)
        packet = {"schema_version": C.SCENARIO_SCHEMA_VERSION, "study_id": study_id, "study_fingerprint": row["fingerprint"],
                  "symbol": row["symbol"], "data_mode": row["mode"], "data_source": view.get("data_source") or row["mode"],
                  "cutoff": _iso(cutoff), "horizon_sessions": h, "anchor": frame["anchor"], "sessions": frame["sessions"],
                  "catalyst": cat, "catalyst_session": cat_session, "catalyst_timing_note": timing_note, "base": base,
                  "analogs": analogs, "factors": fx, "volatility": volatility, "sources": sources, "missing": missing, "question": q}
        if strategy_context:
            packet["strategy_context"] = [str(x)[:300] for x in strategy_context][:8]
        seed_hash = C.digest({"seed_version": SEED_VERSION, **packet})
        hit = self.svc.db.one("SELECT id FROM scenario_seeds WHERE study_id = ? AND seed_hash = ?", (study_id, seed_hash))
        if hit is not None:
            return self.seed_view(hit["id"], reused=True)
        seed_id = C.new_id("seed")
        now = C.utc_now()
        full = C.ScenarioSeed(seed_id=seed_id, seed_hash=seed_hash, created_at=now, **packet)       # strict: the shape is checked
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_seeds (id, study_id, mode, symbol, horizon_days, created_at, cutoff, schema_version, seed_hash,
                           packet_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (seed_id, study_id, row["mode"], row["symbol"], h, now, packet["cutoff"], C.SCENARIO_SCHEMA_VERSION, seed_hash,
                         full.model_dump_json()))
        self.svc.bus.publish("scenario.seeded", seed_id, study_id=study_id, symbol=row["symbol"])
        return self.seed_view(seed_id)

    def seed_view(self, seed_id: str, reused: bool = False) -> dict[str, Any]:
        self._on()
        seed = self._seed(seed_id)
        tpl = template_for(seed)
        return {"seed": seed, "reused": reused, "question": seed["question"], "output_prompt": OUTPUT_PROMPT, "template": tpl,
                "markdown_url": f"/v3/scenario-seeds/{seed_id}?format=markdown",
                "filename": f"{seed['symbol']}-catalyst-evidence-{seed_id[-8:]}.md"}

    def seed_markdown(self, seed_id: str) -> str:
        self._on()
        seed = self._seed(seed_id)
        return markdown(seed, OUTPUT_PROMPT, template_for(seed))

    # --- 2. the provider's cases -----------------------------------------------------------------------------------------
    def check_import(self, seed: dict[str, Any], raw: Any) -> C.ScenarioImport:
        """The provider's answer, checked whole against its seed. Every problem is listed at once."""
        if isinstance(raw, str):
            if len(raw.encode()) > MAX_RESULT_BYTES:
                raise RequestError(f"The result is larger than {MAX_RESULT_BYTES // 1000} kB; paste only the JSON object.")
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RequestError(f"The result is not JSON ({exc.msg} at line {exc.lineno}). Paste the JSON object, or fill in the form.") from exc
        if not isinstance(raw, dict):
            raise RequestError("The result must be one JSON object with schema_version, seed_id and cases.")
        if len(C.canonical(raw).encode()) > MAX_RESULT_BYTES:
            raise RequestError(f"The result is larger than {MAX_RESULT_BYTES // 1000} kB.")
        try:
            imp = C.ScenarioImport.model_validate(raw)
        except ValidationError as exc:
            raise RequestError("The cases were not saved: " + "; ".join(_issues(exc))) from exc
        issues: list[str] = []
        h = seed["horizon_sessions"]
        if imp.seed_id != seed["seed_id"]:
            issues.append(f"seed_id: this result is for {imp.seed_id}, not this study's packet {seed['seed_id']}")
        if imp.symbol is not None and imp.symbol.upper() != seed["symbol"]:
            issues.append(f"symbol: {imp.symbol} is not this study's stock {seed['symbol']}")
        if imp.horizon_sessions is not None and imp.horizon_sessions != h:
            issues.append(f"horizon_sessions: {imp.horizon_sessions}, but the study's horizon is {h}")
        if imp.data_mode is not None and imp.data_mode != seed["data_mode"]:
            issues.append(f"data_mode: {imp.data_mode}, but the packet is on {seed['data_mode']} data")
        if imp.sessions is not None and imp.sessions != [s["date"] for s in seed["sessions"]]:
            issues.append("sessions: the dates are not the study's forecast sessions")
        keys = [c.key for c in imp.cases]
        if len(set(keys)) != len(keys):
            issues.append("cases: each of central, bullish and bearish may appear once")
        known_sources = {s["id"] for s in seed["sources"]}
        known_factors = {f["key"]: f for f in Fx.FACTORS if f["key"] != "peers"}
        for c in imp.cases:
            for j, d in enumerate(c.drivers):
                where = f"cases.{c.key}.drivers.{j}"
                bad = [x for x in d.source_ids if x not in known_sources]
                if bad:
                    issues.append(f"{where}.source_ids: {', '.join(bad)} not in the packet (it has {', '.join(sorted(known_sources)) or 'none'})")
                if d.session is not None and d.session > h:
                    issues.append(f"{where}.session: {d.session} is after the horizon ({h} sessions)")
                if d.factor is not None:
                    if d.factor not in known_factors:
                        issues.append(f"{where}.factor: unknown factor '{d.factor}' (known: {', '.join(known_factors)})")
                    elif d.type not in ("market", "sector", "macro"):
                        issues.append(f"{where}.factor: a factor belongs to a market, sector or macro driver, not {d.type}")
                if (d.proposed_value is None) != (d.proposed_unit is None):
                    issues.append(f"{where}: proposed_value and proposed_unit go together")
                elif d.proposed_value is not None:
                    if d.type == "issuer_event" and d.proposed_unit != "pp":
                        issues.append(f"{where}.proposed_unit: an issuer move is in pp (percentage points), not {d.proposed_unit}")
                    elif d.type != "issuer_event":
                        f = known_factors.get(d.factor or "")
                        if f is None:
                            issues.append(f"{where}: a proposed size needs a known factor (or type issuer_event)")
                        elif d.proposed_unit != f["unit"]:
                            issues.append(f"{where}.proposed_unit: {f['key']} is in {f['unit']}, not {d.proposed_unit}")
        if issues:
            raise RequestError("The cases were not saved: " + "; ".join(issues))
        return imp

    def suggested(self, seed: dict[str, Any], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Starting assumptions for review: a provider's proposed size only where it cites the packet's evidence."""
        rows = {r["key"] for r in (seed.get("factors") or {}).get("rows") or []}
        h, ev_session = seed["horizon_sessions"], seed.get("catalyst_session") or 1
        out = []
        for c in cases:
            a: dict[str, Any] = {"key": c["key"], "visible": True, "issuer": None, "factors": [], "dispersion": None}
            for d in c["drivers"]:
                if d.get("proposed_value") is None or not d.get("source_ids"):
                    continue
                sess = min(h, d.get("session") or (ev_session if d["timing"] in ("first_session_after_release", "unknown") else 1))
                if d["type"] == "issuer_event" and a["issuer"] is None:
                    a["issuer"] = {"value_pct": d["proposed_value"], "session": sess, "source": "provider", "source_ids": d["source_ids"],
                                   "note": d["reason"][:300]}
                elif d.get("factor") in rows and all(f["key"] != d["factor"] for f in a["factors"]):
                    a["factors"].append({"key": d["factor"], "value": d["proposed_value"], "session": sess, "source": "provider",
                                         "source_ids": d["source_ids"], "note": d["reason"][:300]})
            out.append(a)
        return out

    @staticmethod
    def normalized(imp: C.ScenarioImport) -> list[dict[str, Any]]:
        """The cases in order (central, bullish, bearish), labelled, each driver marked supported when it cites."""
        order = {k: i for i, k in enumerate(C.CASE_KEYS)}
        cases = []
        for c in sorted(imp.cases, key=lambda x: order[x.key]):
            d = c.model_dump()
            d["label"] = C.CASE_LABELS[c.key]
            for drv in d["drivers"]:
                drv["supported"] = bool(drv["source_ids"])
            cases.append(d)
        return cases

    def import_run(self, study_id: str, body: RunIn) -> dict[str, Any]:
        self._on()
        if body.provider != "manual":
            raise RequestError("Only a manual import is available in this release (provider 'manual').")
        self._study(study_id)
        srow = self.svc.db.one("SELECT study_id FROM scenario_seeds WHERE id = ?", (body.seed_id,))
        if srow is None:
            raise RequestError(f"No evidence packet {body.seed_id}")
        if srow["study_id"] != study_id:
            raise RequestError(f"That evidence packet belongs to another study ({srow['study_id']}); its cases cannot be saved to "
                               f"{study_id}. Open that study, or export this one's evidence.")
        seed = self._seed(body.seed_id)
        imp = self.check_import(seed, body.result)
        cases = self.normalized(imp)
        response_hash = C.digest(imp.model_dump())
        excerpt = (body.report_excerpt or "").strip()[:MAX_EXCERPT] or None
        hit = self.svc.db.one("SELECT id FROM scenario_runs WHERE seed_id = ? AND provider = 'manual' AND response_hash = ? "
                              "AND IFNULL(report_excerpt, '') = ?", (body.seed_id, response_hash, excerpt or ""))
        if hit is not None:                                     # the same answer pasted twice (a double click): one run
            out = self.run(hit["id"])
            out["reused"] = True
            return out
        run_id = C.new_id("srun")
        now = C.utc_now()
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_runs (id, seed_id, study_id, provider, status, created_at, completed_at, schema_version,
                           prompt_version, model_version, config_json, external_json, response_hash, cases_json, report_excerpt)
                           VALUES (?, ?, ?, 'manual', 'complete', ?, ?, ?, ?, NULL, '{}', '{}', ?, ?, ?)""",
                        (run_id, body.seed_id, study_id, now, now, C.SCENARIO_SCHEMA_VERSION, PROMPT_VERSION, response_hash,
                         json.dumps(cases), excerpt))
        self.svc.bus.publish("scenario.imported", run_id, study_id=study_id, symbol=seed["symbol"], cases=len(cases))
        return self.run(run_id)

    def runs(self, study_id: str) -> dict[str, Any]:
        self._on()
        rows = self.svc.db.query("SELECT id, seed_id, provider, status, created_at, cases_json FROM scenario_runs WHERE study_id = ? "
                                 "ORDER BY created_at DESC, id DESC LIMIT 50", (study_id,))
        out = []
        for r in rows:
            v = self.svc.db.one("SELECT id, version, created_at FROM scenario_versions WHERE run_id = ? ORDER BY version DESC LIMIT 1", (r["id"],))
            out.append({"id": r["id"], "seed_id": r["seed_id"], "provider": r["provider"], "status": r["status"], "created_at": r["created_at"],
                        "cases": [c["key"] for c in json.loads(r["cases_json"] or "[]")], "latest_version": dict(v) if v else None})
        seeds = [dict(s) for s in self.svc.db.query("SELECT id, created_at, seed_hash FROM scenario_seeds WHERE study_id = ? "
                                                    "ORDER BY created_at DESC LIMIT 20", (study_id,))]
        return {"study_id": study_id, "runs": out, "seeds": seeds}

    def run(self, run_id: str) -> dict[str, Any]:
        self._on()
        r = self._run_row(run_id)
        seed = self._seed(r["seed_id"])
        cases = json.loads(r["cases_json"] or "[]")
        versions = [dict(v) for v in self.svc.db.query("SELECT id, version, parent_version, created_at FROM scenario_versions "
                                                       "WHERE run_id = ? ORDER BY version", (run_id,))]
        latest = self.version(versions[-1]["id"]) if versions else None
        return {"run": {"id": r["id"], "seed_id": r["seed_id"], "study_id": r["study_id"], "provider": r["provider"], "status": r["status"],
                        "created_at": r["created_at"], "completed_at": r["completed_at"], "schema_version": r["schema_version"],
                        "prompt_version": r["prompt_version"], "response_hash": r["response_hash"], "cases": cases,
                        "report_excerpt": r["report_excerpt"], "error": r["error"]},
                "seed": self._seed_summary(seed), "suggested": self.suggested(seed, cases), "versions": versions, "latest": latest}

    @staticmethod
    def _seed_summary(seed: dict[str, Any]) -> dict[str, Any]:
        fx = seed.get("factors") or {}
        return {k: seed[k] for k in ("seed_id", "seed_hash", "symbol", "data_mode", "cutoff", "horizon_sessions", "anchor", "sessions",
                                      "catalyst", "catalyst_session", "catalyst_timing_note", "sources", "missing", "volatility")} | {
            "factors": [{k: r[k] for k in ("key", "label", "unit", "beta_unique", "explained", "redundant", "note")} for r in fx.get("rows") or []],
            "factors_unavailable": fx.get("unavailable") or [], "factor_basis": fx.get("basis"),
            "base": {k: seed["base"][k] for k in ("return_pct", "low_pct", "high_pct", "calibrated", "calibration_status", "range_words")}}

    # --- 3. reviewed assumptions and their paths -------------------------------------------------------------------------
    def _check_source(self, where: str, src: str, ids: list[str], known: set[str], issues: list[str]) -> None:
        bad = [x for x in ids if x not in known]
        if bad:
            issues.append(f"{where}.source_ids: {', '.join(bad)} not in the packet")
        if src == "provider" and not ids:
            issues.append(f"{where}: a size from the provider needs the packet's source ids it rests on (or call it a user stress)")

    def _check_rule(self, seed: dict[str, Any], where: str, source: str, value: float, issues: list[str], factor: str | None = None) -> None:
        """A size credited to a deterministic rule must be the rule's own (4.3); any other size is a user stress."""
        if source not in ("analog_tail", "implied_sensitivity", "factor_volatility"):
            return
        want = rule_value(seed, source, "up" if value > 0 else "down", factor)
        if want is None:
            issues.append(f"{where}: the packet has nothing for the {source.replace('_', ' ')} rule; call it a user stress")
        elif abs(value - want) > RULE_TOLERANCE * max(1.0, abs(want)):
            issues.append(f"{where}: the {source.replace('_', ' ')} rule gives {want:+.2f}; another size is a user stress")

    def create_version(self, run_id: str, body: C.ScenarioVersionIn, origin: str = "manual") -> dict[str, Any]:
        """`origin` is "manual" (applied by a person) or "auto" (the one-click pipeline, 4.3)."""
        self._on()
        r = self._run_row(run_id)
        seed, cases, assumptions = self.checked(r, body)
        result = self.compute(seed, cases, assumptions)
        vid = C.new_id("sver")
        now = C.utc_now()
        with self.svc.db.tx() as cur:
            cur.execute("SELECT COALESCE(MAX(version), 0) FROM scenario_versions WHERE run_id = ?", (run_id,))
            n = int(cur.fetchone()[0]) + 1
            cur.execute("""INSERT INTO scenario_versions (id, run_id, version, parent_version, created_at, calc_version, assumptions_json,
                           result_json, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (vid, run_id, n, body.parent_version, now, CALC_VERSION, json.dumps(assumptions), json.dumps(result), origin))
        self.svc.bus.publish("scenario.applied", vid, run_id=run_id, study_id=r["study_id"], symbol=seed["symbol"], version=n, origin=origin)
        return self.version(vid)

    def checked(self, r: Any, body: C.ScenarioVersionIn) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """A run's seed, its cases by key and the full set of reviewed assumptions, checked whole (4.7: the same check for
        a saved version and for an unsaved preview). Raises with every problem listed."""
        if r["status"] != "complete":
            raise RequestError(f"This run is {r['status']}: only a complete run's cases can be applied.")
        seed = self._seed(r["seed_id"])
        cases = {c["key"]: c for c in json.loads(r["cases_json"] or "[]")}
        h = seed["horizon_sessions"]
        rows = {x["key"]: x for x in (seed.get("factors") or {}).get("rows") or []}
        known = {s["id"] for s in seed["sources"]}
        implied = _finite((seed.get("volatility") or {}).get("event_move_pct"))
        issues: list[str] = []
        seen: set[str] = set()
        for a in body.cases:
            w = f"cases.{a.key}"
            if a.key not in cases:
                issues.append(f"{w}: this run has no {a.key} case")
            if a.key in seen:
                issues.append(f"{w}: given twice")
            seen.add(a.key)
            if a.issuer is not None:
                i = a.issuer
                if i.session > h:
                    issues.append(f"{w}.issuer.session: {i.session} is after the horizon ({h} sessions)")
                if i.source == "options_implied_move":
                    if implied is None:
                        issues.append(f"{w}.issuer: the packet has no options-implied move; call it a user stress")
                    elif abs(abs(i.value_pct) - implied) > IMPLIED_TOLERANCE:
                        issues.append(f"{w}.issuer: the options-implied move is ±{implied:.2f}%; another size is a user stress")
                if i.source == "factor_volatility":
                    issues.append(f"{w}.issuer.source: a factor's volatility sizes a factor shock, not the stock's own move")
                self._check_rule(seed, f"{w}.issuer", i.source, i.value_pct, issues)
                self._check_source(f"{w}.issuer", i.source, i.source_ids, known, issues)
            fkeys = [f.key for f in a.factors]
            if len(set(fkeys)) != len(fkeys):
                issues.append(f"{w}.factors: each factor may be shocked once")
            for j, f in enumerate(a.factors):
                fw = f"{w}.factors.{j}"
                row = rows.get(f.key)
                if row is None:
                    known_keys = ", ".join(rows) or "none: the packet has no factor fit"
                    issues.append(f"{fw}.key: '{f.key}' is not a factor with a fit in this packet ({known_keys})")
                    continue
                limit = FACTOR_LIMIT_OVERRIDE.get(f.key, FACTOR_LIMITS.get(row["unit"], 50.0))
                if not math.isfinite(f.value) or abs(f.value) > limit:
                    issues.append(f"{fw}.value: {f.value} {row['unit']} is outside ±{limit:g} {row['unit']} for {row['label']}")
                if f.session > h:
                    issues.append(f"{fw}.session: {f.session} is after the horizon ({h} sessions)")
                if f.source in ("options_implied_move", "analog_tail", "implied_sensitivity"):
                    issues.append(f"{fw}.source: {f.source.replace('_', ' ')} sizes the stock's own move, not a factor's")
                else:
                    self._check_rule(seed, fw, f.source, f.value, issues, factor=f.key)
                self._check_source(fw, f.source, f.source_ids, known, issues)
            if a.dispersion is not None:
                d = a.dispersion
                if d.session > h:
                    issues.append(f"{w}.dispersion.session: {d.session} is after the horizon ({h} sessions)")
                if d.source == "options_implied_move" and implied is None:
                    issues.append(f"{w}.dispersion: the packet has no options-implied move; call it a user stress")
                if d.source in ("analog_tail", "factor_volatility"):
                    issues.append(f"{w}.dispersion.source: {d.source.replace('_', ' ')} sizes a move, not extra uncertainty")
                elif d.source == "implied_sensitivity":
                    self._check_rule(seed, f"{w}.dispersion", d.source, d.value_pct, issues)
                self._check_source(f"{w}.dispersion", d.source, d.source_ids, known, issues)
        if issues:
            raise RequestError("The assumptions were not applied: " + "; ".join(issues))
        given = {a.key: a.model_dump() for a in body.cases}
        assumptions = [given.get(k) or {"key": k, "visible": True, "issuer": None, "factors": [], "dispersion": None}
                       for k in C.CASE_KEYS if k in cases]
        return seed, cases, assumptions

    def compute(self, seed: dict[str, Any], cases: dict[str, dict[str, Any]], assumptions: list[dict[str, Any]]) -> dict[str, Any]:
        b = seed["base"]
        anchor = float(seed["anchor"]["price"])
        out_cases = []
        for a in assumptions:
            c = cases[a["key"]]
            p = case_paths(seed, a)
            quantified = bool(a.get("issuer") or a.get("factors") or a.get("dispersion"))
            vs_base = p["return_pct"][-1] - float(b["return_pct"][-1])
            warnings = []
            if a["key"] == "bearish" and quantified and vs_base > 0:
                warnings.append("This bearish case ends above the base forecast: check the signs of its assumptions.")
            if a["key"] == "bullish" and quantified and vs_base < 0:
                warnings.append("This bullish case ends below the base forecast: check the signs of its assumptions.")
            if not quantified:
                warnings.append("No quantified assumption: the case is a narrative and its path is the base forecast's.")
            unsupported = [d["reason"] for d in c["drivers"] if not d.get("supported")]
            out_cases.append({"key": a["key"], "label": C.CASE_LABELS[a["key"]], "visible": bool(a.get("visible", True)),
                              "direction": c["stock_direction"], "trigger": c["trigger"], "invalidation": c["invalidation"],
                              "quantified": quantified, **p,
                              "terminal": {"return_pct": p["return_pct"][-1], "price": p["price"][-1], "vs_base_pp": vs_base},
                              "hypotheses": list(c.get("hypotheses") or []) + unsupported, "warnings": warnings})
        price = lambda xs: [anchor * (1 + float(x) / 100.0) for x in xs]       # noqa: E731
        return {"study_id": seed["study_id"], "seed_id": seed["seed_id"], "symbol": seed["symbol"], "horizon_sessions": seed["horizon_sessions"],
                "anchor": seed["anchor"], "sessions": seed["sessions"], "catalyst": seed["catalyst"],
                "catalyst_session": seed.get("catalyst_session"),
                "base": {"label": "Base forecast", "return_pct": b["return_pct"], "low_pct": b["low_pct"], "high_pct": b["high_pct"],
                         "log_sd": b["log_sd"], "price": price(b["return_pct"]), "low_price": price(b["low_pct"]),
                         "high_price": price(b["high_pct"]), "calibrated": b["calibrated"], "range_words": b["range_words"]},
                "cases": out_cases,
                "band_note": (f"Each band is {b['range_words']}: the base's own dispersion at each session, plus any extra event "
                              "uncertainty assumed. It is not a probability for the case, and the cases have none."),
                "calc_version": CALC_VERSION}

    def version(self, version_id: str) -> dict[str, Any]:
        self._on()
        v = self.svc.db.one("SELECT * FROM scenario_versions WHERE id = ?", (version_id,))
        if v is None:
            raise RequestError(f"No scenario version {version_id}")
        res = json.loads(v["result_json"])
        last = date.fromisoformat(res["sessions"][-1]["date"])
        today = datetime.now(NY).date()
        return {"id": v["id"], "run_id": v["run_id"], "version": v["version"], "parent_version": v["parent_version"],
                "origin": v["origin"] if "origin" in v.keys() else "manual",
                "created_at": v["created_at"], "calc_version": v["calc_version"], "assumptions": json.loads(v["assumptions_json"]),
                **res, "stale": last < today,
                "stale_note": (f"Its last session ({last.isoformat()}) has passed: it is shown at its original dates and prices."
                               if last < today else None)}


def install(svc: Any) -> None:
    from ..api_v3 import register
    svc.scenarios = Scenarios(svc)

    def routes(router: APIRouter, svc: Any) -> None:
        s = svc.scenarios

        @router.post("/studies/{study_id}/scenario-seeds")
        def seed(study_id: str, body: SeedIn) -> dict[str, Any]:
            return s.create_seed(study_id, body)

        @router.get("/scenario-seeds/{seed_id}", response_model=None)
        def seed_read(seed_id: str, format: str = Query("json", pattern="^(json|markdown)$")):
            if format == "markdown":
                view = s.seed_view(seed_id)
                return PlainTextResponse(s.seed_markdown(seed_id), media_type="text/markdown; charset=utf-8",
                                         headers={"Content-Disposition": f'attachment; filename="{view["filename"]}"'})
            return s.seed_view(seed_id)

        @router.post("/studies/{study_id}/scenario-runs")
        def run_new(study_id: str, body: RunIn) -> dict[str, Any]:
            return s.import_run(study_id, body)

        @router.get("/studies/{study_id}/scenario-runs")
        def runs(study_id: str) -> dict[str, Any]:
            return s.runs(study_id)

        @router.get("/scenario-runs/{run_id}")
        def run_one(run_id: str) -> dict[str, Any]:
            return s.run(run_id)

        @router.post("/scenario-runs/{run_id}/versions")
        def version_new(run_id: str, body: C.ScenarioVersionIn) -> dict[str, Any]:
            return s.create_version(run_id, body)

        @router.get("/scenario-versions/{version_id}")
        def version_one(version_id: str) -> dict[str, Any]:
            return s.version(version_id)

    register(svc, routes)


__all__ = ["Scenarios", "SeedIn", "RunIn", "install", "study_frame", "first_session", "base_dispersion", "factor_context",
           "factor_effect", "case_paths", "markdown", "template_for", "SCENARIO_HORIZONS", "CALC_VERSION", "QUESTION"]
