"""3.0 phase 5: the durable job queue (leases, retries, stale work discarded after a sleep), the daemon and the app's
worker handing work to each other, the notification outbox, and the PIN-protected phone approval pages."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import time_machine
from fastapi.testclient import TestClient

from tradeassist import daemon, jobs as J
from tradeassist.api import create_app
from tradeassist.config import Settings
from tradeassist.db import Database
from tradeassist.notify import Notifier
from tradeassist.optionmath import NY
from tradeassist.providers.demo import DemoData
from tradeassist.remote import COOKIE, MAX_FAILS, create_remote_app
from tradeassist.service import TradeAssistant
from tradeassist.v3.trading import ProposalIn


def settings(tmp_path, **kw):
    base = dict(data_mode="demo", db_path=tmp_path / "t.db", desk_worker="off")
    return Settings(**{**base, **kw})


@pytest.fixture
def svc(tmp_path):
    s = settings(tmp_path)
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    create_app(s, service)
    yield service
    service.db.close()


# --- the queue -----------------------------------------------------------------------------------------------------

def test_a_job_is_queued_once_claimed_under_a_lease_and_finished(svc):
    q = J.Jobs(svc.db)
    a = q.enqueue("x", {"n": 1}, dedupe_key="x:1")
    assert a and q.enqueue("x", {"n": 1}, dedupe_key="x:1") is None
    job = q.claim("w1")
    assert job["id"] == a and job["payload"] == {"n": 1} and job["attempts"] == 1
    assert q.claim("w2") is None                                   # leased
    q.finish(a, {"ok": True})
    assert q.counts() == {"done": 1}


def test_a_failed_job_is_retried_with_growing_waits_then_given_up(svc):
    q = J.Jobs(svc.db)
    jid = q.enqueue("x", max_attempts=2)
    q.claim("w")
    assert q.fail(jid, "boom", retry_seconds=10) == "queued"
    assert q.claim("w") is None                                    # not due for 10 seconds
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(seconds=11), tick=True):
        q.claim("w")
        assert q.fail(jid, "boom again") == "failed"


def test_a_job_whose_worker_died_goes_back_on_the_queue(svc):
    q = J.Jobs(svc.db)
    jid = q.enqueue("x")
    q.claim("dead", lease_seconds=5)
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(seconds=6), tick=True):
        job = q.claim("alive")
    assert job["id"] == jid and job["attempts"] == 2 and job["worker"].startswith("alive")


def test_after_a_sleep_missed_slots_are_not_replayed(svc):
    q = J.Jobs(svc.db)
    s = J.Schedule("tick", 60, 90)
    start = datetime(2026, 10, 22, 14, 0, 10, tzinfo=timezone.utc)
    assert len(J.schedule_tick(q, [s], start)) == 1
    assert J.schedule_tick(q, [s], start + timedelta(seconds=20)) == []          # the same slot
    with time_machine.travel(start + timedelta(hours=5), tick=False):            # the computer slept through five hours
        J.schedule_tick(q, [s])
        ran = []
        w = J.Worker(svc.db, {"tick": lambda p: ran.append(1)}, component="t")
        w.drain()
    rows = {r["status"]: r for r in svc.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    assert rows["discarded"]["n"] == 1 and rows["done"]["n"] == 1 and ran == [1]     # the old slot dropped, the current one run once


def test_market_hours_schedules_wait_for_the_session(svc):
    s = J.Schedule("scan", 1800, 600, when="market")
    assert not s.due(datetime(2026, 10, 24, 15, 0, tzinfo=NY))                 # a Saturday
    assert not s.due(datetime(2026, 10, 22, 8, 0, tzinfo=NY))
    assert s.due(datetime(2026, 10, 22, 11, 0, tzinfo=NY))


def test_the_apps_worker_steps_aside_while_the_daemon_runs(svc):
    ran = []
    w = J.Worker(svc.db, {"x": lambda p: ran.append(p)}, component="app-worker", defer_to="daemon")
    J.Jobs(svc.db).enqueue("x", {"n": 1})
    J.heartbeat(svc.db, "daemon", paper=True)
    assert w.step() == 0 and ran == []
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(seconds=60), tick=True):     # the daemon went quiet
        assert w.step() == 1 and ran == [{"n": 1}]
    assert J.last_heartbeat(svc.db, "app-worker") is not None


def test_the_daemon_recovers_schedules_and_beats(tmp_path):
    s = settings(tmp_path)
    assert daemon.run(s, once=True) == 0
    db = Database(next(tmp_path.glob("t*.db")))
    hb = J.last_heartbeat(db, "daemon")
    assert hb and hb["info"]["paper"] is True and hb["info"]["remote"] is False
    kinds = {r["kind"] for r in db.query("SELECT kind FROM jobs")}
    assert {"desk.reconcile", "desk.expire", "notify.deliver", "paper.tick", "jobs.prune"} <= kinds
    db.close()


@pytest.mark.clock("2026-10-22T10:30")
def test_the_apps_paper_portfolios_are_left_to_a_running_daemon(svc):
    clock = svc.clockwork
    J.heartbeat(svc.db, "daemon", paper=True)
    assert clock._daemon_values_paper()
    J.heartbeat(svc.db, "daemon", paper=False)
    assert not clock._daemon_values_paper()


# --- notifications -------------------------------------------------------------------------------------------------

class FakeHTTP:
    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "json": json, "headers": headers or {}})
        host = "telegram" if "telegram" in url else "ntfy"
        if host in self.fail:
            raise ConnectionError(f"refused by {url}")
        return type("R", (), {"status_code": 200})()


def notifier(tmp_path, http, **kw):
    s = settings(tmp_path, ntfy_topic="ta-test-topic-9f3", telegram_bot_token="123:SECRETTOKEN", telegram_chat_id="42", **kw)
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "n.db"))
    return Notifier(service, session=http), service


def test_without_a_channel_a_notification_is_kept_but_not_sent(svc):
    n = Notifier(svc, session=FakeHTTP())
    nid = n.enqueue("test", "Hello", "Body")
    assert svc.db.one("SELECT status FROM notifications WHERE id = ?", (nid,))["status"] == "suppressed"


def test_each_channel_is_sent_once_and_a_failing_one_is_retried_alone(tmp_path):
    http = FakeHTTP(fail={"telegram"})
    n, service = notifier(tmp_path, http)
    assert n.channels() == ["ntfy", "telegram"]
    nid = n.enqueue("proposal.approval", "Approve? Buy 1 AMZN", "Body", link="http://mac.ts.net:8765/remote/a/appr_1?t=tok", dedupe_key="a:1")
    assert n.enqueue("proposal.approval", "Approve? Buy 1 AMZN", "Body", dedupe_key="a:1") is None
    n.deliver_due()
    row = service.db.one("SELECT * FROM notifications WHERE id = ?", (nid,))
    assert row["status"] == "partial"
    ntfy = http.calls[0]
    assert ntfy["url"] == "https://ntfy.sh/ta-test-topic-9f3" and ntfy["headers"]["Click"].endswith("t=tok") and ntfy["headers"]["Title"]
    assert "SECRETTOKEN" not in row["results_json"]                                # the bot token never reaches the record
    http.fail.clear()
    calls = len(http.calls)
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(seconds=31), tick=True):
        n.deliver_due()
    assert [c["url"] for c in http.calls[calls:]] == ["https://api.telegram.org/bot123:SECRETTOKEN/sendMessage"]   # ntfy not sent twice
    assert service.db.one("SELECT status FROM notifications WHERE id = ?", (nid,))["status"] == "sent"
    assert "t=" not in (n.recent()[0]["link"] or "")                               # the token stays in the message only
    service.db.close()


def test_a_channel_that_keeps_failing_is_given_up_after_its_retries(tmp_path):
    http = FakeHTTP(fail={"telegram", "ntfy"})
    n, service = notifier(tmp_path, http)
    nid = n.enqueue("x", "s", "b")
    t = datetime.now(timezone.utc)
    for i in range(6):
        with time_machine.travel(t + timedelta(hours=i), tick=True):
            n.deliver_due()
    row = service.db.one("SELECT status, attempts, next_attempt_at FROM notifications WHERE id = ?", (nid,))
    assert row["status"] == "failed" and row["attempts"] == 5 and row["next_attempt_at"] is None
    service.db.close()


# --- phone approvals -------------------------------------------------------------------------------------------------

PIN = "246810"


@pytest.fixture
def phone(tmp_path):
    s = settings(tmp_path, remote_approvals=True, remote_pin=PIN, remote_bind="100.64.0.5", remote_hosts=["mac.tail1234.ts.net"],
                 remote_base_url="http://mac.tail1234.ts.net:8765")
    service = TradeAssistant(s, provider=DemoData(), db=Database(tmp_path / "t.db"))
    create_app(s, service)
    from tradeassist import policy as P
    P.save(service.db, {**P.current(service.db).model_dump(), "trial_capital": 100_000, "max_order": 5_000, "max_daily_new_buys": 20_000})
    client = TestClient(create_remote_app(s, service), base_url="http://mac.tail1234.ts.net")
    yield client, service
    service.db.close()


def sign_in(client):
    r = client.post("/remote/login", data={"pin": PIN, "next": "/remote"}, follow_redirects=False)
    assert r.status_code == 303 and COOKIE in r.cookies
    return r


def test_only_the_tailnet_name_and_only_the_approval_pages_answer(phone, tmp_path):
    client, service = phone
    assert client.get("/remote").status_code == 401                                # the PIN first
    other = TestClient(client.app, base_url="http://evil.example")
    assert other.get("/remote").status_code == 400                                 # not one of its host names
    sign_in(client)
    for path in ("/v3/proposals", "/api/status", "/v3/desk/policy", "/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (303, 404) and (r.status_code == 404 or r.headers["location"] == "/remote")
    h = client.get("/remote").headers
    assert "script-src" not in h["content-security-policy"] and h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"


@pytest.mark.clock("2026-10-22T10:30")
def test_a_phone_approves_with_the_pin_and_the_link_once(phone):
    client, service = phone
    desk = service.trading
    p = desk.create(ProposalIn(symbol="AMZN", quantity=1, thesis="phone test"))
    asked = desk._ask_approval(p["id"], notify=False)
    link = asked["link"]
    assert link.startswith("http://mac.tail1234.ts.net:8765/remote/a/") and "t=" in link
    path = link.split("8765", 1)[1]
    assert client.get(path).status_code == 401                                     # no session yet
    sign_in(client)
    assert client.get(path.replace(asked["token"], "wrong")).status_code == 404
    page = client.get(path).text
    assert "Buy 1 share of AMZN" in page and "Approve" in page and p["order_hash"][:12] in page
    r = client.post(path.split("?")[0], data={"t": asked["token"], "order_hash": p["order_hash"], "action": "approve"})
    assert r.status_code == 200 and "Approved" in r.text
    assert desk.get(p["id"])["status"] == "authorized" and desk.get(p["id"])["authorization"]["channel"] == "remote"
    again = client.post(path.split("?")[0], data={"t": asked["token"], "order_hash": p["order_hash"], "action": "approve"})
    assert again.status_code == 409 and "already approved" in again.text


@pytest.mark.clock("2026-10-22T10:30")
def test_a_form_from_another_site_is_refused(phone):
    client, service = phone
    sign_in(client)
    r = client.post("/remote/kill", headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and not service.trading.kill_switch().get("on")
    assert client.post("/remote/kill").status_code == 200 and service.trading.kill_switch()["on"]


def test_wrong_pins_lock_the_pages_and_say_so(phone):
    client, service = phone
    for _ in range(MAX_FAILS):
        assert client.post("/remote/login", data={"pin": "000000"}).status_code == 401
    r = client.post("/remote/login", data={"pin": PIN}, follow_redirects=False)
    assert r.status_code == 401 and "Locked" in r.text                           # even the right PIN, for now
    assert service.db.one("SELECT COUNT(*) AS n FROM notifications WHERE kind = 'remote.locked'")["n"] == 1


@pytest.mark.clock("2026-10-22T10:30")
def test_a_lost_link_is_replaced_and_the_old_one_stops_working(phone):
    client, service = phone
    desk = service.trading
    p = desk.create(ProposalIn(symbol="AMZN", quantity=1))
    first = desk._ask_approval(p["id"], notify=False)
    sign_in(client)
    assert "Send me a new link" in client.get("/remote").text
    assert client.post(f"/remote/p/{p['id']}/link").status_code == 200
    r = client.post(f"/remote/a/{first['id']}", data={"t": first["token"], "order_hash": p["order_hash"], "action": "approve"})
    assert r.status_code == 409 and "superseded" in r.text
    assert service.db.one("SELECT COUNT(*) AS n FROM notifications WHERE kind = 'proposal.approval'")["n"] >= 2


def test_a_session_expires(phone):
    client, _ = phone
    sign_in(client)
    assert client.get("/remote").status_code == 200
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(hours=13), tick=True):
        assert client.get("/remote").status_code == 401
