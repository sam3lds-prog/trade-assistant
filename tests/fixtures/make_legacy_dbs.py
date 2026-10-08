"""Build the legacy database fixtures used by tests/test_migrations.py.

Each fixture is made by the released code of an older version, not by today's code, so the
migration tests check what real older databases look like. Run it from a checkout of the
version being captured, for example:

    git worktree add /tmp/ta-2.1.0 d614c7a
    python tests/fixtures/make_legacy_dbs.py /tmp/ta-2.1.0 tests/fixtures/db_2_1_0.sql

The fixtures in this folder were made from these commits:

    db_0_4_0.sql   5fe0ee7  Trade Assistant 0.4.0 (before V2: classic journal only)
    db_2_0_0.sql   bbbfb56  Trade Assistant 2.0.0 (migrations 1 to 3)
    db_2_1_0.sql   d614c7a  Trade Assistant 2.1.0 (migrations 1 to 4)

Price caches (bars, volatility history, cached lookups, study results) are emptied before
the dump: they are rebuilt from the data source and only make the files large.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

CACHES = ("bars", "bars_meta", "iv_history", "kv_cache", "pattern_results", "signal_occurrences")


def journal_entries(journal) -> None:
    common = dict(data_source="demo", plan={"profit_target_pct": 50, "stop_loss_pct": 50}, quote={}, scorecard=[],
                  signals=[], overrides=[])
    planned = journal.create(symbol="AAPL", option_symbol="AAPL261218C00200000", option_type="call", strike=200,
                             expiration="2026-12-18", contracts=2, thesis="Planned call, never filled", planned_limit=5.0,
                             max_limit=5.2, **common)
    opened = journal.create(symbol="MSFT", option_symbol="MSFT261218P00400000", option_type="put", strike=400,
                            expiration="2026-12-18", contracts=1, thesis="Open put from the journal", planned_limit=8.0,
                            max_limit=8.5, **common)
    journal.log_attempt(opened, side="buy", bid=7.8, ask=8.6, limit_price=8.2, filled=True)
    closed = journal.create(symbol="KO", option_symbol="KO261218C00060000", option_type="call", strike=60,
                            expiration="2026-12-18", contracts=3, thesis="Closed call from the journal", planned_limit=1.2,
                            max_limit=1.3, **common)
    journal.log_attempt(closed, side="buy", bid=1.1, ask=1.3, limit_price=1.2, filled=True)
    journal.log_attempt(closed, side="sell", bid=1.8, ask=2.0, limit_price=1.9, filled=True)
    worthless = journal.create(symbol="JPM", option_symbol="JPM261218P00200000", option_type="put", strike=200,
                               expiration="2026-12-18", contracts=1, thesis="Expired worthless", planned_limit=2.0,
                               max_limit=2.0, **common)
    journal.log_attempt(worthless, side="buy", bid=1.9, ask=2.1, limit_price=2.0, filled=True)
    journal.close_at(worthless, 0.0, "expired worthless")
    cancelled = journal.create(symbol="PEP", option_symbol="PEP261218C00150000", option_type="call", strike=150,
                               expiration="2026-12-18", contracts=1, thesis="Cancelled before filling", planned_limit=3.0,
                               max_limit=3.0, **common)
    journal.cancel(cancelled, "changed my mind")
    del planned


def unified_positions(client) -> None:
    def ok(r):
        assert r.status_code == 200, r.text
        return r.json()

    long_ = ok(client.post("/v2/positions", json={
        "symbol": "AAPL", "quantity": 100, "thesis": "Breakout", "setup_type": "breakout", "invalidation_price": 150,
        "entry": {"price": 200.0, "fees": 1.0, "executed_at": "2026-09-15T10:00:00-04:00"}}))
    ok(client.post(f"/v2/positions/{long_['id']}/executions",
                   json={"side": "SELL", "quantity": 40, "price": 210.0, "fees": 1.0, "executed_at": "2026-09-22T11:00:00-04:00"}))
    short = ok(client.post("/v2/positions", json={
        "symbol": "CRWD", "direction": "SHORT", "quantity": 50, "invalidation_price": 400, "target_1": 200,
        "entry": {"price": 300.0, "executed_at": "2026-09-16T10:30:00-04:00"}}))
    ok(client.post(f"/v2/positions/{short['id']}/executions",
                   json={"side": "BUY_TO_COVER", "quantity": 50, "price": 290.0, "executed_at": "2026-09-25T15:00:00-04:00"}))
    # Closed, then bought again: version 2.1 and earlier let this reopen the same position.
    again = ok(client.post("/v2/positions", json={
        "symbol": "KO", "quantity": 10, "entry": {"price": 60.0, "executed_at": "2026-09-10T10:00:00-04:00"}}))
    ok(client.post(f"/v2/positions/{again['id']}/executions",
                   json={"side": "SELL", "quantity": 10, "price": 62.0, "executed_at": "2026-09-11T10:00:00-04:00"}))
    ok(client.post(f"/v2/positions/{again['id']}/executions",
                   json={"side": "BUY", "quantity": 5, "price": 61.0, "executed_at": "2026-09-12T10:00:00-04:00"}))
    ok(client.post("/v2/positions", json={"symbol": "NVDA", "quantity": 20, "thesis": "Planned, with a risk amount",
                                          "risk_amount": 300, "target_1": 250}))
    plan = ok(client.post("/v2/positions", json={"symbol": "PEP", "quantity": 5, "thesis": "Cancelled plan"}))
    ok(client.post(f"/v2/positions/{plan['id']}/cancel", json={"note": "no"}))


def main(checkout: str, out: str) -> None:
    sys.path.insert(0, str(Path(checkout).resolve()))
    from tradeassist.config import Settings
    from tradeassist.db import Database
    from tradeassist.journal import Journal
    from tradeassist.providers.demo import DemoData

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.db"
        db = Database(path)
        has_v2 = db.one("SELECT name FROM sqlite_master WHERE name = 'positions'") is not None
        if has_v2:
            from fastapi.testclient import TestClient
            from tradeassist.api import create_app
            from tradeassist.service import TradeAssistant
            settings = Settings(data_mode="demo", db_path=path)
            svc = TradeAssistant(settings, provider=DemoData(), db=db)
            journal_entries(svc.journal)
            unified_positions(TestClient(create_app(settings, svc)))
        else:
            journal_entries(Journal(db))
        db.close()
        conn = sqlite3.connect(path)
        for table in CACHES:
            if conn.execute("SELECT name FROM sqlite_master WHERE name = ?", (table,)).fetchone():
                conn.execute(f"DELETE FROM {table}")
        conn.commit()
        Path(out).write_text("\n".join(conn.iterdump()) + "\n")
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
