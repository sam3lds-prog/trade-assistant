"""The Trading Desk's written policy (3.0 phase 5): what it may trade, how much, when, and who must say yes.

One policy is active at a time (`trading_policies`, the row with `enabled = 1`). It is edited on the Trading Desk
and every change is versioned and written to the audit log. The defaults are a small trial: $500 of capital, $50 an
order, $100 of new buying a day, a $25 daily loss stop, three positions, long shares only, limit orders only, inside
regular hours, every order approved by you.

Modes:

| mode | what happens |
| --- | --- |
| disabled | proposals can be written and checked; nothing is sent |
| paper | approved orders go to the paper broker (simulated fills against the quote) |
| approval_required | approved orders go to the live broker (phase 6; off unless enabled in .env) |
| capped_autopilot | the autopilot may approve within the caps (phase 7); you can still approve by hand |
"""

from __future__ import annotations

import json
from datetime import time as dtime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import contracts as C

MODES = ("disabled", "paper", "approval_required", "capped_autopilot")
MODE_WORDS = {"disabled": "Off: nothing is sent", "paper": "Paper: simulated fills",
              "approval_required": "Live, each order approved by you", "capped_autopilot": "Capped autopilot"}


class TradingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="trial", min_length=1, max_length=60)
    mode: C.OperatingMode = "paper"
    account_kind: C.AccountKind = "paper"
    # money
    trial_capital: float = Field(default=500.0, gt=0, le=1_000_000)
    max_order: float = Field(default=50.0, gt=0, le=1_000_000)
    max_daily_new_buys: float = Field(default=100.0, gt=0, le=1_000_000)
    max_daily_loss: float = Field(default=25.0, gt=0, le=1_000_000)
    max_open_positions: int = Field(default=3, ge=1, le=100)
    # what
    instruments: list[C.Instrument] = Field(default_factory=lambda: ["equity"])
    sides: list[C.Side] = Field(default_factory=lambda: ["buy", "sell"])       # long only: buy to open, sell to close
    order_types: list[C.OrderType] = Field(default_factory=lambda: ["limit"])
    allow_extended_hours: bool = False
    symbols_allowed: list[str] = Field(default_factory=list, max_length=500)   # empty: any stock
    symbols_blocked: list[str] = Field(default_factory=list, max_length=500)
    # market conditions
    max_spread_bps: float = Field(default=50.0, gt=0, le=5000)
    max_quote_age_seconds: float = Field(default=20.0, gt=0, le=3600)
    max_limit_through_bps: float = Field(default=10.0, ge=0, le=500)          # a buy's limit above the ask, a sell's below the bid
    window_start: str = "09:35"
    window_end: str = "15:50"
    # time to decide
    approval_minutes: int = Field(default=10, ge=1, le=240)
    proposal_minutes: int = Field(default=60, ge=5, le=1440)
    # the autopilot (phase 7)
    autopilot_max_orders_per_day: int = Field(default=2, ge=0, le=50)
    autopilot_min_score: float = Field(default=60.0, ge=0, le=100)
    autopilot_horizons: list[C.Horizon] = Field(default_factory=lambda: ["swing"])
    autopilot_shadow_days: int = Field(default=10, ge=0, le=250)              # shadow decisions needed before it may act
    notes: str = Field(default="", max_length=1000)

    @field_validator("symbols_allowed", "symbols_blocked")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return sorted({s.strip().upper() for s in v if s and s.strip()})

    @field_validator("window_start", "window_end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        hh, mm = (int(x) for x in v.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError("times are HH:MM in New York")
        return f"{hh:02d}:{mm:02d}"

    @model_validator(mode="after")
    def _consistent(self) -> "TradingPolicy":
        if self.mode == "paper" and self.account_kind != "paper":
            raise ValueError("paper mode trades the paper account")
        if self.mode == "approval_required" and self.account_kind != "live":
            raise ValueError("approval_required is the live account, each order approved; use paper mode for the paper account")
        if self.max_order > self.trial_capital:
            raise ValueError("the largest order cannot be more than the capital")
        if self.window(True) >= self.window(False):
            raise ValueError("the trading window must start before it ends")
        if not self.instruments or not self.sides or not self.order_types:
            raise ValueError("allow at least one instrument, one side and one order type")
        return self

    def window(self, start: bool) -> dtime:
        hh, mm = (int(x) for x in (self.window_start if start else self.window_end).split(":"))
        return dtime(hh, mm)

    def summary(self) -> str:
        return (f"{MODE_WORDS[self.mode]}. ${self.trial_capital:,.0f} of capital, ${self.max_order:,.0f} an order, "
                f"${self.max_daily_new_buys:,.0f} of new buying a day, stop after a ${self.max_daily_loss:,.0f} loss in a day, "
                f"{self.max_open_positions} positions at most; {', '.join(self.instruments)} {'/'.join(self.sides)} "
                f"{'/'.join(self.order_types)} orders, {self.window_start} to {self.window_end} New York.")


def _row_to(r: Any) -> dict[str, Any]:
    body = json.loads(r["body_json"])
    return {"id": r["id"], "version": r["version"], "enabled": bool(r["enabled"]), "updated_at": r["updated_at"],
            "policy": TradingPolicy(**body).model_dump()}


def active(db: Any) -> dict[str, Any]:
    """The policy in force, created with the trial defaults the first time it is asked for."""
    r = db.one("SELECT * FROM trading_policies WHERE enabled = 1 ORDER BY updated_at DESC LIMIT 1")
    if r is None:
        p = TradingPolicy()
        now = C.utc_now()
        pid = C.new_id("pol")
        with db.tx() as cur:
            cur.execute("INSERT OR IGNORE INTO trading_policies (id, name, mode, body_json, version, enabled, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, 1, 1, ?, ?)", (pid, p.name, p.mode, p.model_dump_json(), now, now))
            cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, 'app', 'policy.created', ?, ?)",
                        (now, pid, p.model_dump_json()))
        r = db.one("SELECT * FROM trading_policies WHERE enabled = 1 ORDER BY updated_at DESC LIMIT 1")
    return _row_to(r)


def current(db: Any) -> TradingPolicy:
    return TradingPolicy(**active(db)["policy"])


def save(db: Any, body: dict[str, Any], actor: str = "you", *, live_allowed: bool = False) -> dict[str, Any]:
    """Replace the active policy's terms (a new version). Live trading is refused unless the live adapter is enabled."""
    p = TradingPolicy(**body)
    if p.account_kind == "live" and not live_allowed:
        raise ValueError("Live trading is not enabled on this computer. It needs ROBINHOOD_EXECUTION=1 and the account "
                         "named in .env (phase 6); until then use paper mode.")
    cur_row = active(db)
    now = C.utc_now()
    with db.tx() as cur:
        cur.execute("UPDATE trading_policies SET name = ?, mode = ?, body_json = ?, version = version + 1, updated_at = ? WHERE id = ?",
                    (p.name, p.mode, p.model_dump_json(), now, cur_row["id"]))
        cur.execute("INSERT INTO audit_log (at, actor, action, subject, data_json) VALUES (?, ?, 'policy.changed', ?, ?)",
                    (now, actor, cur_row["id"], json.dumps({"from": cur_row["policy"], "to": p.model_dump()})))
    return active(db)


__all__ = ["TradingPolicy", "MODES", "MODE_WORDS", "active", "current", "save"]
