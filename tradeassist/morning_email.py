"""The 7:00 morning email (2.8): the plan rendered as plain text and simple HTML.

Everything in it comes from morning.plan() and morning.scorecard(); nothing new is computed here. The
wording keeps the app's rules: setups and past frequencies, never forecasts, and no order is ever placed.
"""

from __future__ import annotations

import html as H
from datetime import datetime
from typing import Any

from .optionmath import NY

STYLE_BOX = ("border:1px solid #e3e7ec;border-radius:6px;padding:10px 12px;margin:8px 0;"
             "font-family:-apple-system,'Segoe UI',sans-serif;font-size:14px;line-height:1.45;")
STYLE_MUTED = "color:#566372;"


def money(v: float | None, d: int = 2) -> str:
    return "n/a" if v is None else f"${v:,.{d}f}"


def signed(v: float | None, d: int = 0) -> str:
    return "n/a" if v is None else f"{'+' if v > 0 else ''}{'-' if v < 0 else ''}${abs(v):,.{d}f}"


def _record_line(rec: dict[str, Any] | None, direction: str) -> str | None:
    h = (rec or {}).get("horizons") or {}
    pick = h.get("3") or h.get("5") or h.get("1")
    if not pick:
        return None
    days = next(k for k, v in h.items() if v is pick)
    word = "higher" if direction == "LONG" else "lower"
    few = "" if pick["enough"] else ", few"
    rng = (f" ({pick['cases']} past cases, {100 * pick['low']:.0f}–{100 * pick['high']:.0f}%{few})"
           if pick.get("low") is not None else f" ({pick['cases']} past cases{few})")
    return f"Past record: {100 * pick['frequency']:.0f}% {word} {days} days later{rng}. A frequency, not a forecast."


def _card_lines(c: dict[str, Any]) -> list[str]:
    opt, lines = c.get("option"), []
    news_label = (c.get("news") or {}).get("label")
    head = f"{c['symbol']}: {c['setup']} → {c['side_word']}" + (f" ({news_label})" if news_label else "")
    lines.append(head)
    if opt and (opt.get("limits") or {}).get("viable"):
        lm = opt["limits"]
        kind = "call" if opt["option_type"].upper() == "CALL" else "put"
        lines.append(f"Buy {opt['contracts']} {c['symbol']} {opt['expiration']} ${opt['strike']:g} {kind}{'s' if opt['contracts'] != 1 else ''} "
                     f"at {money(lm['suggested'])} (fair {money(lm['fair'])}; patient {money(lm['patient'])}; "
                     f"never above {money(lm['cap'])}, where target 1 only pays {lm['min_payoff']:g}× the risk).")
        lines.append(f"Stock plan: stop {money(c['stop'])}, target 1 {money(c['target_1'])} by {c['horizon_date']}. "
                     f"Cost {money(opt['cost'], 0)} (the most it can lose); at target 1 about {signed(opt['at_target_1'])}.")
    else:
        sh = c["shares"]
        why = next((n for n in c.get("notes") or [] if "midpoint" in n or "No contract" in n or "no listed" in n.lower()), "")
        lines.append(f"Shares: {sh['count'] or 1} at about {money(c['entry'])}, stop {money(c['stop'])}, "
                     f"target 1 {money(c['target_1'])} by {c['horizon_date']}."
                     + (f" ({why})" if why else ""))
    rec = _record_line(c.get("record"), c["direction"])
    if rec:
        lines.append(rec)
    for w in c.get("warnings") or []:
        lines.append(f"Caution: {w}")
    lines.append(f"Order: {c['order_text']}")
    return lines


def render(plan: dict[str, Any], score: dict[str, Any], *, app_url: str = "http://127.0.0.1:8000/#morning",
           ) -> tuple[str, str, str]:
    """(subject, plain text, HTML) for the plan. The same lines drive both bodies."""
    cards = plan.get("cards") or []
    pending = [r for r in plan.get("reversal") or [] if not r.get("confirmed") and r.get("trigger")]
    try:
        when = datetime.fromisoformat(plan.get("day", "")).strftime("%a %b %-d")
    except ValueError:
        when = datetime.now(NY).strftime("%a %b %-d")
    demo = plan.get("data_source") == "demo"
    subject = (f"Morning plan, {when}: {len(cards)} trade card{'s' if len(cards) != 1 else ''}, "
               f"{len(pending)} reversal trigger{'s' if len(pending) != 1 else ''}" + (" (demo)" if demo else ""))

    m = plan.get("market") or {}
    market_bits = [b for b in (
        m.get("regime"),
        (f"SPY {m['spy_move_pct']:+.1f}% ({m.get('spy_move_is')})" if m.get("spy_move_pct") is not None else None),
    ) if b]
    blocks: list[tuple[str, list[list[str]]]] = []
    if cards:
        blocks.append(("Trade cards", [_card_lines(c) for c in cards]))
    else:
        blocks.append(("Trade cards", [["No mover supports a setup this morning, and no reversal confirmed. "
                                        "Not trading is the plan."]]))
    skipped = [r for r in plan.get("movers") or [] if not r.get("card_key")]
    if skipped:
        blocks.append(("Moved, but no trade", [[
            f"{r['symbol']} {r['move_pct']:+.1f}% ({r['ranges']:.1f} ranges): {r['setup']['why']}"] for r in skipped[:6]]))
    if pending:
        blocks.append(("Reversal triggers to watch", [[
            f"{r['symbol']} {r['trigger_kind']} {money(r['trigger'])} ({r['side']} forming, score {r['score']:.0f}). "
            f"{'Calls or shares' if r['direction'] == 'LONG' else 'Puts or short'} if it triggers."] for r in pending[:6]]))
    graded = score.get("graded_3d", 0)
    if graded:
        lines = [f"{p['day']} {p['symbol']} ({p['setup']}, {p['direction'].lower()}): "
                 f"{p['after']['3']['move_pct']:+.1f}% after 3 closes"
                 for p in score.get("picks", []) if p.get("after", {}).get("3")][:6]
        lines.append(f"{score.get('profitable_3d', 0)} of the last {graded} picks were in profit after 3 closes "
                     "(the pick day's close is the first; graded by the stock plan — past option prices "
                     "are not available).")
        blocks.append(("Recent picks", [lines]))

    footer = (f"{plan.get('price_basis', '')} Setups and past frequencies, not forecasts; this app never places "
              f"orders. Open the Morning plan screen for live limit prices once the market opens: {app_url}")

    # Plain text.
    text_parts = [subject, "", " · ".join(market_bits)] if market_bits else [subject, ""]
    for title, groups in blocks:
        text_parts += ["", title.upper()]
        for g in groups:
            text_parts += [*g, ""]
    text_parts += [footer]
    text = "\n".join(text_parts).strip() + "\n"

    # HTML: the same lines, lightly boxed.
    h: list[str] = [f"<div style=\"max-width:680px;margin:0 auto;{STYLE_BOX}border:0\">"]
    if demo:
        h.append(f"<p style='{STYLE_MUTED}'><b>Demo data:</b> made-up prices; this says nothing about real stocks.</p>")
    if market_bits:
        h.append(f"<p>{H.escape(' · '.join(market_bits))}</p>")
    for title, groups in blocks:
        h.append(f"<h3 style='margin:16px 0 6px;font-size:15px'>{H.escape(title)}</h3>")
        for g in groups:
            parts = []
            for i, line in enumerate(g):
                body = f"<b>{H.escape(line)}</b>" if i == 0 and len(g) > 1 else H.escape(line)
                muted = f' style="{STYLE_MUTED}"' if line.startswith(("Past record", "Caution")) else ""
                parts.append(f"<div{muted}>{body}</div>")
            h.append(f'<div style="{STYLE_BOX}">{"".join(parts)}</div>')
    h.append(f"<p style='{STYLE_MUTED}'>{H.escape(footer)}</p></div>")
    return subject, text, "".join(h)


__all__ = ["render"]
