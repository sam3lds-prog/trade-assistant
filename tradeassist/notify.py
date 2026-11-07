"""The Trading Desk's notifications (3.0 phase 5): an outbox delivered to email, ntfy and Telegram.

Every message is first written to `notifications` (the outbox), then delivered by the job worker to each channel
that is set up; a channel that fails is tried again with growing waits (30 seconds, 2, 8 and 30 minutes) while the
others keep their result. A message with a `dedupe_key` is written once, so a restart never sends it twice.

Channels, each switched on by its settings in .env:

- **email**: SMTP_USER and SMTP_PASSWORD (as the morning plan), unless NOTIFY_EMAIL=0
- **ntfy**: NTFY_TOPIC (a long random name: anyone who knows it can read it), NTFY_SERVER (default ntfy.sh),
  NTFY_TOKEN for a protected topic
- **Telegram**: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

An approval link in a message is useless without the PIN of the approval pages (remote.py), and expires with the
approval.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from . import mailer
from .events import redact

log = logging.getLogger(__name__)

BACKOFF_SECONDS = (30, 120, 480, 1800)
TIMEOUT = 10
PRIORITY = {"info": "default", "warn": "high", "critical": "urgent"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


class Notifier:
    def __init__(self, svc: Any, session: Any = None):
        self.svc = svc
        self.settings = svc.settings
        self.db = svc.db
        self.http = session or requests.Session()

    # --- channels ---------------------------------------------------------------------------------------------
    def channels(self) -> list[str]:
        s = self.settings
        out = []
        if s.notify_email and mailer.configured(s):
            out.append("email")
        if s.ntfy_topic:
            out.append("ntfy")
        if s.telegram_bot_token and s.telegram_chat_id:
            out.append("telegram")
        return out

    def _scrub(self, text: str) -> str:
        out = redact(text)
        for secret in (self.settings.telegram_bot_token, self.settings.ntfy_token, self.settings.smtp_password):
            if secret:
                out = out.replace(secret, "[secret]")
        return out[:300]

    def _link(self, n: dict[str, Any]) -> str | None:
        """An approval link gets its one-time token only here, as it is sent; the outbox keeps the link without it."""
        link = n.get("link")
        m = re.search(r"/remote/a/(appr_[A-Za-z0-9]+)$", link or "")
        if m:
            from .v3.trading import approval_token
            link = f"{link}?t={approval_token(self.settings, m.group(1))}"
        return link

    def _send(self, channel: str, n: dict[str, Any]) -> None:
        s = self.settings
        n = {**n, "link": self._link(n)}
        text = n["body"] + (f"\n\n{n['link']}" if n.get("link") else "")
        if channel == "email":
            mailer.send(s, f"Trade Assistant: {n['subject']}", text)
        elif channel == "ntfy":
            headers = {"Title": n["subject"].encode("utf-8", "replace").decode("latin-1", "replace"),
                       "Priority": PRIORITY.get(n["severity"], "default"), "Tags": "chart_with_upwards_trend"}
            if n.get("link"):
                headers["Click"] = n["link"]
            if s.ntfy_token:
                headers["Authorization"] = f"Bearer {s.ntfy_token}"
            r = self.http.post(f"{s.ntfy_server}/{s.ntfy_topic}", data=n["body"].encode("utf-8"), headers=headers, timeout=TIMEOUT)
            if r.status_code >= 300:
                raise RuntimeError(f"ntfy answered {r.status_code}")
        elif channel == "telegram":
            r = self.http.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
                               json={"chat_id": s.telegram_chat_id, "text": f"{n['subject']}\n\n{text}", "disable_web_page_preview": True},
                               timeout=TIMEOUT)
            if r.status_code >= 300:
                raise RuntimeError(f"Telegram answered {r.status_code}")
        else:
            raise RuntimeError(f"unknown channel {channel}")

    # --- the outbox ---------------------------------------------------------------------------------------------
    def enqueue(self, kind: str, subject: str, body: str, *, severity: str = "info", link: str | None = None,
                proposal_id: str | None = None, dedupe_key: str | None = None) -> int | None:
        chans = self.channels()
        now = _iso(_now())
        with self.db.tx() as cur:
            cur.execute("""INSERT OR IGNORE INTO notifications (created_at, kind, severity, subject, body, link, proposal_id, dedupe_key,
                           status, attempts, next_attempt_at, channels_json, results_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, '{}')""",
                        (now, kind, severity, subject[:200], body[:4000], link, proposal_id, dedupe_key,
                         "queued" if chans else "suppressed", now, json.dumps(chans)))
            nid = int(cur.lastrowid) if cur.rowcount else None
        if nid:
            from .bus import bus_for
            bus_for(self.svc).publish("notification.queued" if chans else "notification.suppressed", str(nid), kind=kind, severity=severity, title=subject[:200])
        return nid

    def deliver_due(self, payload: dict[str, Any] | None = None) -> dict[str, int]:
        rows = self.db.query("SELECT * FROM notifications WHERE status IN ('queued', 'partial') AND next_attempt_at <= ? ORDER BY id LIMIT 20",
                             (_iso(_now()),))
        sent = failed = 0
        for r in rows:
            n = dict(r)
            results = json.loads(n["results_json"] or "{}")
            chans = json.loads(n["channels_json"] or "[]")
            for ch in chans:
                if (results.get(ch) or {}).get("ok"):
                    continue
                try:
                    self._send(ch, n)
                    results[ch] = {"ok": True, "at": _iso(_now())}
                except Exception as exc:  # noqa: BLE001 - recorded per channel and retried
                    results[ch] = {"ok": False, "at": _iso(_now()), "error": self._scrub(f"{exc.__class__.__name__}: {exc}")}
                    log.warning("Notification %s by %s failed: %s", n["id"], ch, results[ch]["error"])
            oks = [ch for ch in chans if (results.get(ch) or {}).get("ok")]
            attempts = n["attempts"] + 1
            if len(oks) == len(chans):
                status, nxt = "sent", None
                sent += 1
            elif attempts > len(BACKOFF_SECONDS):            # out of retries: what reached a channel stays partial
                status, nxt = ("partial" if oks else "failed"), None
                failed += 1
            else:
                status, nxt = ("partial" if oks else "queued"), _iso(_now() + timedelta(seconds=BACKOFF_SECONDS[attempts - 1]))
            with self.db.tx() as cur:
                cur.execute("UPDATE notifications SET status = ?, attempts = ?, next_attempt_at = ?, results_json = ?, sent_at = ? WHERE id = ?",
                            (status, attempts, nxt, json.dumps(results), _iso(_now()) if oks else None, n["id"]))
        return {"sent": sent, "failed": failed, "looked_at": len(rows)}

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)):
            d = dict(r)
            d["channels"] = json.loads(d.pop("channels_json") or "[]")
            d["results"] = json.loads(d.pop("results_json") or "{}")
            d.pop("dedupe_key", None)
            if d.get("link") and "t=" in d["link"]:
                d["link"] = d["link"].split("?")[0]           # the approval token is only in the message itself
            out.append(d)
        return out

    def describe(self) -> dict[str, Any]:
        s = self.settings
        return {"channels": self.channels(),
                "set_up": {"email": mailer.configured(s), "ntfy": bool(s.ntfy_topic), "telegram": bool(s.telegram_bot_token and s.telegram_chat_id)},
                "email_switched_off": mailer.configured(s) and not s.notify_email}


__all__ = ["Notifier", "BACKOFF_SECONDS"]
