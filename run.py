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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from tradeassist.events import protect_logging
    # No credential reaches the log, from any module or from uvicorn, in a message or a traceback.
    protect_logging("uvicorn", "uvicorn.error", "uvicorn.access", "tradeassist")
    settings = load_settings()
    if args.robinhood_signout:
        from tradeassist.providers.mcp_bridge import FileTokenStorage
        FileTokenStorage(settings.robinhood_signin_path).clear()
        print("Removed the Robinhood sign-in from this computer. To withdraw the permission at Robinhood too, "
              "disconnect Trade Assistant in your Robinhood settings.")
        return
    if args.robinhood_check:
        robinhood_check(settings, args.robinhood_check.upper())
        return
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
    svc = TradeAssistant(settings)
    if svc.market.info.mode != "demo" and svc.market.clock().get("state") == "closed":
        print("The market is closed today (a holiday), so no morning email.")
        return 0
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
        print(f"\nThe plan could not be built: {exc}")
        return 1
    print(f"\n{len(plan['movers'])} movers, {len(plan['cards'])} trade cards, "
          f"{len(plan['reversal'])} reversal rows, in {plan['seconds']:.0f}s.")
    record_picks(svc.db, plan, "email")
    subject, text, html = renderer.render(plan, scorecard(svc))
    if not send:
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
