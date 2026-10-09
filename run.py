#!/usr/bin/env python3
"""Start Trade Assistant on this computer: python run.py  (then open http://127.0.0.1:8000)."""

import argparse
import logging

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
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from tradeassist.events import RedactingFilter
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())          # no credential reaches the log, from any module
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
