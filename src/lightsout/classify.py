"""Sort every failed `claude -p` run into one of five kinds, because each one needs a different answer.

  ok            nothing to do
  usage_limit   the plan's limit is used up. Not the job's fault: nothing starts again before the reset
                time (read from the message, e.g. "resets 11:30pm (Europe/Madrid)"), and it is NOT a failure.
  auth_expired  the login lapsed. Retrying won't help; it is reported, not counted as a failure.
  transient     5xx, overloaded, network, a dropped connection. Retried with back-off in the same firing.
  failure       a real failure. The only kind that counts toward "failed N times in a row, park the job".

Only error text is looked at. A successful run's reply can contain any words ("timed out", "limit") and
must never be read as an error.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import clock

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

OK, LIMIT, AUTH, TRANSIENT, FAILURE = "ok", "usage_limit", "auth_expired", "transient", "failure"
LIMIT_MARGIN_MIN = 3            # start a little after the stated reset
LIMIT_UNKNOWN_WAIT_MIN = 60     # when the reset time can't be read

_LIMIT_RE = re.compile(r"hit your .{0,25}limit|usage limit|session limit|weekly limit|limit reached|"
                       r"out of (extra )?usage|credit balance is too low", re.I)
_AUTH_RE = re.compile(r"failed to authenticate|oauth (session|token)[^.\n]{0,40}(expired|revoked|invalid)|"
                      r"not logged in|please run /login|run /login|invalid api key|authentication_failed|"
                      r"invalid bearer token|authentication_error|api error: 401|\b401\b.{0,30}(unauthor|auth)|"
                      r"token (has )?(expired|been revoked)", re.I)
_TRANSIENT_RE = re.compile(r"api error: 5\d\d|internal server error|overloaded|service unavailable|bad gateway|"
                           r"gateway time-?out|econnreset|etimedout|enotfound|eai_again|econnrefused|socket hang up|"
                           r"fetch failed|network error|connection (error|reset|closed|refused)|unable to connect|"
                           r"request timed out|timed out|temporarily unavailable|try again in a moment|"
                           r"nodename nor servname|rate_limit_error|api error: 429|\bhttp 5\d\d\b|"
                           r"status(?: code)?:? 5\d\d\b", re.I)
# "resets 11:30pm", "resets 4am (Europe/Madrid)", "resets Oct 21, 9am". \s also matches the narrow
# no-break space (U+202F) that macOS puts before AM/PM; a plain space here once silently broke a guard.
_RESET_RE = re.compile(r"resets?\s+(?:(?P<mon>[A-Z][a-z]{2,8})\s+(?P<day>\d{1,2})(?:,|\s+at)?\s+)?"
                       r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*(?:\((?P<tz>[A-Za-z_]+/[A-Za-z_]+)\))?",
                       re.I)
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


@dataclass
class Verdict:
    kind: str
    detail: str = ""
    resume_after: dt.datetime | None = None

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "resume_after": clock.iso(self.resume_after)}


def parse_reset(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """'resets 11:30pm (Europe/Madrid)' -> the next 23:30 in Madrid, as an aware datetime. None if unreadable."""
    m = _RESET_RE.search(text or "")
    if not m:
        return None
    now = now or clock.now()
    tz = now.tzinfo
    if m.group("tz") and ZoneInfo:
        try:
            tz = ZoneInfo(m.group("tz"))
        except Exception:
            pass
    h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    mi = int(m.group("m") or 0)
    now_tz = now.astimezone(tz)
    if m.group("mon") and m.group("day"):
        mon = _MONTHS.get(m.group("mon")[:3].lower())
        if not mon:
            return None
        t = dt.datetime(now_tz.year, mon, int(m.group("day")), h, mi, tzinfo=tz)
        if t < now_tz - dt.timedelta(days=1):
            t = t.replace(year=t.year + 1)
    else:
        t = now_tz.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= now_tz:
            t += dt.timedelta(days=1)
    return t.astimezone(now.tzinfo)


def _limit_verdict(seg: str, now) -> Verdict:
    now = now or clock.now()
    ra = parse_reset(seg, now)
    ra = ra + dt.timedelta(minutes=LIMIT_MARGIN_MIN) if ra else now + dt.timedelta(minutes=LIMIT_UNKNOWN_WAIT_MIN)
    return Verdict(LIMIT, seg.strip()[:200], ra)


def classify(text: str = "", *, exit_code: int = 0, is_error: bool | None = None, api_status=None,
             timed_out: bool = False, now: dt.datetime | None = None) -> Verdict:
    """Classify one run from its final error text (stream-json `result`, or the CLI's own short output)."""
    if is_error is None:
        is_error = exit_code != 0
    if not is_error and exit_code == 0 and not timed_out:
        return Verdict(OK)
    head = (text or "").strip()[:1500]
    try:
        status = int(api_status) if api_status not in (None, "") else None
    except (TypeError, ValueError):
        status = None
    if _LIMIT_RE.search(head) or (status == 429 and re.search(r"resets?\s", head, re.I)):
        return _limit_verdict(head, now)
    if status in (401, 403) or _AUTH_RE.search(head):
        return Verdict(AUTH, head[:200])
    if timed_out:
        return Verdict(FAILURE, "stopped at the job's time limit")
    if (status is not None and (status >= 500 or status == 429)) or _TRANSIENT_RE.search(head):
        return Verdict(TRANSIENT, head[:200])
    return Verdict(FAILURE, head[:300] or f"exited with code {exit_code}")


def scan(text: str, exit_code: int, now=None) -> Verdict:
    """Classify a plain command's whole output (the error can be anywhere in it). Exit 0 = ok."""
    if exit_code == 0:
        return Verdict(OK)
    t = text or ""
    m = _LIMIT_RE.search(t)
    if m:
        return _limit_verdict(t[max(0, m.start() - 80): m.end() + 200], now)
    for rx, kind in ((_AUTH_RE, AUTH), (_TRANSIENT_RE, TRANSIENT)):
        m = rx.search(t)
        if m:
            return Verdict(kind, t[max(0, m.start() - 40): m.end() + 120].strip()[:200])
    tail = "\n".join(t.strip().splitlines()[-3:])
    return Verdict(FAILURE, (tail or f"exited with code {exit_code}")[:300])


def read_result(log_path) -> dict:
    """The last stream-json `result` event of a log (cost, turns, session id, error text), plus any
    non-JSON lines at the end (that is where the CLI prints errors it hit before streaming)."""
    info: dict = {}
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return info
    for line in reversed(lines):
        if line.startswith("{") and '"type":"result"' in line.replace(" ", ""):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            info = {"cost_usd": r.get("total_cost_usd"), "turns": r.get("num_turns"),
                    "session_id": r.get("session_id"), "is_error": r.get("is_error"),
                    "api_status": r.get("api_error_status"), "result_text": str(r.get("result") or "")[:600]}
            break
    info["stray_text"] = "\n".join(x for x in lines[-12:] if x and not x.startswith("{"))[-600:]
    return info
