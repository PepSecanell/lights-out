"""One place that says what time it is.

LIGHTSOUT_NOW (an ISO time like 2026-10-18T23:10) freezes the clock. The tests and the demo use it
to replay a whole night in a few seconds. Nothing else in the package calls datetime.now().
"""
from __future__ import annotations

import datetime as dt
import os


def now() -> dt.datetime:
    fake = os.environ.get("LIGHTSOUT_NOW")
    if fake:
        t = dt.datetime.fromisoformat(fake)
        return t if t.tzinfo else t.astimezone()
    return dt.datetime.now().astimezone()


def hm(t: dt.datetime | None) -> str:
    return t.strftime("%H:%M") if t else "--:--"


def iso(t: dt.datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def parse_iso(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()
