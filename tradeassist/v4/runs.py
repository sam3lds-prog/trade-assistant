"""Starting and stopping replays from Model Lab (4.0, phase 5).

A replay never runs inside the web app: the screen starts `python run.py --replay resume RUN` (or `dataset build`)
as a separate process on this computer, writing its output to data/replay-logs/. Stopping is a request the run reads
between sessions; it pauses at its checkpoint and can be resumed. A run whose heartbeat is more than two minutes old
while it says "running" was cut off (a closed laptop, a crash) and is shown as interrupted.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import replay_store

ROOT = Path(__file__).resolve().parent.parent.parent
STALE_SECONDS = 120

Spawner = Callable[[list[str], Path], Any]


def _spawn(args: list[str], log: Path) -> Any:
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab")                                        # noqa: SIM115 - handed to the child process
    return subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--replay", *args], cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)


SPAWN: Spawner = _spawn


def log_dir(svc: Any) -> Path:
    return Path(svc.settings.db_path).parent / "replay-logs"


def start(svc: Any, run_id: str) -> dict[str, Any]:
    store = replay_store(svc)
    r = store.one("SELECT status, heartbeat_at FROM replay_runs WHERE id = ?", (run_id,))
    if r is None:
        raise ValueError(f"No run {run_id}")
    if r["status"] == "complete":
        raise ValueError("That run is complete")
    if r["status"] == "running" and not interrupted(r["heartbeat_at"]):
        raise ValueError("That run is already going")
    log = log_dir(svc) / f"{run_id}.log"
    with store.tx() as cur:
        cur.execute("INSERT INTO run_controls (run_id, stop_requested_at, log_path) VALUES (?, NULL, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET stop_requested_at = NULL, log_path = excluded.log_path", (run_id, str(log)))
    SPAWN(["resume", run_id], log)
    return {"run": run_id, "started": True, "log": str(log)}


def stop(svc: Any, run_id: str) -> dict[str, Any]:
    store = replay_store(svc)
    if store.one("SELECT 1 FROM replay_runs WHERE id = ?", (run_id,)) is None:
        raise ValueError(f"No run {run_id}")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with store.tx() as cur:
        cur.execute("INSERT INTO run_controls (run_id, stop_requested_at) VALUES (?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET stop_requested_at = excluded.stop_requested_at", (run_id, now))
    return {"run": run_id, "stop_requested": now}


def build_dataset(svc: Any) -> dict[str, Any]:
    log = log_dir(svc) / f"dataset-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.log"
    SPAWN(["dataset", "build"], log)
    return {"started": True, "log": str(log)}


def interrupted(heartbeat_at: str | None) -> bool:
    if not heartbeat_at:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat_at)
    return age.total_seconds() > STALE_SECONDS


def tail(path: str | None, lines: int = 20) -> list[str]:
    if not path or not Path(path).is_file():
        return []
    text = Path(path).read_bytes()[-20000:].decode(errors="replace").replace("\r", "\n")
    return [x for x in text.splitlines() if x.strip()][-lines:]


__all__ = ["start", "stop", "build_dataset", "interrupted", "tail", "SPAWN", "ROOT"]
