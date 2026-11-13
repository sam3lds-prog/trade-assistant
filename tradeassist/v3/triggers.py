"""Catalyst triggers as evidence for review (4.5.1).

Before 4.5.1 a Bullish or Bearish case's trigger was recorded from what you typed (a title, a publication time, a
link) and at once counted as observed, which could turn a Watch for trigger into a Qualified setup. Now a submission
is **evidence submitted for review**, with a status:

| status | meaning |
| --- | --- |
| unverified | recorded, not (or not yet) verified: no link, or the source could not be read or gave no publication time |
| verification_pending | the source is being read |
| verified | the source was read and supports it (below) |
| rejected | the source contradicts it (published before the cutoff or after the window, about another stock, or not about the trigger) |
| expired | the trigger's window ended before it was verified |

Only **verified** counts: an unverified trigger never makes a setup qualified, and the setup stays a Watch.

**Verification is deterministic** and done by the app, never by a language model's reading:

1. the link is fetched (http or https only; never a loopback, private, link-local or reserved address, after each
   redirect too; at most 1 MB, 10 seconds);
2. the publication time is taken from **the source itself** (its article:published_time, og:published_time,
   datePublished, pubDate or `<time datetime>`), never from what was typed; without one it stays unverified;
3. that time must be after the study's evidence cutoff and no later than the trigger's deadline (the catalyst's
   session for a dated catalyst, else the last forecast session's close);
4. the text must name the stock (its ticker as a word in capitals, `$TICKER`, or `exchange: TICKER`);
5. one sentence of it must **affirm** the claimed event: it names the stock or holds most of what you typed, holds at
   least two of the case's trigger words, and is neither negated (*did not beat*), nor the opposite (*misses* for
   *beats*, *cuts* for *raises*), nor speculative (*preview*, *expected to*, *will*, a question). A page whose only
   matching sentences contradict the trigger is **rejected**; one whose only matching sentences are speculative stays
   **unverified**.

This is a conservative word-level check, not an understanding of the page: it can miss a real event worded
unusually (it then stays unverified, and the setup a Watch), and a cleverly worded page could still pass it. A
verification also belongs to the scenario version it was checked against: after a retry with new cases, the trigger
must be submitted again.

Every time is kept apart: the event (when you say it happened), the source's publication (and the one you typed), the
retrieval, the verification, your recording, and the forecast cutoff. A trigger recorded after the target session's
close (when the outcome was known) never counts toward the forward scorecard (v3/forward.py).
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import socket
from datetime import date, datetime, time as dtime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from .. import contracts as C
from ..optionmath import NY
from ..service import RequestError

log = logging.getLogger(__name__)

STATES = ("unverified", "verification_pending", "verified", "rejected", "expired")
# A trigger word and the words that say the opposite happened (stems; a word matches when it starts with one).
OPPOSITES = {"beat": ("miss", "fell short", "short of", "below"), "rais": ("cut", "lower", "reduc", "slash", "trim"),
             "increas": ("decreas", "declin", "reduc", "cut"), "upgrad": ("downgrad",), "approv": ("reject", "deni", "declin"),
             "win": ("lose", "lost", "loses"), "won": ("lose", "lost"), "gain": ("loss", "los", "fall", "fell", "drop"),
             "rise": ("fall", "fell", "drop", "declin"), "rose": ("fell", "drop", "declin"), "above": ("below",), "exceed": ("miss", "below"),
             "grow": ("shrink", "declin", "contract"), "strong": ("weak",), "positiv": ("negativ",), "accelerat": ("decelerat", "slow"),
             "expand": ("contract", "shrink"), "improv": ("worsen", "deteriorat"), "launch": ("delay", "cancel", "postpon"),
             "complet": ("terminat", "cancel", "abandon"), "sign": ("terminat", "cancel"), "award": ("lost", "lose")}
NEGATIONS = ("not", "no", "never", "didn't", "doesn't", "don't", "won't", "wasn't", "isn't", "fails", "failed", "unable", "without")
SPECULATIVE = ("preview", "expected", "expects", "expect", "will", "could", "may", "might", "should", "ahead", "forecast", "anticipat",
               "rumor", "rumour", "reportedly", "speculat", "if", "whether", "likely", "possible", "possibly", "plans", "plan")
VERIFY = "scenario.verify_trigger"
MAX_BYTES = 1_000_000
TIMEOUT = 10.0
MAX_REDIRECTS = 3
STOP = set("""a an and are as at be been but by for from has have in into is it its of on or that the their this to was were will with
after before over under about than then they them there these those what when which while would could should said says says new
company companies inc corp ltd share shares stock stocks report reports reported quarter quarterly results result year""".split())
META_TIMES = ("article:published_time", "og:published_time", "og:article:published_time", "datepublished", "publishdate", "pubdate",
              "dc.date.issued", "dc.date", "parsely-pub-date", "sailthru.date", "date")


class Unsafe(Exception):
    """The address may not be fetched."""


# --- fetching, safely ----------------------------------------------------------------------------------------------

def _public(host: str) -> str:
    """The host's address, when every address it resolves to is a global (public) one: never loopback, private,
    link-local, shared (100.64/10), reserved or multicast. The connection is then made to that very address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise Unsafe(f"{host} could not be resolved") from exc
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise Unsafe(f"{host} is not a public address")
        ips.append(str(ip))
    if not ips:
        raise Unsafe(f"{host} could not be resolved")
    return ips[0]


def check_url(url: str) -> str:
    return _checked(url)[0]


def _checked(url: str) -> tuple[str, str]:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or not u.hostname:
        raise Unsafe("only http and https links can be checked")
    if u.username or u.password:
        raise Unsafe("a link with a user name or password is not fetched")
    if u.port not in (None, 80, 443):
        raise Unsafe("only the standard web ports are fetched")
    return u.geturl(), _public(u.hostname)


def _pinned_get(url: str, ip: str) -> Any:
    """GET `url` by connecting to the address that was checked (not resolving the name again, which a DNS rebinding
    could point at this computer), with the host name kept for the Host header and the TLS certificate check."""
    import urllib3
    u = urlparse(url)
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    headers = {"Host": u.hostname, "User-Agent": "TradeAssistant/4.5 trigger check",
               "Accept": "text/html,application/xhtml+xml,application/xml,text/plain"}
    if u.scheme == "https":
        import certifi
        pool = urllib3.HTTPSConnectionPool(ip, port=port, server_hostname=u.hostname, assert_hostname=u.hostname, cert_reqs="CERT_REQUIRED",
                                           ca_certs=certifi.where(), timeout=urllib3.Timeout(total=TIMEOUT), retries=False)
    else:
        pool = urllib3.HTTPConnectionPool(ip, port=port, timeout=urllib3.Timeout(total=TIMEOUT), retries=False)
    r = pool.urlopen("GET", path, headers=headers, redirect=False, preload_content=False)

    class Reply:
        status_code = r.status
        headers_ = r.headers
        encoding = None

        def iter_content(self, n: int):
            yield from r.stream(n)

        def close(self) -> None:
            r.release_conn()
    out = Reply()
    out.headers = r.headers
    return out


def fetch(url: str, session: Any = None) -> dict[str, Any]:
    """The page at `url`, read safely: global addresses only, checked again after every redirect and connected to
    directly (so the name cannot be re-pointed between the check and the connection), standard ports, a size cap and
    a timeout. Returns its final address, status, content type and text. (`session` is for tests.)"""
    current, ip = _checked(url)
    for _ in range(MAX_REDIRECTS + 1):
        if session is not None:
            r = session.get(current, timeout=TIMEOUT, allow_redirects=False, stream=True)
        else:
            try:
                r = _pinned_get(current, ip)
            except Exception as exc:  # noqa: BLE001 - reported as unreadable, never followed elsewhere
                raise requests.ConnectionError(str(exc)[:200]) from exc
        loc = (r.headers or {}).get("Location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            r.close()
            current, ip = _checked(urljoin(current, loc))
            continue
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        r.close()
        ctype = ((r.headers or {}).get("Content-Type") or "").lower()
        charset = re.search(r"charset=([\w-]+)", ctype)
        return {"url": current, "status": r.status_code, "content_type": ctype,
                "text": body[:MAX_BYTES].decode(charset.group(1) if charset else "utf-8", "replace")}
    raise Unsafe("too many redirects")


# --- reading the page -----------------------------------------------------------------------------------------------

def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            dt = parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=NY)          # a time with no zone is read as New York's


def published_time(page: str) -> tuple[datetime | None, str | None]:
    """The source's own publication time and where it was found, or (None, None)."""
    for m in re.finditer(r"<meta\b[^>]*>", page, re.I):
        tag = m.group(0)
        key = re.search(r"""(?:property|name|itemprop)\s*=\s*["']([^"']+)["']""", tag, re.I)
        val = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
        if key and val and key.group(1).strip().lower() in META_TIMES:
            dt = _when(html.unescape(val.group(1)))
            if dt:
                return dt, f"meta {key.group(1)}"
    for m in re.finditer(r"""["']datePublished["']\s*:\s*["']([^"']+)["']""", page):
        dt = _when(m.group(1))
        if dt:
            return dt, "JSON-LD datePublished"
    m = re.search(r"<pubDate>\s*([^<]+)</pubDate>", page, re.I)
    if m and _when(m.group(1)):
        return _when(m.group(1)), "pubDate"
    m = re.search(r"""<time\b[^>]*\bdatetime\s*=\s*["']([^"']+)["']""", page, re.I)
    if m and _when(m.group(1)):
        return _when(m.group(1)), "<time datetime>"
    return None, None


def page_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", page)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"(?s)<[^>]+>", " ", page))).strip()


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9'-]{2,}", (text or "").lower()) if w not in STOP and len(w) >= 4]


def names_stock(text: str, symbol: str) -> bool:
    sym = re.escape(symbol.upper())
    return bool(re.search(rf"(?<![A-Za-z0-9]){sym}(?![A-Za-z0-9])", text) or re.search(rf"\${sym}\b", text, re.I)
                or re.search(rf"\b(?:NYSE|NASDAQ|Nasdaq|AMEX|NYSEARCA|Cboe|OTC)\s*:\s*{sym}\b", text))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z][a-z0-9'-]*", (text or "").lower())


def _stem_in(stems: tuple[str, ...] | list[str], toks: list[str], joined: str) -> bool:
    return any((" " in s and s in joined) or any(t.startswith(s) for t in toks) for s in stems)


def sentence_verdict(sentence: str, symbol: str, claim_words: list[str], trigger_words: list[str]) -> str | None:
    """`affirms`, `contradicts`, `speculative`, or None when the sentence is not about the trigger."""
    toks = _tokens(sentence)
    joined = " ".join(toks)
    have = set(toks)
    t_hit = [w for w in trigger_words if w in have]
    c_hit = [w for w in claim_words if w in have]
    about = names_stock(sentence, symbol) or (claim_words and len(c_hit) >= max(1, (len(claim_words) + 1) // 2))
    keys = [k for k in OPPOSITES if any(w.startswith(k) for w in trigger_words)]
    opposite = any(_stem_in(OPPOSITES[k], toks, joined) for k in keys)
    if not about or (len(t_hit) < min(2, len(trigger_words)) and not opposite):
        return None
    if opposite:
        return "contradicts"
    for w in t_hit:                                    # a negation within three words before a trigger word
        for i, t in enumerate(toks):
            if t == w and any(x in NEGATIONS for x in toks[max(0, i - 3):i]):
                return "contradicts"
    if sentence.strip().endswith("?") or any(any(t == sp or (len(sp) > 4 and t.startswith(sp)) for t in toks) for sp in SPECULATIVE):
        return "speculative"
    return "affirms"


def supports(text: str, claim: str, trigger: str, symbol: str = "") -> tuple[bool | None, dict[str, Any]]:
    """True when one sentence affirms the trigger; False when the sentences about it only contradict it; None when
    they are only speculative or there are none (not enough to verify)."""
    cw, tw = list(dict.fromkeys(words(claim))), list(dict.fromkeys(words(trigger)))
    sentences = [x for x in re.split(r"(?<=[.!?])\s+|\n+", text) if x.strip()]
    found: dict[str, list[str]] = {"affirms": [], "contradicts": [], "speculative": []}
    for x in sentences:
        v = sentence_verdict(x, symbol, cw, tw)
        if v:
            found[v].append(x.strip()[:300])
    have = set(words(text))
    match = {"claim_words": cw, "claim_found": [w for w in cw if w in have], "trigger_words": tw, "trigger_found": [w for w in tw if w in have],
             "affirming": found["affirms"][:3], "contradicting": found["contradicts"][:3], "speculative": found["speculative"][:3]}
    if found["affirms"] and not found["contradicts"]:
        return True, match
    if found["contradicts"]:
        return False, match
    return None, match


def check(page: dict[str, Any], *, symbol: str, claim: str, trigger: str, cutoff: datetime, deadline: datetime,
          now: datetime) -> dict[str, Any]:
    """The verdict on one fetched source (pure): verified, rejected or unverified, with the reason and the evidence."""
    out: dict[str, Any] = {"url": page.get("url"), "http_status": page.get("status"), "checked_at": now.isoformat()}
    if int(page.get("status") or 0) >= 400:
        return {**out, "status": "unverified", "reason": f"the source answered HTTP {page.get('status')}"}
    raw = page.get("text") or ""
    pub, where = published_time(raw)
    text = page_text(raw)
    out.update(published_at=pub.astimezone(timezone.utc).isoformat() if pub else None, published_from=where,
               excerpt=None)
    if pub is None:
        return {**out, "status": "unverified", "reason": "the source gives no publication time of its own"}
    if pub <= cutoff:
        return {**out, "status": "rejected", "reason": "the source was published before the study's evidence cutoff"}
    if pub > now:
        return {**out, "status": "rejected", "reason": "the source's publication time is in the future"}
    if pub > deadline:
        return {**out, "status": "rejected", "reason": "the source was published after the trigger's window closed"}
    if not names_stock(text, symbol):
        return {**out, "status": "rejected", "reason": f"the source does not name {symbol}"}
    ok, match = supports(text, claim, trigger, symbol)
    out["match"] = match
    out["excerpt"] = (match["affirming"] or match["contradicting"] or match["speculative"] or [None])[0]
    if ok is False:
        return {**out, "status": "rejected", "reason": "the source says the opposite of the trigger, or that it did not happen"}
    if ok is None:
        if match["speculative"]:
            return {**out, "status": "unverified", "reason": "the source speaks of it as expected or possible, not as having happened"}
        return {**out, "status": "rejected", "reason": "the source's text does not support the claimed event and the case's trigger"}
    return {**out, "status": "verified", "reason": f"published {pub.astimezone(NY):%Y-%m-%d %H:%M} New York ({where}), names {symbol}, "
                                                  "and supports the claim and the case's trigger"}


# --- the record ------------------------------------------------------------------------------------------------------

def _dt(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def deadline_of(version: dict[str, Any], case_key: str) -> datetime:
    """A dated catalyst's case must happen by the catalyst's session; an undated one by the last forecast session."""
    sessions = version.get("sessions") or []
    cs = version.get("catalyst_session")
    day = date.fromisoformat(sessions[-1]["date"])
    if cs and (version.get("catalyst") or {}).get("event_date") and int(cs) <= len(sessions):
        day = date.fromisoformat(sessions[int(cs) - 1]["date"])
    return datetime.combine(day, dtime(16, 0), tzinfo=NY)


class Triggers:
    def __init__(self, svc: Any, fetcher: Callable[[str], dict[str, Any]] | None = None):
        self.svc = svc
        self.fetcher = fetcher

    def _fetch(self, url: str) -> dict[str, Any]:
        f = self.fetcher or getattr(self.svc, "trigger_fetcher", None) or fetch
        return f(url)

    def _context(self, study_id: str) -> dict[str, Any]:
        r = self.svc.db.one("SELECT symbol, created_at, pipeline_json FROM studies WHERE id = ?", (study_id,))
        if r is None or not r["pipeline_json"]:
            raise RequestError("This study has no scenarios.")
        p = json.loads(r["pipeline_json"])
        if not p.get("version_id"):
            raise RequestError("This study's scenarios have no version yet.")
        ver = self.svc.scenarios.version(p["version_id"])
        seed = self.svc.db.one("SELECT cutoff FROM scenario_seeds WHERE id = ?", (p.get("seed_id"),)) if p.get("seed_id") else None
        cutoff = _dt(seed["cutoff"] if seed else None) or _dt(r["created_at"])
        study_cut = _dt(r["created_at"])
        if study_cut and cutoff and study_cut > cutoff:
            cutoff = study_cut
        return {"symbol": r["symbol"], "cutoff": cutoff, "version": ver, "pipeline": p}

    def submit(self, study_id: str, case_key: str, title: str, published_at: str, url: str | None = None, note: str = "",
               event_at: str | None = None) -> dict[str, Any]:
        """Record a report that a case's trigger happened, as evidence for review. With a link, its source is read in
        the background and the setup is decided again on a fresh snapshot; without one it stays unverified."""
        if case_key not in ("bullish", "bearish"):
            raise RequestError("Only a Bullish or Bearish case's trigger can be recorded.")
        ctx = self._context(study_id)
        claimed = _dt(published_at.replace(" ", "T")) if published_at else None
        if claimed is None:
            raise RequestError(f"published_at must be a date and time, got {published_at!r}")
        if claimed.tzinfo is None:
            claimed = claimed.replace(tzinfo=NY)
        now = datetime.now(timezone.utc)
        cutoff, ver = ctx["cutoff"], ctx["version"]
        if claimed <= cutoff:
            raise RequestError("The source was published before the study's evidence cutoff: it cannot show the trigger happened afterwards.")
        if claimed > now:
            raise RequestError("The source's publication time is in the future.")
        ev = _dt(event_at) if event_at else None
        if event_at and ev is None:
            raise RequestError(f"event_at must be a date and time, got {event_at!r}")
        if ev is not None and (ev <= cutoff or ev > now):
            raise RequestError("The event's time must be after the study's evidence cutoff and not in the future.")
        link = (url or "").strip()[:500] or None
        if link:
            try:
                urlparse(link)
                if urlparse(link).scheme not in ("http", "https"):
                    raise ValueError
            except ValueError as exc:
                raise RequestError("The link must start with http:// or https://") from exc
        deadline = deadline_of(ver, case_key)
        status = "verification_pending" if link else "unverified"
        src = {"title": title.strip()[:200], "url": link, "claimed_published_at": claimed.astimezone(timezone.utc).isoformat(),
               "event_at": ev.astimezone(timezone.utc).isoformat() if ev else None, "note": note.strip()[:300]}
        verification = {"status": status, "reason": "the source is being read" if link else "no link was given: it cannot be verified"}
        stamp = C.utc_now()
        with self.svc.db.tx() as cur:
            cur.execute("""INSERT INTO scenario_triggers (study_id, case_key, observed_at, recorded_at, source_json, status, claimed_published_at,
                           published_at, event_at, retrieved_at, verified_at, cutoff, deadline, verification_json, version_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, ?)
                           ON CONFLICT(study_id, case_key) DO UPDATE SET observed_at = excluded.observed_at, recorded_at = excluded.recorded_at,
                           source_json = excluded.source_json, status = excluded.status, claimed_published_at = excluded.claimed_published_at,
                           published_at = NULL, event_at = excluded.event_at, retrieved_at = NULL, verified_at = NULL, cutoff = excluded.cutoff,
                           deadline = excluded.deadline, verification_json = excluded.verification_json, version_id = excluded.version_id""",
                        (study_id, case_key, src["claimed_published_at"], stamp, json.dumps(src), status, src["claimed_published_at"],
                         src["event_at"], cutoff.astimezone(timezone.utc).isoformat(), deadline.astimezone(timezone.utc).isoformat(),
                         json.dumps(verification), ver.get("id")))
            cur.execute("INSERT INTO scenario_trigger_evidence (study_id, case_key, submitted_at, source_json, status, verification_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (study_id, case_key, stamp, json.dumps(src), status, json.dumps(verification)))
        self.svc.bus.publish("scenario.trigger", study_id, case=case_key, status=status)
        if link:
            from .workers import workers_for
            workers_for(self.svc).enqueue("scenarios", VERIFY, {"study_id": study_id, "case": case_key, "recorded_at": stamp},
                                          dedupe_key=f"{VERIFY}:{study_id}:{case_key}:{stamp}", max_attempts=2)
        return self.get(study_id, case_key) or {}

    def verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Read the source of a submitted trigger and record the verdict; then decide the setup again."""
        study_id, case_key = payload["study_id"], payload["case"]
        row = self.svc.db.one("SELECT * FROM scenario_triggers WHERE study_id = ? AND case_key = ?", (study_id, case_key))
        if row is None or (payload.get("recorded_at") and row["recorded_at"] != payload["recorded_at"]):
            return {"skipped": "a newer submission replaced this one"}
        if row["status"] != "verification_pending":
            return {"skipped": f"the trigger is {row['status']}"}
        src = json.loads(row["source_json"])
        ctx = self._context(study_id)
        if row["version_id"] and row["version_id"] != ctx["version"]["id"]:
            return {"skipped": "the study's scenarios changed since it was submitted: submit it again"}
        case = next((c for c in ctx["version"]["cases"] if c["key"] == case_key), {})
        now = datetime.now(timezone.utc)
        try:
            page = self._fetch(src["url"])
            verdict = check(page, symbol=ctx["symbol"], claim=src.get("title") or "", trigger=case.get("trigger") or "",
                            cutoff=_dt(row["cutoff"]) or ctx["cutoff"], deadline=_dt(row["deadline"]) or deadline_of(ctx["version"], case_key),
                            now=now)
            verdict["checked_url"] = src.get("url")
            verdict["version_id"] = ctx["version"]["id"]
        except Unsafe as exc:
            verdict = {"status": "unverified", "reason": f"the link was not fetched: {exc}", "url": src.get("url")}
        except requests.RequestException as exc:
            verdict = {"status": "unverified", "reason": f"the source could not be read ({exc.__class__.__name__})", "url": src.get("url")}
        stamp = C.utc_now()
        with self.svc.db.tx() as cur:
            cur.execute("""UPDATE scenario_triggers SET status = ?, published_at = ?, retrieved_at = ?, verified_at = ?, verification_json = ?
                           WHERE study_id = ? AND case_key = ? AND recorded_at = ?""",
                        (verdict["status"], verdict.get("published_at"), stamp, stamp if verdict["status"] == "verified" else None,
                         json.dumps(verdict, default=str), study_id, case_key, row["recorded_at"]))
            cur.execute("INSERT INTO scenario_trigger_evidence (study_id, case_key, submitted_at, source_json, status, verification_json) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (study_id, case_key, stamp, row["source_json"], verdict["status"], json.dumps(verdict, default=str)))
        self.svc.bus.publish("scenario.trigger", study_id, case=case_key, status=verdict["status"])
        try:
            self.svc.pipeline.revalue(study_id)                 # a fresh snapshot, then the decision again (via the hook)
        except RequestError as exc:
            log.warning("Could not decide %s again after its trigger was checked: %s", study_id, exc)
        return {"status": verdict["status"], "reason": verdict.get("reason")}

    def expire(self, study_id: str | None = None) -> int:
        """Unverified or pending triggers whose window has ended are expired."""
        now = datetime.now(timezone.utc).isoformat()
        sql = "UPDATE scenario_triggers SET status = 'expired' WHERE status IN ('unverified', 'verification_pending') AND deadline IS NOT NULL AND deadline < ?"
        params: list[Any] = [now]
        if study_id:
            sql += " AND study_id = ?"
            params.append(study_id)
        with self.svc.db.tx() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def get(self, study_id: str, case_key: str) -> dict[str, Any] | None:
        r = self.svc.db.one("SELECT * FROM scenario_triggers WHERE study_id = ? AND case_key = ?", (study_id, case_key))
        return self._row(r, self._version_of(study_id)) if r else None

    def _version_of(self, study_id: str) -> str | None:
        r = self.svc.db.one("SELECT pipeline_json FROM studies WHERE id = ?", (study_id,))
        return (json.loads(r["pipeline_json"]) or {}).get("version_id") if r and r["pipeline_json"] else None

    @staticmethod
    def _row(r: Any, version_id: str | None = None) -> dict[str, Any]:
        keys = r.keys()
        src = json.loads(r["source_json"] or "{}")
        ver = json.loads(r["verification_json"] or "{}") if "verification_json" in keys else {}
        status = r["status"] if "status" in keys and r["status"] else "unverified"
        reason = ver.get("reason")
        if status == "verified":
            # A verification belongs to the source that was read and to the scenario version it was checked against:
            # a source replaced without a new check (the 4.5.0 code would do that after a rollback) or a newer
            # version's case is not verified by it.
            if ver.get("checked_url") != src.get("url"):
                status, reason = "unverified", "the source was changed after it was checked: submit it again"
            elif version_id and ("version_id" in keys and r["version_id"]) and r["version_id"] != version_id:
                status, reason = "unverified", "it was checked against an earlier version of the scenarios: submit it again"
        return {**src, "case": r["case_key"], "status": status, "reason": reason, "excerpt": ver.get("excerpt"),
                "observed_at": r["observed_at"], "recorded_at": r["recorded_at"],
                "published_at": r["published_at"] if "published_at" in keys else None,
                "retrieved_at": r["retrieved_at"] if "retrieved_at" in keys else None,
                "verified_at": r["verified_at"] if "verified_at" in keys else None,
                "cutoff": r["cutoff"] if "cutoff" in keys else None, "deadline": r["deadline"] if "deadline" in keys else None}

    def for_study(self, study_id: str, version_id: str | None = None) -> dict[str, dict[str, Any]]:
        self.expire(study_id)
        version_id = version_id or self._version_of(study_id)
        return {r["case_key"]: self._row(r, version_id) for r in self.svc.db.query("SELECT * FROM scenario_triggers WHERE study_id = ?", (study_id,))}

    def history(self, study_id: str) -> list[dict[str, Any]]:
        return [{"case": r["case_key"], "submitted_at": r["submitted_at"], "status": r["status"], **json.loads(r["source_json"]),
                 "verification": json.loads(r["verification_json"] or "{}")}
                for r in self.svc.db.query("SELECT * FROM scenario_trigger_evidence WHERE study_id = ? ORDER BY id", (study_id,))]


def install(svc: Any) -> None:
    from .workers import workers_for
    svc.triggers = tr = Triggers(svc)
    workers_for(svc).add("scenarios", VERIFY, tr.verify)


__all__ = ["Triggers", "install", "check", "fetch", "check_url", "published_time", "supports", "names_stock", "deadline_of", "STATES", "Unsafe"]
