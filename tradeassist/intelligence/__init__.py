"""Trade Assistant 4.0 intelligence: a model that learns from its own frozen predictions (see docs/V4.md).

The package replays history one trading session at a time, as the app would have lived through it:

    reconstruct what was known after a session's close -> predict -> freeze the prediction -> move on ->
    grade the prediction once its window has ended -> retrain on what has matured -> promote only on evidence

Nothing here can place, change or cancel an order. The package reads market data only through a function it is
handed (`fetch(symbol, start, end) -> bars`), never through a data provider, the Trading Desk or a broker, and a
test (tests/test_v4_isolation.py) fails if any module in it can reach an order path.

Layout:

    data/       the frozen dataset, the session calendar, the universe and the causal features
    replay/     the as-of view, the labels and their maturity, simulated trades, the day loop, the scorecards
    learning/   the immutable ledger, training, validation and the model registry
    models/     baselines, next-day and five-day return models, the target/stop/timeout model, the take/pass gate
    store.py    data/replay.db: its own SQLite file and migrations, apart from the trading database
"""

ENGINE_VERSION = "intelligence-4.0"
