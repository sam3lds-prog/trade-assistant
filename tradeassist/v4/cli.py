"""`python run.py --replay ...`: build datasets, run and resume replays, read their scorecards (4.0).

    python run.py --replay dataset build              fetch ten years (plus a warm-up year) for the universe and freeze it
    python run.py --replay dataset list               the frozen datasets and their checks
    python run.py --replay dataset checks DATASET     every check of one dataset
    python run.py --replay run [options]              start a development replay on the newest dataset (or --dataset)
    python run.py --replay resume RUN                 continue a paused or interrupted run from its checkpoint
    python run.py --replay runs                       every run, newest first
    python run.py --replay report RUN [--json]        a run's scorecard

A replay runs in this process, not in the web app, so it never slows the Trading Desk. Stop it with Ctrl-C at any
point and resume it later: everything up to the last finished session is kept.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from typing import Any

from ..intelligence.config import DATASET_FROM, DEVELOPMENT_FROM, HOLDOUT_FROM, ReplayConfig


def _store(settings: Any):
    from ..intelligence.store import ReplayStore, replay_path
    return ReplayStore(replay_path(settings))


def _bar(done: int, total: int, label: str, started: float) -> None:
    width = 30
    filled = int(width * done / max(1, total))
    rate = (time.monotonic() - started) / max(1, done)
    eta = rate * (total - done)
    sys.stdout.write(f"\r[{'#' * filled}{'.' * (width - filled)}] {done}/{total} {label:<12} ~{eta / 60:.0f} min left ")
    sys.stdout.flush()


def build_dataset(settings: Any, universe_name: str, first: date | None, last: date | None) -> int:
    from ..intelligence.data import dataset as DS
    from ..intelligence.data import universe as U
    from ..market import settled_through
    from ..marketdays import is_trading_day, previous_trading_day
    from ..providers import make_provider
    provider = make_provider(settings)
    info = provider.info
    if info.mode == "demo":
        print("Demo mode: these are synthetic prices, useful for trying the replay, not for learning anything. Set a Tradier "
              "token in .env for real history.")
    end = last or settled_through(time.time())
    while not is_trading_day(end):
        end = previous_trading_day(end)
    start = first or DATASET_FROM
    universe = U.get(universe_name)
    print(f"Fetching {len(universe.symbols)} symbols from {start} to {end} ({info.label})…")
    started = time.monotonic()
    store = _store(settings)
    ds = DS.build(store, provider.history, universe, start, end, source=info.mode,
                  adjustment=getattr(info, "history_adjustment", "unknown"),
                  progress=lambda n, t, s: _bar(n, t, s, started))
    print()
    counts = {lvl: sum(1 for c in ds.checks if c["level"] == lvl) for lvl in ("fail", "warn", "info")}
    print(f"Dataset {ds.id}: {len(ds.bars)} symbols, {len(ds.calendar)} sessions ({ds.calendar.first} to {ds.calendar.last}); "
          f"checks: {counts['fail']} failed, {counts['warn']} warnings, {counts['info']} notes.")
    for c in ds.checks:
        if c["level"] in ("fail", "warn"):
            print(f"  {c['level'].upper():5} {c['code']}: {c['text']}")
    return 1 if counts["fail"] else 0


def list_datasets(settings: Any) -> int:
    from ..intelligence.data.dataset import summaries
    rows = summaries(_store(settings))
    if not rows:
        print("No datasets yet. Build one with: python run.py --replay dataset build")
    for r in rows:
        c = r["checks"]
        print(f"{r['id']}  {r['source']:<9} {r['universe']:<16} {r['first_date']} to {r['last_date']}  {r['symbols']} symbols  "
              f"{r['sessions']} sessions  checks: {c['fail']} failed, {c['warn']} warnings")
    return 0


def show_checks(settings: Any, dataset_id: str) -> int:
    rows = _store(settings).query("SELECT symbol, level, code, text FROM dataset_checks WHERE dataset_id = ? ORDER BY id", (dataset_id,))
    for r in rows:
        print(f"{r['level'].upper():5} {r['code']:<26} {r['text']}")
    return 0


def _config(args: argparse.Namespace) -> ReplayConfig:
    cfg = ReplayConfig()
    changes: dict[str, Any] = {}
    if args.start:
        changes["start"] = date.fromisoformat(args.start)
    if args.end:
        changes["end"] = date.fromisoformat(args.end)
    if args.families:
        changes["families"] = tuple(f.strip() for f in args.families.split(",") if f.strip())
    if args.retrain:
        changes["retrain"] = args.retrain
    if args.universe:
        changes["universe"] = args.universe
    return cfg.with_(**changes).check()


def run_replay(settings: Any, args: argparse.Namespace) -> int:
    from ..intelligence.replay.runner import ReplayError, create_run
    store = _store(settings)
    dataset_id = args.dataset
    if not dataset_id:
        row = store.one("SELECT id FROM datasets ORDER BY created_at DESC LIMIT 1")
        if row is None:
            print("No dataset yet. Build one first: python run.py --replay dataset build")
            return 1
        dataset_id = row["id"]
    try:
        cfg = _config(args)
        run_id = create_run(store, dataset_id, cfg, allow_failed_checks=args.allow_failed_checks)
    except (ReplayError, ValueError) as exc:
        print(str(exc))
        return 1
    print(f"Run {run_id} on {dataset_id}: {cfg.start} to {cfg.end}, horizons {', '.join(map(str, cfg.horizons))}, "
          f"retraining {cfg.retrain}, challengers {', '.join(cfg.families)}.")
    return _drive(store, run_id)


def _drive(store: Any, run_id: str) -> int:
    from ..intelligence.replay.runner import Replay
    print("Loading the dataset and computing features…")
    replay = Replay(store, run_id)
    started = time.monotonic()
    replay.progress = lambda p: _bar(p["done"], p["total"], p["session"], started)
    try:
        result = replay.run()
    except KeyboardInterrupt:
        replay._pause()
        print(f"\nStopped. Everything through the last finished session is kept; continue with: python run.py --replay resume {run_id}")
        return 130
    print()
    if result["status"] != "complete":
        print(f"Paused at {result.get('checkpoint')}.")
        return 0
    _print_card(result["scorecard"])
    print(f"Ledger hash {result['ledger_sha256'][:16]}… (the same dataset and settings always give the same hash)")
    return 0


def resume(settings: Any, run_id: str) -> int:
    store = _store(settings)
    row = store.one("SELECT status FROM replay_runs WHERE id = ?", (run_id,))
    if row is None:
        print(f"No run {run_id}")
        return 1
    if row["status"] == "complete":
        print("That run is complete.")
        return 0
    return _drive(store, run_id)


def list_runs(settings: Any) -> int:
    rows = _store(settings).query("SELECT id, kind, status, dataset_id, first_session, last_session, checkpoint, summary_json "
                                  "FROM replay_runs ORDER BY created_at DESC")
    if not rows:
        print("No runs yet.")
    for r in rows:
        head = json.loads(r["summary_json"]).get("headline") if r["summary_json"] else ""
        print(f"{r['id']}  {r['kind']:<11} {r['status']:<9} {r['first_session']} to {r['last_session']}  at {r['checkpoint'] or '-'}")
        if head:
            print(f"    {head}")
    return 0


def report(settings: Any, run_id: str, as_json: bool) -> int:
    from ..intelligence.replay.metrics import scorecard
    store = _store(settings)
    row = store.one("SELECT status, summary_json FROM replay_runs WHERE id = ?", (run_id,))
    if row is None:
        print(f"No run {run_id}")
        return 1
    card = json.loads(row["summary_json"]) if row["status"] == "complete" and row["summary_json"] else scorecard(store, run_id)
    if as_json:
        print(json.dumps(card, indent=2, default=str))
    else:
        _print_card(card)
    return 0


def _print_card(card: dict[str, Any]) -> None:
    print(f"\n{card['headline']}\n")
    for h, b in card.get("daily", {}).items():
        if not b.get("graded"):
            continue
        cov = f"{100 * b['coverage']:.0f}%" if b.get("coverage") is not None else "-"
        acc = f"{100 * b['directional_accuracy']:.1f}% of {b['directional_cases']:,}" if b.get("directional_accuracy") is not None else "-"
        print(f"{h}-day: error {b['mae_pct']:.3f}% vs {b['nochange_mae_pct']:.3f}% for no change "
              f"({b['gain_bp']:+.2f} bp, t {b['t']:.2f}); 80% band held {cov}; direction right {acc} leaning calls; "
              f"{b.get('pending', 0)} pending")
    tr = card.get("trade") or {}
    for k in ("all_setups", "taken", "passed"):
        s = tr.get(k)
        if s and s.get("trades"):
            pf = f"{s['profit_factor']:.2f}" if s.get("profit_factor") is not None else "-"
            print(f"trades {k.replace('_', ' ')}: {s['trades']:,}, win {100 * s['win_rate']:.0f}%, average {100 * s['expectancy']:+.2f}% net, "
                  f"profit factor {pf}, worst run {100 * s['max_drawdown']:+.1f}%")
    for k, v in (card.get("champions") or {}).items():
        print(f"final champion {k}: {v['family']} ({v['version']})")
    for n in card.get("notes", []):
        print(f"note: {n}")


def main(settings: Any, argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="run.py --replay", description="Trade Assistant 4.0 replays")
    sub = p.add_subparsers(dest="cmd", required=True)
    ds = sub.add_parser("dataset")
    ds_sub = ds.add_subparsers(dest="action", required=True)
    b = ds_sub.add_parser("build")
    b.add_argument("--universe", default="large-caps-2026")
    b.add_argument("--from", dest="first", help=f"first day (default {DATASET_FROM})")
    b.add_argument("--to", dest="last", help="last day (default: the last finished session)")
    ds_sub.add_parser("list")
    c = ds_sub.add_parser("checks")
    c.add_argument("dataset")
    r = sub.add_parser("run")
    r.add_argument("--dataset")
    r.add_argument("--start", help=f"first prediction session (default {DEVELOPMENT_FROM})")
    r.add_argument("--end", help=f"last prediction session (default {HOLDOUT_FROM - timedelta(days=1)}; must be before {HOLDOUT_FROM})")
    r.add_argument("--families", help="challenger families, comma-separated (default drift,engines30,ridge,gbm)")
    r.add_argument("--retrain", choices=["weekly", "monthly", "never"])
    r.add_argument("--universe")
    r.add_argument("--allow-failed-checks", action="store_true")
    rs = sub.add_parser("resume")
    rs.add_argument("run")
    sub.add_parser("runs")
    rp = sub.add_parser("report")
    rp.add_argument("run")
    rp.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if args.cmd == "dataset":
        if args.action == "build":
            return build_dataset(settings, args.universe, date.fromisoformat(args.first) if args.first else None,
                                 date.fromisoformat(args.last) if args.last else None)
        if args.action == "list":
            return list_datasets(settings)
        return show_checks(settings, args.dataset)
    if args.cmd == "run":
        return run_replay(settings, args)
    if args.cmd == "resume":
        return resume(settings, args.run)
    if args.cmd == "runs":
        return list_runs(settings)
    return report(settings, args.run, args.json)


__all__ = ["main"]
