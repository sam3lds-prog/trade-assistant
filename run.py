#!/usr/bin/env python3
"""Start Trade Assistant on this computer: python run.py  (then open http://127.0.0.1:8000)."""

import argparse
import logging
from datetime import datetime

import uvicorn

from tradeassist.api import create_app
from tradeassist.config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade Assistant")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--snapshot", action="store_true",
                        help="Record today's implied volatility for the watchlist and exit (for a daily job)")
    parser.add_argument("--robinhood-check", nargs="?", const="AAPL", metavar="SYMBOL",
                        help="Sign in to Robinhood, test every data call the app makes, and write a capture file")
    parser.add_argument("--robinhood-signout", action="store_true",
                        help="Delete the Robinhood sign-in stored on this computer")
    parser.add_argument("--morning-email", action="store_true",
                        help="Build the morning plan and email it (for the 7:00 scheduled job), then exit")
    parser.add_argument("--morning-print", action="store_true",
                        help="Build the morning plan and print it instead of emailing (a dry run), then exit")
    parser.add_argument("--daemon", action="store_true",
                        help="Run the trading daemon: the Trading Desk's jobs, notifications and phone approvals (setup_daemon.sh keeps it running)")
    parser.add_argument("--scenario-check", nargs="?", const="SPY", metavar="SYMBOL",
                        help="read-only readiness check, nothing paid: Tradier data (a quote, daily bars, a chain, the clock), "
                             "MiroFish's connection and API (levels A and B), the settings, the circuit breaker, the spending "
                             "left, and whether a real end-to-end run has been checked; then exit")
    parser.add_argument("--scenario-smoke", nargs="?", const="SPY", metavar="SYMBOL",
                        help="4.5.2 functional test (level C), PAID: one real, bounded study through MiroFish end to end "
                             "(evidence, graph, agents, simulation, report, cases, paths, valuation, decision, usage record), "
                             "checked again after reopening the database. Asks before spending unless --yes")
    parser.add_argument("--profile", choices=("quick", "standard", "deep"), default="quick",
                        help="the simulation profile for --scenario-smoke (default quick)")
    parser.add_argument("--yes", action="store_true", help="with --scenario-smoke: do not ask before spending")
    parser.add_argument("--replay", nargs=argparse.REMAINDER, metavar="COMMAND",
                        help="4.0 replays: 'dataset build', 'dataset list', 'run', 'resume RUN', 'runs', 'report RUN' "
                             "(python run.py --replay --help for more)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from tradeassist.events import protect_logging
    # No credential reaches the log, from any module or from uvicorn, in a message or a traceback.
    protect_logging("uvicorn", "uvicorn.error", "uvicorn.access", "tradeassist")
    settings = load_settings()
    if args.replay is not None:
        from tradeassist.v4.cli import main as replay_main
        raise SystemExit(replay_main(settings, args.replay))
    if args.daemon:
        from tradeassist.daemon import run as run_daemon
        raise SystemExit(run_daemon(settings))
    if args.robinhood_signout:
        from tradeassist.providers.mcp_bridge import FileTokenStorage
        FileTokenStorage(settings.robinhood_signin_path).clear()
        print("Removed the Robinhood sign-in from this computer. To withdraw the permission at Robinhood too, "
              "disconnect Trade Assistant in your Robinhood settings.")
        return
    if args.robinhood_check:
        robinhood_check(settings, args.robinhood_check.upper())
        return
    if args.scenario_check:
        raise SystemExit(scenario_check(settings, args.scenario_check.upper()))
    if args.scenario_smoke:
        raise SystemExit(scenario_smoke(settings, args.scenario_smoke.upper(), args.profile, args.yes))
    if args.morning_email or args.morning_print:
        raise SystemExit(morning_email(settings, send=args.morning_email))
    if args.snapshot:
        from tradeassist.service import TradeAssistant
        result = TradeAssistant(settings).snapshot()
        if result.get("skipped"):
            print(f"{result['date']}: {result['skipped']}")
            return
        print(f"{result['date']}: saved {len(result['saved'])}, failed {len(result['failed'])}")
        for item in result["failed"]:
            print(f"  {item['symbol']}: {item['error']}")
        return
    # Localhost only: this app holds a brokerage data token and should not be reachable from the network.
    uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, log_level="info")


def scenario_check(settings, symbol: str) -> int:
    """The one-click Research readiness check, read-only and free: Tradier, then MiroFish at levels A (connection) and
    B (its API and storage), the settings, the circuit breaker, the spending left and the last functional test. Never
    prints a token and never starts a simulation. Exit code 0 when Tradier and the provider are ready."""
    from tradeassist.service import TradeAssistant
    from tradeassist.tradier_check import check_tradier
    from tradeassist.v3 import simulation as G
    from tradeassist.v3.scenario_providers import PINNED_UPSTREAM, provider_for
    svc = TradeAssistant(settings)
    print(f"Data mode: {settings.data_mode} ({svc.market.info.label}; {'real time' if svc.market.info.realtime else 'delayed or made up'})")
    t = check_tradier(svc, symbol)
    print(f"Tradier: {'OK' if t['ok'] else 'NOT READY'}: {t['message']}")
    for c in t.get("checks") or []:
        print(f"  {'ok ' if c['ok'] else 'FAIL'} {c['label']}: {c['text'] if c['ok'] else (c.get('error') or c['text'])}")
    prof = G.profile(settings, settings.scenario_default_profile)
    print(f"Scenario provider: {settings.scenario_provider} (automatic runs {'on' if settings.scenario_auto else 'off'} in .env; "
          "Research settings can change both)")
    try:
        prov = provider_for(svc, settings.scenario_provider, profile=prof["key"])
        r = G.readiness(svc, prov, getattr(prov, "name", settings.scenario_provider), prof["key"])
    except ValueError as exc:
        r = {"ok": False, "detail": str(exc), "state_label": "Configuration error", "levels": {}}
    print(f"  {'OK' if r['ok'] else 'NOT READY'}: {r.get('state_label') or ('Ready' if r['ok'] else 'Not ready')}: {r['detail']}")
    if r.get("action"):
        print(f"  What to do: {r['action']}")
    if settings.scenario_provider == "mirofish":
        lv = r.get("levels") or {}
        for key, words in (("A", "connection"), ("B", "API and storage")):
            x = lv.get(key)
            print(f"  Level {key}, {words}: " + ("not checked (level A failed)" if x is None else f"{'ok' if x.get('ok') else 'FAIL'}: {x.get('detail')}"))
        fn = r.get("functional") or {}
        print(f"  Level C, functional: {fn.get('words') or ('passed' if fn.get('ok') else 'failed')}"
              + (f" ({fn.get('at')})" if fn.get("at") else ""))
        print(f"  MIROFISH_URL {settings.mirofish_url or '(not set)'}; pinned upstream {PINNED_UPSTREAM[:7]}; {settings.mirofish_concurrency} at once")
        for p in G.profiles(settings):
            print(f"  Profile {p['label']}{' (default)' if p['key'] == prof['key'] else ''}: {p['words']}"
                  + ("; always asks first" if p["always_ask"] else ""))
        c = r.get("circuit") or {}
        print(f"  Circuit breaker: {c.get('state', 'closed')}, {c.get('failures', 0)} failure(s) in a row of {c.get('threshold')}"
              + (f"; last: {c.get('last_error')}" if c.get("last_error") else ""))
        b = r.get("budgets") or {}
        est = r.get("estimate") or {}
        print(f"  Next {prof['label']} run: {est.get('words')}; {r.get('admission', {}).get('reason', '')}")
        print(f"  Spending today: {G.money(b.get('today', {}).get('high_usd'))} at most (limit {G.money(b.get('daily_limit'))}); "
              f"this month: {G.money(b.get('month', {}).get('high_usd'))} at most (limit {G.money(b.get('monthly_limit'))}); "
              f"automatic approval up to {G.money(b.get('auto_approval_usd'))}")
    if settings.scenario_control:
        print(f"  Single-prompt control: {'AI set up' if settings.anthropic_api_key else 'ANTHROPIC_API_KEY is not set: the control will fail'}")
    budget = settings.scenario_budget or "the Trading Desk policy's trial capital"
    loss = settings.scenario_max_loss or "the Trading Desk policy's daily loss stop"
    print(f"Opportunity checks: budget {budget}; loss limit {loss}; minimum net {settings.opportunity_min_net_pct:g}%; "
          f"widest spread {settings.opportunity_max_spread_pct:g}%; minimum open interest {settings.opportunity_min_open_interest:g}; "
          f"notifications {'on' if settings.scenario_notify else 'off'}")
    return 0 if (t["ok"] or settings.data_mode == "demo") and r["ok"] else 1


def scenario_smoke(settings, symbol: str, profile: str, yes: bool, *, wait_seconds: float | None = None, ask=input) -> int:
    """Level C, the functional test, which costs model calls: one real, bounded study of SYMBOL through the self-hosted
    MiroFish end to end, then the database reopened and the result read back. Every stage is reported; the outcome
    is saved and shown by --scenario-check and in Research. Exit code 0 only when every stage passed."""
    import time
    from dataclasses import replace
    from datetime import datetime, timezone

    from tradeassist.service import TradeAssistant
    from tradeassist.tradier_check import check_tradier
    from tradeassist.v3 import install as install_v3
    from tradeassist.v3 import simulation as G
    from tradeassist.v3.research import StudyIn
    from tradeassist.v3.workers import workers_for
    if settings.scenario_provider != "mirofish" and not settings.mirofish_url:
        print("The functional test is for MiroFish: set MIROFISH_URL (and SCENARIO_PROVIDER=mirofish).")
        return 2
    run_settings = replace(settings, desk_worker="off", scenario_provider="mirofish", scenario_default_profile=profile)
    svc = TradeAssistant(run_settings)
    install_v3(svc)
    stages: list[dict[str, object]] = []
    started = time.time()

    def stage(name: str, ok: bool, words: str) -> bool:
        stages.append({"stage": name, "ok": ok, "words": words})
        print(f"  {'ok ' if ok else 'FAIL'} {name}: {words}")
        return ok

    def done(ok: bool, study_id: str | None = None, run_id: str | None = None) -> int:
        rec = {"ok": ok, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": symbol, "profile": profile,
               "study_id": study_id, "run_id": run_id, "seconds": round(time.time() - started, 1), "stages": stages,
               "words": (f"Passed {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC: {symbol}, {profile}, every stage" if ok else
                         f"Failed {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC at {next((s['stage'] for s in stages if not s['ok']), '?')}")}
        svc.db.cache_put(G.SMOKE_KEY, rec)
        print(f"Functional test {'PASSED' if ok else 'FAILED'} in {rec['seconds']:.0f} s.")
        return 0 if ok else 1

    print(f"Functional test (level C): {symbol}, {profile} profile. This sends a real study to MiroFish and costs model calls.")
    if settings.data_mode == "demo":
        stage("market data", False, "DATA_MODE=demo: the functional test needs real Tradier data (DATA_MODE=live or sandbox)")
        return done(False)
    t = check_tradier(svc, symbol)
    if not stage("Tradier", bool(t["ok"]), t["message"]):
        return done(False)
    prov = svc.pipeline.provider("mirofish", profile)
    r = G.readiness(svc, prov, "mirofish", profile)
    if not stage("MiroFish readiness", bool(r["ok"]), f"{r['state_label']}: {r['detail']}"):
        return done(False)
    est = r["estimate"]
    print(f"  Estimated cost: {est['words']}. {r['admission']['reason']}.")
    if r["admission"]["decision"] == "refused":
        stage("spending", False, r["admission"]["reason"])
        return done(False)
    if not yes and str(ask("Spend this on a real run? Type yes to go on: ")).strip().lower() != "yes":
        print("Not run.")
        return 3
    from tradeassist.v3.pipeline import SETTINGS_KEY
    before = svc.db.cache_get(SETTINGS_KEY)              # the test's profile is used for this study only, then put back
    svc.pipeline.save_settings(None, None, profile)
    try:
        st = svc.research3.start(StudyIn(symbol=symbol, horizon_days=1, scenarios=True, request_key=f"smoke-{int(started)}"))
    finally:
        svc.db.cache_put(SETTINGS_KEY, before or {})
    study_id = st["id"]
    end = time.time() + (wait_seconds if wait_seconds is not None else 20 * 60)
    while time.time() < end:                                    # the quantitative study, in its own thread
        s = svc.db.one("SELECT status FROM studies WHERE id = ?", (study_id,))
        if s and s["status"] != "running":
            break
        time.sleep(1.0)
    if not stage("evidence and strategies", bool(s and s["status"] in ("complete", "partial")), f"study {study_id}: {s['status'] if s else '?'}"):
        return done(False, study_id)
    rw = workers_for(svc)
    limit = G.profile(settings, profile)["minutes"] * 60 + 600 if wait_seconds is None else wait_seconds
    end, last = time.time() + limit, None
    approved = False
    while time.time() < end:
        rw.drain("scenarios")
        p = svc.pipeline.view(study_id) or {}
        run = p.get("run") or {}
        now = (p.get("status"), run.get("stage"), run.get("detail"))
        if now != last:
            print(f"    {p.get('status')}: {run.get('stage') or ''} {run.get('detail') or ''}".rstrip())
            last = now
        if p.get("status") == "awaiting_approval" and not approved:
            print(f"    Approving (you confirmed this test): {(p.get('governance') or {}).get('reason')}")
            svc.pipeline.approve(study_id)
            approved = True
            continue
        if p.get("status") in ("complete", "failed", "canceled", "needs_attention"):
            break
        time.sleep(3.0)
    p = svc.pipeline.view(study_id) or {}
    run = p.get("run") or {}
    if not stage("MiroFish simulation and report", bool(run.get("report_received")),
                 f"run {run.get('id')}: {run.get('status')}; ids {run.get('remote')}; {run.get('error') or ''}".strip()):
        return done(False, study_id, run.get("id"))
    v = p.get("version_id")
    stage("cases normalized and checked against the packet", bool(v), f"version {v}" if v else (p.get("error") or "no version"))
    val = p.get("valuation") or {}
    stage("price paths and strategy revaluation", val.get("status") in ("complete", "partial"),
          f"valuation {val.get('id')}: {val.get('status')}, {len(val.get('structures') or [])} structures")
    d = p.get("decision") or {}
    stage("opportunity decision", bool(d.get("status")), f"{d.get('label')}" if d else "none")
    u = p.get("usage") or {}
    stage("usage record", bool(u), f"{u.get('agents')} agents, {u.get('rounds')} rounds; cost {u.get('words')}" if u else "missing")
    again = TradeAssistant(run_settings)
    install_v3(again)
    p2 = again.pipeline.view(study_id) or {}
    stage("read back after reopening the database", p2.get("version_id") == v and (p2.get("valuation") or {}).get("id") == val.get("id"),
          f"status {p2.get('status')}, version {p2.get('version_id')}")
    ok = all(x["ok"] for x in stages)
    print(f"  Enter what your model provider charged for it, to calibrate the estimates: Research → Study settings → Simulation usage "
          f"(run {run.get('id')}).")
    return done(ok, study_id, run.get("id"))


def morning_email(settings, send: bool) -> int:
    """Build today's plan and email it (or print it with --morning-print). Exit code 0 on success and on a
    deliberate skip (weekend, market holiday); 1 when the plan or the email failed, so launchd logs it."""
    from tradeassist import mailer, morning_email as renderer
    from tradeassist.analysis import Analyst
    from tradeassist.ideas import SCAN_PACE
    from tradeassist.morning import Morning, record_picks, scorecard
    from tradeassist.optionmath import NY
    from tradeassist.service import TradeAssistant

    now = datetime.now(NY)
    if now.weekday() >= 5:
        print(f"{now:%A}: the market is closed, so no morning email.")
        return 0
    # A weekday holiday is NOT skipped: the plan still goes out, says the market is closed, and prices are
    # the last session's. Skipping on the clock alone would silently drop real mornings (the clock reads
    # "closed" before 7:00 New York time, so a 7:00 in a time zone east of New York would never send).
    svc = TradeAssistant(settings)
    if send and not mailer.configured(settings):
        print("Email is not set up: put SMTP_USER and SMTP_PASSWORD in .env (with Gmail, an app password from\n"
              "myaccount.google.com/apppasswords; 2-Step Verification must be on). Or run --morning-print.")
        return 1
    morning = Morning(svc, Analyst(svc), settings.features)
    print(f"Building the morning plan ({svc.market.info.label})…")
    try:
        with svc.market.paced(SCAN_PACE.get(svc.market.info.mode, 0.0)):
            plan = morning.plan(progress=_morning_progress)
    except Exception as exc:                          # noqa: BLE001 - the job must end with a plain line
        from tradeassist.events import redact
        reason = redact(str(exc))[:400]
        print(f"\nThe plan could not be built: {reason}")
        if send and mailer.configured(settings):
            # Silence at 7:00 looks like a quiet day. Say that the plan failed, and why, instead.
            try:
                mailer.send(settings, f"Morning plan failed \u2014 {now:%a %b %-d}",
                            f"The morning plan could not be built.\n\n{reason}\n\n"
                            "Nothing was scanned and nothing was recorded. Fix the cause and run\n"
                            "'.venv/bin/python run.py --morning-email' by hand, or wait for tomorrow's run.")
                print("A failure notice was emailed.")
            except mailer.MailError as mexc:
                print(f"The failure notice could not be emailed either: {mexc}")
        return 1
    print(f"\n{len(plan['movers'])} movers, {len(plan['cards'])} trade cards, "
          f"{len(plan['reversal'])} reversal rows, in {plan['seconds']:.0f}s.")
    morning.save_result(plan)                          # the screen shows this plan without a rescan
    subject, text, html = renderer.render(plan, scorecard(svc))
    if not send:
        # A dry run: nothing is sent and nothing is recorded as a pick.
        out = settings.db_path.parent / "morning.html"
        out.write_text(html)
        print("\n" + text)
        print(f"(The HTML version is at {out}.)")
        return 0
    try:
        to = mailer.send(settings, subject, text, html)
    except mailer.MailError as exc:
        print(f"\n{exc}")
        return 1
    record_picks(svc.db, plan, "email")                # picks count only once the plan reached you
    print(f"Sent to {to}: {subject}")
    return 0


def _morning_progress(done: int, total: int, current) -> None:
    if current and done % 25 == 0:
        print(f"  {done}/{total} {current}", flush=True)


def robinhood_check(settings, symbol: str) -> None:
    from dataclasses import replace

    from tradeassist.providers import make_provider
    from tradeassist.robinhood_check import run_check

    logging.getLogger().setLevel(logging.WARNING)
    print("Checking the Robinhood data connection. This app only reads market data: it never places, changes or\n"
          "cancels an order, and it does not read your positions or balances.\n\n"
          "If your browser opens, Robinhood's page will list what the sign-in permits. Robinhood decides that\n"
          "list, and it may cover more than market data. Read it before you approve.\n")
    provider = make_provider(replace(settings, data_mode="robinhood"), interactive=True)
    out = settings.db_path.parent / "robinhood_capture.json"
    try:
        result = run_check(provider, symbol, out, settings.risk_free_rate)
    finally:
        provider.bridge.close()
    for step in result["steps"]:
        mark = "OK    " if step["ok"] else ("skip  " if step.get("optional") else "FAILED")
        print(f"  {mark}  {step['step']}: {step['detail']}")
        for note in step.get("notes", []):
            print(f"          note: {note}")
    print(f"\nCapture written to {result['capture']}")
    if result["ok"]:
        print("Everything the app needs worked" + (", with the notes above" if result.get("noted") else "") +
              ". Compare the sample contract's bid, ask and delta with the Robinhood screen. If they match, "
              "set DATA_MODE=robinhood in .env and start the app with 'python run.py'.")
    else:
        print("Something did not match what the app expects. Send the capture file so the connection can be "
              "adjusted. It holds tool descriptions and sample market data with personal details filtered "
              "out; read it first to be sure.")
    print(f"Never send {settings.robinhood_signin_path.name}: that file is your sign-in.")


if __name__ == "__main__":
    main()
