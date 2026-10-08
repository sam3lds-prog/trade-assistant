BEGIN TRANSACTION;
CREATE TABLE attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades (id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    side            TEXT NOT NULL,           -- buy | sell
    bid             REAL NOT NULL,
    ask             REAL NOT NULL,
    limit_price     REAL NOT NULL,
    position        REAL NOT NULL,           -- 0 = passive side of the spread, 1 = crossing it
    filled          INTEGER NOT NULL,
    seconds_to_fill REAL
);
INSERT INTO "attempts" VALUES(1,2,'2026-10-08T22:44:03+00:00','buy',7.8,8.6,8.2,4.99999999999999444e-01,1,NULL);
INSERT INTO "attempts" VALUES(2,3,'2026-10-08T22:44:03+00:00','buy',1.1,1.3,1.2,4.99999999999999444e-01,1,NULL);
INSERT INTO "attempts" VALUES(3,3,'2026-10-08T22:44:03+00:00','sell',1.8,2.0,1.9,5.00000000000000555e-01,1,NULL);
INSERT INTO "attempts" VALUES(4,4,'2026-10-08T22:44:03+00:00','buy',1.9,2.1,2.0,0.5,1,NULL);
CREATE TABLE bars (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE bars_meta (
    symbol     TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    first_date TEXT NOT NULL,
    last_date  TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE iv_history (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    atm_iv REAL NOT NULL,
    hv20   REAL,
    spot   REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE kv_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE pattern_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    run_at      TEXT NOT NULL,
    source      TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    horizon     INTEGER NOT NULL,
    verdict     TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE TABLE trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    data_source    TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    option_symbol  TEXT NOT NULL,
    option_type    TEXT NOT NULL,
    strike         REAL NOT NULL,
    expiration     TEXT NOT NULL,
    contracts      INTEGER NOT NULL,
    thesis         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,            -- planned | open | closed | cancelled
    planned_limit  REAL,
    max_limit      REAL,
    plan_json      TEXT NOT NULL DEFAULT '{}',
    quote_json     TEXT NOT NULL DEFAULT '{}',
    scorecard_json TEXT NOT NULL DEFAULT '[]',
    signals_json   TEXT NOT NULL DEFAULT '[]',
    overrides_json TEXT NOT NULL DEFAULT '[]',
    entry_price    REAL,
    entry_at       TEXT,
    exit_price     REAL,
    exit_at        TEXT,
    pnl            REAL,
    notes          TEXT NOT NULL DEFAULT ''
);
INSERT INTO "trades" VALUES(1,'2026-10-08T22:44:03+00:00','demo','AAPL','AAPL261218C00200000','call',200.0,'2026-12-18',2,'Planned call, never filled','planned',5.0,5.2,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',NULL,NULL,NULL,NULL,NULL,'');
INSERT INTO "trades" VALUES(2,'2026-10-08T22:44:03+00:00','demo','MSFT','MSFT261218P00400000','put',400.0,'2026-12-18',1,'Open put from the journal','open',8.0,8.5,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',8.2,'2026-10-08T22:44:03+00:00',NULL,NULL,NULL,'');
INSERT INTO "trades" VALUES(3,'2026-10-08T22:44:03+00:00','demo','KO','KO261218C00060000','call',60.0,'2026-12-18',3,'Closed call from the journal','closed',1.2,1.3,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',1.2,'2026-10-08T22:44:03+00:00',1.9,'2026-10-08T22:44:03+00:00',210.0,'');
INSERT INTO "trades" VALUES(4,'2026-10-08T22:44:03+00:00','demo','JPM','JPM261218P00200000','put',200.0,'2026-12-18',1,'Expired worthless','closed',2.0,2.0,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',2.0,'2026-10-08T22:44:03+00:00',0.0,'2026-10-08T22:44:03+00:00',-200.0,'expired worthless');
INSERT INTO "trades" VALUES(5,'2026-10-08T22:44:03+00:00','demo','PEP','PEP261218C00150000','call',150.0,'2026-12-18',1,'Cancelled before filling','cancelled',3.0,3.0,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',NULL,NULL,NULL,NULL,NULL,'changed my mind');
CREATE INDEX idx_pattern_results_run ON pattern_results (run_id);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('trades',5);
INSERT INTO "sqlite_sequence" VALUES('attempts',4);
COMMIT;
