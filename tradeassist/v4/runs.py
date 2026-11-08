"""Starting and stopping replays from Model Lab (4.0, phase 5).

A replay never runs inside the web app: the screen starts `python run.py --replay resume RUN` (or `dataset build`)
as a separate process on this computer, writing its output to data/replay-logs/. Stopping is a request the run reads
between sessions; it pauses at its checkpoint and can be resumed. A run whose heartbeat is older than the runner's
lease while it says "running" was cut off (a closed laptop, a crash) and is shown as interrupted. Starting is claimed
in one statement, so a double click starts one process; the runner's own claim stops a second one regardless.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..intelligence.replay.runner import LEASE_SECONDS
from . import replay_store

ROOT = Path(__file__).resolve().parent.parent.parent
STALE_SECONDS = LEASE_SECONDS          # the runner's lease: a heartbeat older than this was cut off

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
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(seconds=STALE_SECONDS)).isoformat(timespec="seconds")
    with store.tx() as cur:                # claimed in one statement: a second click starts nothing
        cur.execute("INSERT INTO run_controls (run_id, stop_requested_at, log_path, spawned_at) VALUES (?, NULL, ?, ?) "
                    "ON CONFLICT(run_id) DO UPDATE SET stop_requested_at = NULL, log_path = excluded.log_path, spawned_at = excluded.spawned_at "
                    "WHERE run_controls.spawned_at IS NULL OR run_controls.spawned_at < ? "
                    # ...or the process started then has picked the run up and stopped since (paused, failed)
                    "OR EXISTS (SELECT 1 FROM replay_runs r WHERE r.id = run_controls.run_id AND r.status != 'running' "
                    "AND r.heartbeat_at >= run_controls.spawned_at)",
                    (run_id, str(log), now.isoformat(timespec="seconds"), recent))
        claimed = cur.rowcount > 0
    if not claimed:
        raise ValueError("That run was started a moment ago")
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
