"""The app's timed work (2.15): one background thread that wakes every half minute while there is work to do.

    paper trading      every active paper portfolio: catch up after a gap, value it (every minute in the session,
                       every five before and after it), and take an automated portfolio's step when it is due
    the morning        with "refresh in the morning session" on: rebuild the Morning plan every few minutes from the
                       open to the end of the morning session (11:30 unless set)
    the news digest    while Live news is listening: rebuild the digest at the interval set on its screen

It only runs while the app runs on this computer, and it stops by itself when there is nothing to do (no active
portfolio, neither refresh switched on); anything that creates such work starts it again. Nothing here places an
order.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime
from typing import Any

from .opportunities import load_prefs
from .optionmath import NY

log = logging.getLogger(__name__)

TICK_SECONDS = 30
SESSION_DIGEST_MINUTES = 30


class Clockwork:
    def __init__(self, svc: Any, *, paper: Any = None, morning: Any = None, digest: Any = None, feed: Any = None):
        self.svc = svc
        self.paper = paper
        self.morning = morning
        self.digest = digest
        self.feed = feed
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self.last: dict[str, Any] = {"morning_at": 0.0, "digest_at": 0.0, "tick_at": None, "error": None}

    # --- what there is to do ----------------------------------------------------------------------------------
    def _active_portfolios(self) -> int:
        try:
            r = self.svc.db.one("SELECT COUNT(*) AS n FROM paper_portfolios WHERE status = 'ACTIVE'")
            return int(r["n"]) if r else 0
        except Exception:                             # noqa: BLE001 - no table yet is no work
            return 0

    def needed(self) -> bool:
        if self._active_portfolios():
            return True
        try:
            prefs = load_prefs(self.svc.db, self.svc.settings)
        except Exception:                             # noqa: BLE001
            return False
        if prefs["morning"]["auto_refresh"] and self.morning is not None and self.svc.settings.features.get("morning"):
            return True
        return bool(self.feed is not None and getattr(self.feed, "running", False) and self.digest is not None)

    def ensure_running(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self.stop = threading.Event()
                self._thread = threading.Thread(target=self._run, name="clockwork", daemon=True)
                self._thread.start()

    def status(self) -> dict[str, Any]:
        return {"running": bool(self._thread and self._thread.is_alive()), "last_tick": self.last.get("tick_at"),
                "last_error": self.last.get("error")}

    def _run(self) -> None:
        while True:
            try:
                if not self.needed():
                    with self._lock:
                        if not self.needed():          # anything created meanwhile keeps it going
                            self._thread = None
                            return
                self.tick()
            except Exception as exc:                  # noqa: BLE001 - the loop must survive anything
                self.last["error"] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                log.exception("Clockwork pass failed")
            if self.stop.wait(TICK_SECONDS):
                return

    # --- one pass -----------------------------------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(NY)).astimezone(NY)
        done: dict[str, Any] = {}
        self.last["tick_at"] = now.isoformat(timespec="seconds")
        if self.paper is not None and self.svc.settings.features.get("paper_trading", True) and self._active_portfolios():
            done["paper"] = self.paper.tick()
        prefs = load_prefs(self.svc.db, self.svc.settings)
        done["morning"] = self._morning(now, prefs["morning"])
        done["digest"] = self._digest(prefs["live"]["digest"])
        return done

    def _morning(self, now: datetime, mp: dict[str, Any]) -> str | None:
        if not (mp["auto_refresh"] and self.morning is not None and self.svc.settings.features.get("morning")):
            return None
        from .paper import market_phase
        if market_phase(self.svc, now) != "open":
            return None
        hh, mm = (int(x) for x in mp["am_end"].split(":"))
        if now.time() >= dtime(hh, mm):
            return None
        if self.morning.running or time.time() - self.last["morning_at"] < 60 * int(mp["refresh_minutes"]) - 1:
            return None
        self.last["morning_at"] = time.time()
        self.morning.start(rescan=True)
        return "rescan started"

    def _digest(self, choice: str) -> str | None:
        if self.digest is None or self.feed is None or not getattr(self.feed, "running", False):
            return None
        if not self.svc.settings.features.get("live_news"):
            return None
        minutes = SESSION_DIGEST_MINUTES if choice == "session" else int(choice)
        if time.time() - self.last["digest_at"] < 60 * minutes - 1:
            return None
        self.last["digest_at"] = time.time()
        self.digest.build(choice)
        return "built"


__all__ = ["Clockwork", "TICK_SECONDS"]
