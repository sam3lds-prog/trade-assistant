"""3.1, UX and reliability: no unsupported claim (a range is an 80% band only when calibrated, fillability is a
heuristic, exits are software-monitored and say when they could not be checked), every widget has help, a Research
strategy reaches the Trading Desk as a draft with its context and an honest route, and an interrupted study is
found, failed and can be run again. Nothing here loosens a trading permission."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tradeassist import analogs as A
from tradeassist import contracts as C
from tradeassist import forecast as F
from tradeassist import policy as P
from tradeassist import studio
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.providers.demo import DemoData
from tradeassist.service import RequestError, TradeAssistant
from tradeassist.v3 import drafts as DR
from tradeassist.v3 import research as R
from tests.test_research3 import frame, synthetic_rows, walk

STATIC = Path(__file__).resolve().parent.parent / "tradeassist" / "static"
SIX = ("purpose", "data", "use", "read", "limits", "next")


# --- every widget has help ------------------------------------------------------------------------------------------

def _registered_types() -> set[str]:
    types = set()
    for name in ("ws-widgets.js", "ws-research.js", "ws-discover.js", "ws-desk.js"):
        types |= set(re.findall(r'WS\.register\("(\w+)"', (STATIC / name).read_text()))
    return types


def _help_blocks() -> dict[str, str]:
    """Each native widget's entry in ws-help.js's registry, as source text."""
    src = (STATIC / "ws-help.js").read_text()
    body = src[src.index("const H = {"):src.index("// What changed in 3.0 for each classic screen")]
    starts = [(m.group(1), m.start()) for m in re.finditer(r"^    (\w+): \{", body, re.M)]
    return {k: body[s:(starts[i + 1][1] if i + 1 < len(starts) else len(body))] for i, (k, s) in enumerate(starts)}


def test_every_native_widget_has_help_in_six_parts_and_trading_widgets_say_what_is_sent():
    blocks = _help_blocks()
    native = _registered_types()
    assert native and native <= set(blocks), sorted(native - set(blocks))
    for t in native:
        for part in SIX:
            assert re.search(rf"\b{part}: \"", blocks[t]), (t, part)
    trading = set()
    for name in ("ws-widgets.js", "ws-desk.js"):
        trading |= set(re.findall(r'WS\.register\("(\w+)", \{ title: "[^"]+", category: "Trading"', (STATIC / name).read_text()))
    assert {"approvals", "proposals", "deskstatus", "policy", "deskpositions", "autopilot", "ticket", "activity"} <= trading
    for t in trading:
        assert "trading: \"" in blocks[t], t


def test_every_classic_screen_widget_reuses_its_2x_help():
    classic = re.search(r"const CLASSIC = \{(.*?)\};", (STATIC / "ws-widgets.js").read_text(), re.S).group(1)
    views = re.findall(r"(\w+): \[", classic)
    help_js = (STATIC / "help.js").read_text()
    entries = set(re.findall(r"^  (\w+): \{ lead:", help_js, re.M))
    assert views and set(views) <= entries, set(views) - entries


def test_the_frame_draws_the_icon_and_nothing_hardcodes_one_per_widget():
    core = (STATIC / "ws-core.js").read_text()
    assert "WS.helpButton ? WS.helpButton(w) : null" in core                    # first among the header's controls
    for name in ("ws-widgets.js", "ws-research.js", "ws-discover.js", "ws-desk.js"):
        assert "helpButton(" not in (STATIC / name).read_text(), name


# --- P0: no claim the numbers do not support -------------------------------------------------------------------------

def test_an_uncalibrated_forecast_never_claims_80_percent_coverage():
    s = A.prepare("XYZ", frame(walk()), None, own=True)
    fc = F.forecast(s, [s], 5, last_price=100.0)
    words = " ".join(fc["explanation"] + fc["invalidators"])
    assert "80%" not in words and "held 80" not in words
    assert fc["range_words"] == F.RANGE_WORDS["unvalidated"] and fc["range_calibrated"] is False
    assert "10th to 90th percentile" in words and "not yet" in words.lower()


def test_a_calibration_that_failed_its_coverage_check_is_not_called_a_band_either():
    cal = F.fit(synthetic_rows(signal=0.6), 5)
    s = A.prepare("XYZ", frame(walk()), None, own=True)
    for status, claims in (("not_calibrated", False), ("insufficient_data", False), ("calibrated", True)):
        fc = F.forecast(s, [s], 5, calibration={**cal, "status": status}, last_price=100.0)
        words = " ".join(fc["explanation"] + fc["invalidators"])
        assert ("held 80% of outcomes" in words) is claims, status
        assert fc["range_calibrated"] is claims


def test_the_study_screen_words_the_range_by_its_calibration():
    js = (STATIC / "ws-research.js").read_text()
    assert "80% of outcomes between" not in js
    assert "Model-implied 10th–90th percentile range" in js and "Historical coverage has not yet been validated." in js
    assert 'fc.calibration.status === "calibrated"' in js
    assert "Est. fillability" in js and "Fill chance" not in js


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off")
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    client = TestClient(create_app(s, service))
    yield service, client
    service.db.close()


def test_fillability_is_labelled_a_heuristic(svc):
    service, _ = svc
    s = A.prepare("XYZ", frame(walk()), None, own=True)
    fc = F.forecast(s, [s], 5, last_price=100.0)
    out = studio.compare(service.whatif, "AAPL", forecast=fc, horizon_days=5, direction="LONG")
    assert out["candidates"] and all(c["fill_basis"] == "heuristic" for c in out["candidates"])
    assert "heuristic" in out["note"] and "not a measured chance of execution" in out["note"]
    assert "Trading Desk can send" not in (out["why"] or "")


# --- the route and the draft -----------------------------------------------------------------------------------------

def fake_desk(**pol):
    policy = P.TradingPolicy(**{**P.TradingPolicy().model_dump(), **pol})
    return SimpleNamespace(policy=lambda: policy, live_ready=False)


SHARES = {"key": "shares", "instrument": "shares", "legs": []}
CALL = {"key": "long_call", "instrument": "option", "legs": [{"side": "buy", "option_type": "call", "strike": 100, "expiration": "2026-12-18", "mid": 3.1, "ask": 3.2}]}
SPREAD = {"key": "bull_call_spread", "instrument": "multi_leg", "legs": [{"side": "buy", "mid": 3.0}, {"side": "sell", "mid": 1.0}]}
UNPRICED = {"key": "long_put", "instrument": "option", "legs": [{"side": "buy", "option_type": "put", "strike": 90, "expiration": "2026-12-18"}]}


def test_the_route_follows_the_policy_and_the_broker_not_the_strategy():
    paper = fake_desk(mode="paper", account_kind="paper")
    assert DR.route(paper, SHARES, "tradier")["route"] == "automatic"
    call = DR.route(paper, CALL, "tradier")                                    # the default policy allows shares only
    assert call["route"] == "manual" and any("not option" in r for r in call["reasons"])
    assert DR.route(paper, CALL, "demo")["route"] == "research"               # made-up prices: nothing to enter
    assert DR.route(paper, SPREAD, "tradier")["route"] == "manual"            # several legs: by hand
    assert DR.route(paper, UNPRICED, "tradier")["route"] == "research"         # no price to set a limit from
    opts = fake_desk(mode="paper", account_kind="paper", instruments=["equity", "option"])
    assert DR.route(opts, CALL, "tradier")["route"] == "automatic"
    live = fake_desk(mode="approval_required", account_kind="live", instruments=["equity", "option"])
    r = DR.route(live, CALL, "tradier")
    assert r["route"] == "manual" and "share orders only" in " ".join(r["reasons"])
    off = fake_desk(mode="disabled", account_kind="paper")
    assert DR.route(off, SHARES, "tradier")["route"] == "manual"
    short = DR.route(paper, {"key": "short", "instrument": "short", "legs": []}, "tradier")
    assert short["route"] == "manual" and "sell short" in " ".join(short["reasons"])     # long only by default


def run_study(service, symbol="AMZN", **kw):
    req = R.StudyIn(symbol=symbol, horizon_days=5, thesis="swing", direction="LONG", **kw)
    out = service.research3.start(req)
    end = time.time() + 90
    while time.time() < end:
        got = service.research3.get(out["id"])
        if got["status"] != "running":
            return got
        time.sleep(0.2)
    raise AssertionError("the study did not finish")


@pytest.mark.clock("2026-10-22T10:30")
def test_a_draft_carries_the_study_its_risk_its_exit_and_a_policy_sized_proposal(svc):
    service, client = svc
    got = run_study(service)
    assert got["routes"]["shares"]["route"] == "automatic" and got["routes"]["long_call"]["route"] == "research"
    d = client.post("/v3/drafts", json={"study_id": got["id"], "strategy": "shares"}).json()
    assert d["kind"] == "draft_trade" and d["symbol"] == "AMZN" and d["source"]["study_id"] == got["id"]
    assert d["scenario"]["model_version"] == F.MODEL_VERSION and d["scenario"]["calibration"] == "not_calibrated"
    assert d["scenario"]["range_words"] == DR.UNVALIDATED_RANGE
    ex = d["execution"]
    assert ex["route"] == "automatic" and ex["check"]["ok"], ex
    pol = service.trading.policy()
    body = ex["proposal"]
    assert body["source"] == "research" and body["study_id"] == got["id"] and body["instrument"] == "equity"
    assert body["quantity"] * body["limit_price"] <= pol.max_order + 1e-9      # sized to the policy, not 100 shares
    assert d["quantity"] == body["quantity"] and "fits the policy" in d["sizing"]
    for k in ("theoretical_max_loss", "modeled_p10", "expected_shortfall", "capital_required"):
        assert k in d["risk"]
    assert any("not an order" in r for r in d["exit_plan"]["rules"]) and d["exit_plan"]["time_exit"]
    # The body is exactly what the desk takes: proposing it writes a research proposal bound to the study.
    p = client.post("/v3/proposals", json=body).json()
    assert p["status"] == "awaiting_approval" and p["source"] == "research" and p["study_id"] == got["id"]


@pytest.mark.clock("2026-10-22T10:30")
def test_a_draft_the_desk_cannot_send_says_so_and_carries_no_proposal(svc):
    service, client = svc
    got = run_study(service)
    d = client.post("/v3/drafts", json={"study_id": got["id"], "strategy": "long_call"}).json()
    assert d["execution"]["route"] == "research" and "proposal" not in d["execution"]
    assert any("demo" in r for r in d["execution"]["reasons"])
    assert client.post("/v3/drafts", json={"study_id": got["id"], "strategy": "nope"}).status_code == 400
    assert client.post("/v3/drafts", json={"study_id": "study_missing", "strategy": "shares"}).status_code == 400


# --- interrupted studies --------------------------------------------------------------------------------------------

def _orphan(service, sid, worker, heartbeat, created=None):
    now = C.utc_now()
    req = R.StudyIn(symbol="MSFT", horizon_days=5, thesis="swing")
    with service.db.tx() as cur:
        cur.execute("""INSERT INTO studies (id, mode, symbol, created_at, as_of, horizon_days, thesis, status, engine_version,
                       contract_version, inputs_json, evidence_json, fingerprint, worker, heartbeat_at)
                       VALUES (?, ?, 'MSFT', ?, ?, 5, 'swing', 'running', ?, ?, ?, '{}', '', ?, ?)""",
                    (sid, service.market.info.mode, created or now, now, R.ENGINE_VERSION, C.CONTRACT_VERSION, req.model_dump_json(), worker, heartbeat))


def test_a_study_left_running_by_a_stopped_process_is_failed_and_can_be_run_again(svc):
    service, client = svc
    r = service.research3
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    _orphan(service, "study_gone_elsewhere", "another-mac:123", old)                  # another computer, silent for 30 minutes
    _orphan(service, "study_dead_here", f"{r.host}:999999", C.utc_now())               # this computer, its process gone
    _orphan(service, "study_legacy", None, None, created=old)                          # 3.0: no worker recorded
    _orphan(service, "study_alive_here", f"{r.host}:{os.getppid()}", old)              # another live process here: its own
    _orphan(service, "study_fresh_elsewhere", "another-mac:5", C.utc_now())            # beating a moment ago
    failed = set(r.sweep())
    assert failed == {"study_gone_elsewhere", "study_dead_here", "study_legacy"}
    s = client.get("/v3/studies/study_dead_here").json()
    assert s["status"] == "failed" and s["error"].startswith("Interrupted") and s["retryable"]
    for keep in ("study_alive_here", "study_fresh_elsewhere"):
        assert client.get(f"/v3/studies/{keep}").json()["status"] == "running"
    again = client.post("/v3/studies/study_dead_here/retry").json()
    assert again["status"] == "running" and again["retry_of"] == "study_dead_here" and again["id"] != "study_dead_here"
    row = service.db.one("SELECT symbol, retry_of, worker FROM studies WHERE id = ?", (again["id"],))
    assert row["symbol"] == "MSFT" and row["retry_of"] == "study_dead_here" and row["worker"] == r.worker
    assert client.post("/v3/studies/study_alive_here/retry").status_code == 400          # still running: not retried
    end = time.time() + 90
    while time.time() < end and client.get(f"/v3/studies/{again['id']}").json()["status"] == "running":
        time.sleep(0.2)
    assert client.get(f"/v3/studies/{again['id']}").json()["status"] in ("complete", "partial")


def test_a_running_study_beats_and_is_not_failed_by_its_own_process(svc):
    service, _ = svc
    got = run_study(service, symbol="KO")
    row = service.db.one("SELECT worker, heartbeat_at, status FROM studies WHERE id = ?", (got["id"],))
    assert row["worker"] == service.research3.worker and row["heartbeat_at"] and row["status"] in ("complete", "partial")


# --- exits: software-monitored, and said so ---------------------------------------------------------------------------

from tests.test_autopilot import ap as _ap_fixture  # noqa: E402 - the autopilot's fixture, used by name below
from tests.test_autopilot import candidate, policy, run_jobs  # noqa: E402

ap = _ap_fixture


@pytest.mark.clock("2026-10-22T10:30")
def test_autopilot_exits_are_labelled_software_monitored_and_an_unquoted_stop_is_reported_not_skipped(ap, monkeypatch):
    service, pilot, client = ap
    st = pilot.status()["exit_protection"]
    assert st["kind"] == "software_monitored" and "not stop orders at the broker" in st["text"] and st["check_seconds"] == 300
    policy(service)
    candidate(service, stop=20.0, target=40.0)
    pilot.run()
    run_jobs(service)
    pos = client.get("/v3/desk/positions").json()
    row = next(p for p in pos["positions"] if p["symbol"] == "AMZN")
    assert row["exit"]["managed_by"] == "autopilot" and row["exit"]["protection"] == "software_monitored" and row["exit"]["stop"] == 20.0
    assert pos["exit_protection"]["kind"] == "software_monitored"
    # With no quote, the stop is not checked, and the run says so instead of passing over it in silence.
    real = service.trading.positions

    def no_marks(kind=None, marks=True):
        out = real(kind, marks=marks)
        for p in out["positions"]:
            p["mark"] = None
        return out
    monkeypatch.setattr(service.trading, "positions", no_marks)
    out = pilot._exits(service.trading.policy(), datetime.now(timezone.utc).astimezone())
    assert out and out[0]["status"] == "unchecked" and "not checked: no quote" in out[0]["why"]


@pytest.mark.clock("2026-10-22T10:30")
def test_a_stop_gapped_through_and_a_late_time_exit_are_reported(ap, monkeypatch):
    service, pilot, _ = ap
    policy(service)
    candidate(service, stop=40.0, target=90.0)                  # a stop far above the price: gapped through on the next run
    pilot.run()
    run_jobs(service)
    out = pilot._exits(service.trading.policy(), datetime.now(timezone.utc).astimezone())
    assert len(out) == 1 and "gapped through the stop" in out[0]["why"]


def test_positions_you_hold_are_yours_to_exit(svc):
    _, client = svc
    pos = client.get("/v3/desk/positions").json()
    assert "exit_protection" in pos and all(p["exit"]["managed_by"] == "you" for p in pos["positions"])


# --- the workstation's state ------------------------------------------------------------------------------------------

def test_the_session_keeps_which_layouts_it_has_seen(svc):
    _, client = svc
    body = {"tabs": [], "active": None, "dock": {}, "context": {"blue": {"symbol": "AMD", "draft": {"kind": "draft_trade"}}}, "layouts_version": 31}
    assert client.post("/v3/workspaces/session", json=body).status_code == 200
    back = client.get("/v3/workspaces").json()["session"]
    assert back["layouts_version"] == 31 and back["context"]["blue"]["draft"]["kind"] == "draft_trade"


def test_the_status_bar_knows_the_kill_switch(svc):
    service, client = svc
    assert client.get("/v3/status").json()["risk"]["kill_switch"] is False
    service.trading.set_kill_switch(True, "test")
    risk = client.get("/v3/status").json()["risk"]
    assert risk["kill_switch"] is True and risk["level"] == "fail"


def test_nothing_in_31_loosens_a_trading_permission():
    pol = P.TradingPolicy()
    assert pol.instruments == ["equity"] and pol.sides == ["buy", "sell"] and pol.mode == "paper" and pol.account_kind == "paper"
    src = (Path(DR.__file__)).read_text()
    assert "desk.create(" not in src and ".create(ProposalIn" not in src          # a draft never writes a proposal itself
    with pytest.raises(RequestError):
        DR.build(SimpleNamespace(research3=None), DR.DraftIn(study_id="x", strategy="shares"))


def test_the_migration_adds_the_study_heartbeat_columns(tmp_path):
    db = Database(tmp_path / "m.db")
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(studies)").fetchall()}
    assert {"worker", "heartbeat_at", "retry_of"} <= cols
    db.close()


def test_the_draft_module_is_documented_and_exported():
    assert set(DR.__all__) >= {"DraftIn", "build", "route", "routes_for"} and "research" in DR.ROUTE_WORDS
    assert json.dumps(DR.ROUTE_WORDS)
