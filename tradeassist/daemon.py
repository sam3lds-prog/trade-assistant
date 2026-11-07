"""The trading daemon (3.0 phase 5): `python run.py --daemon`, kept running by launchd (setup_daemon.sh).

It is a second process beside the web app, on the same database. It runs the Trading Desk's durable jobs (submitting
approved orders, reconciling them with the broker, expiring what was not decided in time, delivering
notifications), values the paper portfolios, refreshes Discover's reversal scan and the scanner during the session,
and, with REMOTE_APPROVALS=1, serves the phone approval pages on the Tailscale address.

While it runs (its heartbeat is under 45 seconds old), the web app leaves these jobs to it; when it stops, the web
app takes them over (DESK_WORKER=auto). After a sleep it does the current work only: missed scheduled runs are
discarded, not replayed. On start it marks any submission that was cut off as unknown and reconciles it; it never
sends an order twice.
"""

from __future__ import annotations

import logging
import signal
from typing import Any

from .jobs import Schedule, heartbeat

log = logging.getLogger(__name__)


def build(settings: Any) -> tuple[Any, Any]:
    """The service and the app's objects, built as the web app builds them, without serving them."""
    from .api import create_app
    from .bus import bus_for
    from .service import TradeAssistant
    svc = TradeAssistant(settings)
    bus_for(svc).origin = "daemon"
    app = create_app(settings, svc)
    clock = getattr(svc, "clockwork", None)
    if clock is not None:
        clock.stop.set()            # its paper work is a job here; the morning and the digest stay with the web app
    return svc, app


def handlers_for(svc: Any) -> tuple[dict[str, Any], list[Schedule]]:
    handlers: dict[str, Any] = {}
    schedules: list[Schedule] = []
    clock = getattr(svc, "clockwork", None)
    if clock is not None and clock.paper is not None:
        def paper_tick(_: dict[str, Any]) -> Any:
            if clock._active_portfolios() and svc.settings.features.get("paper_trading", True):
                return clock.paper.tick()
            return {"skipped": "no active paper portfolio"}
        handlers["paper.tick"] = paper_tick
        schedules.append(Schedule("paper.tick", 60, 90))
    d3 = getattr(svc, "discover3", None)
    if d3 is not None:
        from .v3.discover import RefreshIn

        def discover_refresh(_: dict[str, Any]) -> Any:
            got = d3.refresh(RefreshIn(sources=["reversal", "scanner"]))
            return {"observations": got["observations"], "candidates": got["candidates"], "notes": got["notes"][:3]}
        handlers["discover.refresh"] = discover_refresh
        schedules.append(Schedule("discover.refresh", 1800, 600, when="market"))
    desk = svc.trading
    handlers["jobs.prune"] = lambda _: {"pruned": desk.jobs.prune()}
    if "live" in desk.brokers:
        handlers["desk.live_check"] = lambda _: {k: v for k, v in desk.check_live(max_age=0).items() if k != "tools"}
        schedules.append(Schedule("desk.live_check", 600, 300))
    schedules.append(Schedule("jobs.prune", 86400, 3600))
    return handlers, schedules


def run(settings: Any, *, once: bool = False) -> int:
    svc, _app = build(settings)
    desk = svc.trading
    desk.worker_mode = "daemon"                  # this process is the worker; it starts no app worker of its own
    recovered = desk.recover()
    if "live" in desk.brokers:
        live = desk.check_live(max_age=0)
        log.info("Live execution: %s", "ready" if live["ready"] else "not ready: " + "; ".join(live["reasons"]))
    handlers, schedules = handlers_for(svc)
    remote = None
    if settings.remote_approvals:
        from .remote import serve_in_thread
        remote = serve_in_thread(settings, svc)
        log.info("Phone approvals on http://%s:%s/remote", settings.remote_bind, settings.remote_port)
    from . import __version__
    worker = desk.make_worker("daemon", None, handlers, schedules)
    worker.info = lambda: {"mode": desk.mode, "paper": "paper.tick" in handlers, "remote": remote is not None, "version": __version__}
    heartbeat(svc.db, "daemon", **worker.info())
    log.info("Trading daemon started (%s); recovered %s interrupted submissions", desk.mode, recovered["recovered"])
    svc.bus.publish("daemon.started", None, recovered=recovered["recovered"], remote=remote is not None)
    if once:
        worker.step()
        return 0

    def stop(signum: int, _frame: Any) -> None:
        log.info("Trading daemon stopping (signal %s)", signum)
        worker.stop.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.loop()
    svc.bus.publish("daemon.stopped", None)
    return 0


__all__ = ["run", "build", "handlers_for"]
