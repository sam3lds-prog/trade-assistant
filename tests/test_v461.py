"""4.6.1: a MiroFish run that finished its rounds is closed so it reports completion, and the time cap counts from the
approval, not from before it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from tradeassist.v3.pipeline import cap_seconds
from tradeassist.v3.scenario_providers import MiroFish


class Resp:
    def __init__(self, body):
        self.status_code, self._body = 200, body

    def json(self):
        return self._body


class Server:
    """run-status as the pinned MiroFish reports it after its last round: every platform logged simulation_end, but
    the process waits for commands, so runner_status stays "running" until the environment is closed."""

    def __init__(self, closes_work=True):
        self.calls, self.closed, self.closes_work = [], False, closes_work

    def request(self, method, url, timeout=None, **kw):
        path = urlparse(url).path
        self.calls.append((method, path))
        if path.endswith("/run-status"):
            status = "completed" if self.closed else "running"
            return Resp({"success": True, "data": {"runner_status": status, "current_round": 12, "total_rounds": 12,
                                                   "twitter_completed": True, "reddit_completed": True}})
        if path == "/api/simulation/close-env":
            self.closed = self.closes_work
            return Resp({"success": True, "data": {"success": True}})
        if path == "/api/simulation/stop":
            self.closed = True
            return Resp({"success": True, "data": {}})
        return Resp({"success": False, "error": "no route"})


def _mf(server):
    return MiroFish("http://127.0.0.1:5001", session=server, poll_first=0.0, poll_most=0.0)


def test_a_finished_run_waiting_for_commands_is_closed_and_goes_on_to_the_report():
    srv = Server()
    mf = _mf(srv)
    st = {"stage": "run_wait", "simulation_id": "sim_1", "started": True}
    step = mf._run_wait({}, st)
    assert ("POST", "/api/simulation/close-env") in srv.calls and "closing" in step.detail
    step = mf._run_wait({}, step.state)
    assert step.state["stage"] == "report" and not step.state["stopped_early"]
    assert ("POST", "/api/simulation/stop") not in srv.calls


def test_if_closing_does_not_end_it_the_run_is_stopped_and_still_counts_as_finished():
    srv = Server(closes_work=False)
    mf = _mf(srv)
    st = {"stage": "run_wait", "simulation_id": "sim_1", "started": True}
    for _ in range(4):
        st = mf._run_wait({}, st).state
    assert ("POST", "/api/simulation/stop") in srv.calls
    step = mf._run_wait({}, st)
    assert step.state["stage"] == "report" and not step.state["stopped_early"]


def test_a_run_still_in_its_rounds_is_not_closed():
    class Mid(Server):
        def request(self, method, url, timeout=None, **kw):
            if urlparse(url).path.endswith("/run-status"):
                self.calls.append((method, urlparse(url).path))
                return Resp({"success": True, "data": {"runner_status": "running", "current_round": 5, "total_rounds": 12,
                                                       "twitter_completed": True, "reddit_completed": False}})
            return super().request(method, url, timeout, **kw)
    srv = Mid()
    step = _mf(srv)._run_wait({}, {"stage": "run_wait", "simulation_id": "sim_1", "started": True})
    assert ("POST", "/api/simulation/close-env") not in srv.calls and step.state["stage"] == "run_wait"


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_the_time_cap_clock_starts_at_the_approval():
    now = datetime.now(timezone.utc)
    ext = {"started_at": _iso(now - timedelta(minutes=30)), "clock_from": _iso(now - timedelta(minutes=2)), "paused_seconds": 0.0}
    assert 115 <= cap_seconds(ext, now) <= 125
    # Before any approval: from the start, less any time paused.
    ext = {"started_at": _iso(now - timedelta(minutes=10)), "paused_seconds": 120.0}
    assert 475 <= cap_seconds(ext, now) <= 485
    # Paused now (waiting for an approval): the wait so far is not counted.
    ext = {"started_at": _iso(now - timedelta(minutes=10)), "paused_at": _iso(now - timedelta(minutes=4))}
    assert 355 <= cap_seconds(ext, now) <= 365
    assert cap_seconds({}, now) is None
