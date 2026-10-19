"""Small JSON files on disk that every lightsout process agrees on.

  state/limit.json        the usage limit: no job starts a Claude session before `resume_after`.
                          One file for every job, because the limit is shared by all of them.
  state/slot.json         THE SLOT: whoever holds it is the only agent running. Created with O_EXCL, so
                          there is exactly one holder. Stale when the holder's pid is dead or its heartbeat
                          is older than 20 minutes, and then whoever looks next clears it.
  state/jobs/<job>.json   per job: last start, last result, real failures in a row, parked or not.
  state/locks/<name>.lock a lock on one folder or resource, for jobs and for you (a hand edit takes the
                          same lock so a scheduled job leaves that folder alone).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import threading
from pathlib import Path

from . import clock, paths

HOST = socket.gethostname()
HEARTBEAT_EVERY_S = 60
STALE_MIN = 20


def read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(p: Path, d) -> None:
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def _create_excl(p: Path, d: dict) -> bool:
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, indent=2))
    return True


def _stale(d: dict | None) -> bool:
    if not d:
        return True
    if d.get("host") == HOST and d.get("pid") and not pid_alive(d["pid"]):
        return True
    if d.get("expires"):
        exp = clock.parse_iso(d["expires"])
        return exp is None or clock.now() > exp
    hb = clock.parse_iso(d.get("heartbeat"))
    return hb is None or (clock.now() - hb).total_seconds() > STALE_MIN * 60


# ── usage limit ──────────────────────────────────────────────────────────────────────────────────

def limit_file() -> Path:
    return paths.state_dir() / "limit.json"


def limit_until(now=None):
    """The time before which nothing may start a Claude session, or None."""
    d = read_json(limit_file()) or {}
    ra = clock.parse_iso(d.get("resume_after"))
    now = now or clock.now()
    return ra if ra and ra > now else None


def set_limit(resume_after, job: str, detail: str) -> None:
    cur = limit_until()
    if cur and resume_after and cur > resume_after:
        resume_after = cur
    write_json(limit_file(), {"resume_after": clock.iso(resume_after), "set_by": job,
                              "at": clock.iso(clock.now()), "detail": detail[:200]})


# ── per-job state ────────────────────────────────────────────────────────────────────────────────

def job_file(name: str) -> Path:
    return paths.state_dir() / "jobs" / f"{name}.json"


def job_state(name: str) -> dict:
    return read_json(job_file(name)) or {}


def update_job_state(name: str, **kw) -> dict:
    d = job_state(name)
    d.update(kw)
    write_json(job_file(name), d)
    return d


# ── locks (the slot is just the lock called "slot") ──────────────────────────────────────────────

class Busy(Exception):
    def __init__(self, reason: str, holder: dict | None = None):
        super().__init__(reason)
        self.reason, self.holder = reason, holder


def lock_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120]
    return paths.state_dir() / ("slot.json" if name == "slot" else f"locks/{safe}.lock")


def holder(name: str):
    p = lock_path(name)
    d = read_json(p)
    if d is None:
        return None
    if _stale(d):
        p.unlink(missing_ok=True)
        return None
    return d


class Lock:
    """`with Lock("slot", owner="nightly-tests"):` Raises Busy when someone else holds it.

    A process lock carries a pid and a heartbeat thread. A hand lock (`minutes=`) has no pid and simply
    expires; run `lightsout lock` again to renew it."""

    def __init__(self, name: str, owner: str, pid: int | None = None, minutes: int | None = None):
        self.name, self.owner, self.minutes = name, owner, minutes
        self.pid = 0 if minutes else (pid or os.getpid())
        self._stop = threading.Event()
        self.held = False

    def _doc(self, started=None):
        now = clock.iso(clock.now())
        d = {"owner": self.owner, "pid": self.pid, "host": HOST, "started": started or now, "heartbeat": now}
        if self.minutes:
            d["expires"] = clock.iso(clock.now() + dt.timedelta(minutes=self.minutes))
        return d

    def acquire(self):
        p = lock_path(self.name)
        cur = holder(self.name)
        if cur and cur.get("owner") == self.owner and self.minutes:
            write_json(p, self._doc(cur.get("started")))            # renewing my own hand lock
        elif cur or not _create_excl(p, self._doc()):
            cur = cur or holder(self.name) or {}
            raise Busy(f"{cur.get('owner', 'someone')} holds it since {str(cur.get('started', ''))[11:16]}", cur)
        self.held = True
        if not self.minutes:
            threading.Thread(target=self._beat, daemon=True).start()
        return self

    def _beat(self):
        while not self._stop.wait(HEARTBEAT_EVERY_S):
            d = read_json(lock_path(self.name))
            if d and d.get("owner") == self.owner and d.get("pid") == self.pid:
                d["heartbeat"] = clock.iso(clock.now())
                write_json(lock_path(self.name), d)

    def release(self):
        self._stop.set()
        if self.held:
            d = read_json(lock_path(self.name))
            if d and d.get("owner") == self.owner:
                lock_path(self.name).unlink(missing_ok=True)
            self.held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *a):
        self.release()


def release_by_owner(name: str, owner: str) -> bool:
    d = read_json(lock_path(name))
    if d and d.get("owner") == owner:
        lock_path(name).unlink(missing_ok=True)
        return True
    return False
