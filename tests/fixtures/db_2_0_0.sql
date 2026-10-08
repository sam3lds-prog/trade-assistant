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
INSERT INTO "attempts" VALUES(1,2,'2026-10-08T22:44:05+00:00','buy',7.8,8.6,8.2,4.99999999999999444e-01,1,NULL);
INSERT INTO "attempts" VALUES(2,3,'2026-10-08T22:44:05+00:00','buy',1.1,1.3,1.2,4.99999999999999444e-01,1,NULL);
INSERT INTO "attempts" VALUES(3,3,'2026-10-08T22:44:05+00:00','sell',1.8,2.0,1.9,5.00000000000000555e-01,1,NULL);
INSERT INTO "attempts" VALUES(4,4,'2026-10-08T22:44:05+00:00','buy',1.9,2.1,2.0,0.5,1,NULL);
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
, adjustment TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE executions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
            side        TEXT NOT NULL CHECK (side IN ('BUY', 'SELL', 'SELL_SHORT', 'BUY_TO_COVER')),
            quantity    REAL NOT NULL CHECK (quantity > 0),
            price       REAL NOT NULL CHECK (price >= 0),
            fees        REAL NOT NULL DEFAULT 0 CHECK (fees >= 0),
            executed_at TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL', 'ROBINHOOD', 'IMPORT')),
            note        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL
        );
INSERT INTO "executions" VALUES(1,2,'BUY',1.0,8.2,0.0,'2026-10-08T22:44:05+00:00','IMPORT','from the option journal','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(3,3,'BUY',3.0,1.2,0.0,'2026-10-08T22:44:05+00:00','IMPORT','from the option journal','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(4,3,'SELL',3.0,1.9,0.0,'2026-10-08T22:44:05+00:00','IMPORT','from the option journal','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(6,4,'BUY',1.0,2.0,0.0,'2026-10-08T22:44:05+00:00','IMPORT','from the option journal','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(7,4,'SELL',1.0,0.0,0.0,'2026-10-08T22:44:05+00:00','IMPORT','from the option journal','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(8,6,'BUY',100.0,200.0,1.0,'2026-09-15T14:00:00+00:00','MANUAL','','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(9,6,'SELL',40.0,210.0,1.0,'2026-09-22T15:00:00+00:00','MANUAL','','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(10,7,'SELL_SHORT',50.0,300.0,0.0,'2026-09-16T14:30:00+00:00','MANUAL','','2026-10-08T22:44:05+00:00');
INSERT INTO "executions" VALUES(11,7,'BUY_TO_COVER',50.0,290.0,0.0,'2026-09-25T19:00:00+00:00','MANUAL','','2026-10-08T22:44:06+00:00');
INSERT INTO "executions" VALUES(12,8,'BUY',10.0,60.0,0.0,'2026-09-10T14:00:00+00:00','MANUAL','','2026-10-08T22:44:06+00:00');
INSERT INTO "executions" VALUES(13,8,'SELL',10.0,62.0,0.0,'2026-09-11T14:00:00+00:00','MANUAL','','2026-10-08T22:44:06+00:00');
INSERT INTO "executions" VALUES(14,8,'BUY',5.0,61.0,0.0,'2026-09-12T14:00:00+00:00','MANUAL','','2026-10-08T22:44:06+00:00');
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
CREATE TABLE option_contracts (
            position_id       INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
            leg               INTEGER NOT NULL DEFAULT 1,
            underlying_symbol TEXT NOT NULL,
            option_symbol     TEXT NOT NULL DEFAULT '',
            option_type       TEXT NOT NULL CHECK (option_type IN ('CALL', 'PUT')),
            strike            REAL NOT NULL,
            expiration        TEXT NOT NULL,
            multiplier        REAL NOT NULL DEFAULT 100,
            ratio             INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (position_id, leg)
        );
INSERT INTO "option_contracts" VALUES(1,1,'AAPL','AAPL261218C00200000','CALL',200.0,'2026-12-18',100.0,1);
INSERT INTO "option_contracts" VALUES(2,1,'MSFT','MSFT261218P00400000','PUT',400.0,'2026-12-18',100.0,1);
INSERT INTO "option_contracts" VALUES(3,1,'KO','KO261218C00060000','CALL',60.0,'2026-12-18',100.0,1);
INSERT INTO "option_contracts" VALUES(4,1,'JPM','JPM261218P00200000','PUT',200.0,'2026-12-18',100.0,1);
INSERT INTO "option_contracts" VALUES(5,1,'PEP','PEP261218C00150000','CALL',150.0,'2026-12-18',100.0,1);
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
CREATE TABLE position_snapshots (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id              INTEGER NOT NULL REFERENCES positions (id) ON DELETE CASCADE,
            snapshot_type            TEXT NOT NULL CHECK (snapshot_type IN ('PLAN', 'ENTRY', 'EXIT', 'REVIEW')),
            timestamp                TEXT NOT NULL,
            underlying_price REAL, spy_price REAL, qqq_price REAL, vix REAL,
            rsi_14 REAL, macd REAL, macd_signal REAL, macd_histogram REAL,
            sma_20 REAL, sma_50 REAL, sma_200 REAL, ema_20 REAL, adx REAL, atr REAL, volume_ratio REAL,
            structure_state TEXT, trend_state TEXT,
            relative_strength_spy REAL, relative_strength_sector REAL,
            bullish_score REAL, bearish_score REAL, market_regime TEXT,
            iv REAL, iv_rank REAL, delta REAL, gamma REAL, theta REAL, vega REAL,
            bid REAL, ask REAL, mid REAL, spread_pct REAL, dte INTEGER,
            signal_engine_version    TEXT,
            data_timestamp           TEXT,
            data_source              TEXT,
            bar_status               TEXT,
            raw_json                 TEXT NOT NULL DEFAULT '{}'
        );
INSERT INTO "position_snapshots" VALUES(1,6,'ENTRY','2026-09-15T16:00:00-04:00',208.34,555.65,1253.45,NULL,6.37009607612160095e+01,2.06161086757407474e+00,8.23640497464290799e-01,1.23797037010978394e+00,200.126,199.5596,218.7024,2.01489129323002316e+02,1.22968169909112248e+01,5.49640012055632176e+00,0.833000568541982,'range','NEUTRAL',-9.64,11.0,37.7,29.6,'BULL_CALM',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-09-15','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:05+00:00", "as_of": "2026-09-15", "note": "Recorded after the fact: the market as of the fill date''s close.", "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "NEUTRAL", "status": "CONFIRMED", "as_of": "2026-09-15", "net": 8.1, "note": "Worked out from the bars up to the fill date, without validated-pattern points."}}}');
INSERT INTO "position_snapshots" VALUES(2,7,'ENTRY','2026-09-16T16:00:00-04:00',661.29,555.78,1262.58,NULL,6.05708506288661752e+01,1.94376269742925842e+01,1.38728162609396186e+01,5.56481071335296562e+00,626.31,613.0024,615.05395,6.36518013049808814e+02,1.82031465900569351e+01,3.01282275242908603e+01,6.19551509198604577e-01,'up','UP',5.68,24.74,92.0,7.6,'BULL_CALM',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-09-16','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:05+00:00", "as_of": "2026-09-16", "note": "Recorded after the fact: the market as of the fill date''s close.", "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "UP", "status": "CONFIRMED", "as_of": "2026-09-16", "net": 84.3, "note": "Worked out from the bars up to the fill date, without validated-pattern points."}}}');
INSERT INTO "position_snapshots" VALUES(3,7,'EXIT','2026-09-25T16:00:00-04:00',704.31,583.8,1247.37,NULL,6.55184996099612249e+01,2.64322415690295429e+01,2.30164454305343895e+01,3.4157961384951534e+00,6.6225249999999994e+02,624.5836,618.7233,6.69863161458311083e+02,2.54646724665268031e+01,3.00380866138159454e+01,1.42131283540923503e+00,'up','STRONG_UP',-0.42,9.1,83.6,3.8,'TRANSITION',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-09-25','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:06+00:00", "as_of": "2026-09-25", "note": "Recorded after the fact: the market as of the fill date''s close.", "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "STRONG_UP", "status": "CONFIRMED", "as_of": "2026-09-25", "net": 79.8, "note": "Worked out from the bars up to the fill date, without validated-pattern points."}}}');
INSERT INTO "position_snapshots" VALUES(4,8,'ENTRY','2026-09-10T16:00:00-04:00',179.75,553.48,1227.37,NULL,7.03746914170396564e+01,6.338254292692,6.01122120772816,3.27033084963839648e-01,1.67856499999999982e+02,1.54388199999999983e+02,1.55199449999999984e+02,1.67379251721038684e+02,1.69015933612400566e+01,7.41874234244216168e+00,1.46219888181598167e+00,'up','UP',28.55,16.84,95.7,4.3,'TRANSITION',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-09-10','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:06+00:00", "as_of": "2026-09-10", "note": "Recorded after the fact: the market as of the fill date''s close.", "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "UP", "status": "CONFIRMED", "as_of": "2026-09-10", "net": 91.3, "note": "Worked out from the bars up to the fill date, without validated-pattern points."}}}');
INSERT INTO "position_snapshots" VALUES(5,8,'EXIT','2026-09-11T16:00:00-04:00',188.55,565.1,1311.34,NULL,7.59952924692837115e+01,7.25508686543648195e+00,6.25999433926982451e+00,9.95092526166657443e-01,169.423,1.55222400000000021e+02,155.2036,1.69395513461892136e+02,1.80499119261251053e+01,7.86811788941058,1.55660698824030241e+00,'up','STRONG_UP',32.58,22.27,100.0,0.0,'BULL_CALM',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-09-11','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:06+00:00", "as_of": "2026-09-11", "note": "Recorded after the fact: the market as of the fill date''s close.", "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "STRONG_UP", "status": "CONFIRMED", "as_of": "2026-09-11", "net": 100.0, "note": "Worked out from the bars up to the fill date, without validated-pattern points."}}}');
INSERT INTO "position_snapshots" VALUES(6,9,'PLAN','2026-10-08T22:44:07+00:00',712.93,575.23,1095.41,NULL,8.35647340629533204e+01,3.43984339183671181e+01,3.12092569778226334e+01,3.18917694054448475e+00,659.0245,595.078,549.31135,6.62400171948233833e+02,6.01485627889363456e+01,2.28235431029211781e+01,1.26966673698494414e+00,'range','UP',23.91,25.26,78.8,3.3,'TRANSITION',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-10-08','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:06+00:00", "indicators": {"rsi_14": 83.56473406295332, "macd": 34.39843391836712, "macd_signal": 31.209256977822633, "macd_histogram": 3.1891769405444848, "sma_20": 659.0245, "sma_50": 595.078, "sma_200": 549.31135, "ema_20": 662.4001719482338, "adx": 60.148562788936346, "atr": 22.823543102921178, "volume_ratio": 1.2696667369849441}, "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "UP", "status": "CONFIRMED", "confirmed_state": "UP", "net": 75.5, "confidence": "HIGH"}}}');
INSERT INTO "position_snapshots" VALUES(7,10,'PLAN','2026-10-08T22:44:07+00:00',238.1,575.23,1095.41,NULL,5.72728765499570258e+01,1.26209674358068468e+00,6.64970008483987795e-01,5.97126735096696892e-01,233.01,2.30307400000000029e+02,2.34823099999999982e+02,2.32543167335872965e+02,2.23506467561382535e+01,1.01373768019695766e+01,0.694129531701523,'range','NEUTRAL',5.98,-9.91,48.3,35.9,'TRANSITION',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'signals-2.0','2026-10-08','demo','COMPLETE','{"errors": [], "taken_at": "2026-10-08T22:44:07+00:00", "indicators": {"rsi_14": 57.272876549957026, "macd": 1.2620967435806847, "macd_signal": 0.6649700084839878, "macd_histogram": 0.5971267350966969, "sma_20": 233.01, "sma_50": 230.30740000000003, "sma_200": 234.82309999999998, "ema_20": 232.54316733587297, "adx": 22.350646756138254, "atr": 10.137376801969577, "volume_ratio": 0.694129531701523}, "analysis": {"trend": {"version": "trend_engine_v2.0", "state": "NEUTRAL", "status": "CONFIRMED", "confirmed_state": "NEUTRAL", "net": 12.4, "confidence": "MEDIUM"}}}');
CREATE TABLE positions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol                TEXT NOT NULL,
            instrument_type       TEXT NOT NULL CHECK (instrument_type IN ('STOCK', 'OPTION', 'OPTION_SPREAD')),
            direction             TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
            status                TEXT NOT NULL CHECK (status IN ('PLANNED', 'OPEN', 'PARTIALLY_CLOSED', 'CLOSED',
                                                                  'CANCELLED')),
            quantity              REAL NOT NULL,           -- planned size: shares, or contracts for options
            multiplier            REAL NOT NULL DEFAULT 1, -- 100 for an equity option
            open_quantity         REAL NOT NULL DEFAULT 0, -- worked out from executions
            opened_at             TEXT,
            closed_at             TEXT,
            average_entry_price   REAL,
            average_exit_price    REAL,
            realized_pnl          REAL NOT NULL DEFAULT 0,
            fees                  REAL NOT NULL DEFAULT 0,
            thesis                TEXT NOT NULL DEFAULT '',
            setup_type            TEXT NOT NULL DEFAULT '',
            intended_horizon      INTEGER,                 -- trading days
            invalidation_price    REAL,                    -- price of the underlying that proves the idea wrong
            target_1              REAL,
            target_2              REAL,
            risk_amount           REAL,                    -- dollars at risk, if entered
            initial_risk_per_unit REAL,
            data_source           TEXT NOT NULL DEFAULT '',
            legacy_trade_id       INTEGER UNIQUE REFERENCES trades (id) ON DELETE SET NULL,
            notes                 TEXT NOT NULL DEFAULT '',
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        );
INSERT INTO "positions" VALUES(1,'AAPL','OPTION','LONG','PLANNED',2.0,100.0,0.0,NULL,NULL,NULL,NULL,0.0,0.0,'Planned call, never filled','option journal',NULL,NULL,NULL,NULL,1040.0,NULL,'demo',1,'','2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00');
INSERT INTO "positions" VALUES(2,'MSFT','OPTION','LONG','OPEN',1.0,100.0,1.0,'2026-10-08T22:44:05+00:00',NULL,8.2,NULL,0.0,0.0,'Open put from the journal','option journal',NULL,NULL,NULL,NULL,850.0,NULL,'demo',2,'Exit plan: take profit at +50%, stop at -50% of the premium.','2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00');
INSERT INTO "positions" VALUES(3,'KO','OPTION','LONG','CLOSED',3.0,100.0,0.0,'2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00',1.2,1.89999999999999968e+00,2.09999999999999971e+02,0.0,'Closed call from the journal','option journal',NULL,NULL,NULL,NULL,390.0,NULL,'demo',3,'Exit plan: take profit at +50%, stop at -50% of the premium.','2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00');
INSERT INTO "positions" VALUES(4,'JPM','OPTION','LONG','CLOSED',1.0,100.0,0.0,'2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00',2.0,0.0,-200.0,0.0,'Expired worthless','option journal',NULL,NULL,NULL,NULL,200.0,NULL,'demo',4,'expired worthless Exit plan: take profit at +50%, stop at -50% of the premium.','2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00');
INSERT INTO "positions" VALUES(5,'PEP','OPTION','LONG','CANCELLED',1.0,100.0,0.0,NULL,NULL,NULL,NULL,0.0,0.0,'Cancelled before filling','option journal',NULL,NULL,NULL,NULL,300.0,NULL,'demo',5,'changed my mind','2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00');
INSERT INTO "positions" VALUES(6,'AAPL','STOCK','LONG','PARTIALLY_CLOSED',100.0,1.0,60.0,'2026-09-15T14:00:00+00:00',NULL,200.0,210.0,398.0,2.0,'Breakout','breakout',NULL,150.0,NULL,NULL,NULL,NULL,'demo',NULL,'','2026-10-08T22:44:05+00:00','2026-10-08T22:44:05+00:00');
INSERT INTO "positions" VALUES(7,'CRWD','STOCK','SHORT','CLOSED',50.0,1.0,0.0,'2026-09-16T14:30:00+00:00','2026-09-25T19:00:00+00:00',300.0,290.0,500.0,0.0,'','',NULL,400.0,200.0,NULL,NULL,NULL,'demo',NULL,'','2026-10-08T22:44:05+00:00','2026-10-08T22:44:06+00:00');
INSERT INTO "positions" VALUES(8,'KO','STOCK','LONG','PARTIALLY_CLOSED',10.0,1.0,5.0,'2026-09-10T14:00:00+00:00',NULL,6.03333333333333357e+01,62.0,20.0,0.0,'','',NULL,NULL,NULL,NULL,NULL,NULL,'demo',NULL,'','2026-10-08T22:44:06+00:00','2026-10-08T22:44:06+00:00');
INSERT INTO "positions" VALUES(9,'NVDA','STOCK','LONG','PLANNED',20.0,1.0,0.0,NULL,NULL,NULL,NULL,0.0,0.0,'Planned, with a risk amount','',NULL,NULL,250.0,NULL,300.0,NULL,'demo',NULL,'','2026-10-08T22:44:06+00:00','2026-10-08T22:44:06+00:00');
INSERT INTO "positions" VALUES(10,'PEP','STOCK','LONG','CANCELLED',5.0,1.0,0.0,NULL,NULL,NULL,NULL,0.0,0.0,'Cancelled plan','',NULL,NULL,NULL,NULL,NULL,NULL,'demo',NULL,'no','2026-10-08T22:44:07+00:00','2026-10-08T22:44:07+00:00');
CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL);
INSERT INTO "schema_migrations" VALUES(1,'unified positions, option contracts, executions and position snapshots','2026-10-08T22:44:04+00:00');
INSERT INTO "schema_migrations" VALUES(2,'record how cached daily bars are adjusted for splits and dividends','2026-10-08T22:44:04+00:00');
INSERT INTO "schema_migrations" VALUES(3,'signal occurrences: each past signal with its context and what followed','2026-10-08T22:44:04+00:00');
CREATE TABLE signal_occurrences (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        data_source    TEXT NOT NULL,
        engine_version TEXT NOT NULL,
        trend_version  TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        symbol         TEXT NOT NULL,
        signal_key     TEXT NOT NULL,      -- pattern:<key> or trend:<state>
        direction      INTEGER NOT NULL,   -- +1 expects a rise, -1 a fall, 0 neither
        date           TEXT NOT NULL,      -- the finished bar the signal came from
        close          REAL,
        trend_state    TEXT, net_score REAL, regime TEXT,
        fwd_5 REAL, fwd_10 REAL, fwd_20 REAL,   -- next open to the close 5, 10, 20 days on
        spy_5 REAL, spy_10 REAL, spy_20 REAL,   -- SPY over the same days
        high_20 REAL, low_20 REAL,              -- best and worst prices in the next 20 days, from entry
        UNIQUE (data_source, symbol, signal_key, date)
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
INSERT INTO "trades" VALUES(1,'2026-10-08T22:44:05+00:00','demo','AAPL','AAPL261218C00200000','call',200.0,'2026-12-18',2,'Planned call, never filled','planned',5.0,5.2,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',NULL,NULL,NULL,NULL,NULL,'');
INSERT INTO "trades" VALUES(2,'2026-10-08T22:44:05+00:00','demo','MSFT','MSFT261218P00400000','put',400.0,'2026-12-18',1,'Open put from the journal','open',8.0,8.5,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',8.2,'2026-10-08T22:44:05+00:00',NULL,NULL,NULL,'');
INSERT INTO "trades" VALUES(3,'2026-10-08T22:44:05+00:00','demo','KO','KO261218C00060000','call',60.0,'2026-12-18',3,'Closed call from the journal','closed',1.2,1.3,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',1.2,'2026-10-08T22:44:05+00:00',1.9,'2026-10-08T22:44:05+00:00',210.0,'');
INSERT INTO "trades" VALUES(4,'2026-10-08T22:44:05+00:00','demo','JPM','JPM261218P00200000','put',200.0,'2026-12-18',1,'Expired worthless','closed',2.0,2.0,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',2.0,'2026-10-08T22:44:05+00:00',0.0,'2026-10-08T22:44:05+00:00',-200.0,'expired worthless');
INSERT INTO "trades" VALUES(5,'2026-10-08T22:44:05+00:00','demo','PEP','PEP261218C00150000','call',150.0,'2026-12-18',1,'Cancelled before filling','cancelled',3.0,3.0,'{"profit_target_pct": 50, "stop_loss_pct": 50}','{}','[]','[]','[]',NULL,NULL,NULL,NULL,NULL,'changed my mind');
CREATE INDEX idx_pattern_results_run ON pattern_results (run_id);
CREATE INDEX idx_positions_symbol ON positions (symbol);
CREATE INDEX idx_positions_status ON positions (status);
CREATE INDEX idx_executions_position ON executions (position_id, executed_at);
CREATE INDEX idx_snapshots_position ON position_snapshots (position_id, timestamp);
CREATE INDEX idx_signals_key ON signal_occurrences (data_source, signal_key);
CREATE INDEX idx_signals_symbol ON signal_occurrences (data_source, symbol);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('trades',5);
INSERT INTO "sqlite_sequence" VALUES('positions',10);
INSERT INTO "sqlite_sequence" VALUES('attempts',4);
INSERT INTO "sqlite_sequence" VALUES('executions',14);
INSERT INTO "sqlite_sequence" VALUES('position_snapshots',7);
COMMIT;
