""""Is now a good moment to start an agent on this laptop?" Every check returns None (go) or a reason (wait).

These only decide whether a job STARTS. A job that is already running is left alone: killing an agent
halfway through an edit is worse than letting it finish.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import Guards

IS_MAC = sys.platform == "darwin"
PROBE = ("api.anthropic.com", 443)


def _run(cmd, timeout=10) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None, ""


def busy_process(names) -> str | None:
    """A process from the list is running (exact name first, then full command line)."""
    if not names or not shutil.which("pgrep"):
        return None
    for n in names:
        for flag in ("-x", "-f"):
            code, _ = _run(["pgrep", flag, n], 5)
            if code == 0:
                return f"{n} is running"
    return None


def busy_command(cmd: str) -> str | None:
    """Your own check. Exit 0 means busy, e.g. `test -f ~/.recording` or a script reading your camera app's log."""
    if not cmd:
        return None
    code, out = _run(["/bin/sh", "-c", cmd], 20)
    if code == 0:
        first = (out.strip().splitlines() or [""])[0][:80]
        return f"busy_command says busy" + (f": {first}" if first else "")
    return None


def idle_seconds() -> float | None:
    """Seconds since the last keyboard or mouse input (macOS). None when it can't be read."""
    if not IS_MAC:
        return None
    code, out = _run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], 10)
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out or "")
    return int(m.group(1)) / 1e9 if m else None


def user_active(min_idle_minutes: int) -> str | None:
    if not min_idle_minutes:
        return None
    s = idle_seconds()
    if s is not None and s < min_idle_minutes * 60:
        return f"you used the keyboard or mouse {int(s // 60)} min ago (waits for {min_idle_minutes} min idle)"
    return None


def battery() -> tuple:
    """(on_battery, percent) or (None, None) when there is no battery to read."""
    if IS_MAC:
        code, out = _run(["pmset", "-g", "batt"], 5)
        if code != 0:
            return None, None
        pct = re.search(r"(\d+)%", out)
        return ("Battery Power" in out), (int(pct.group(1)) if pct else None)
    base = Path("/sys/class/power_supply")
    if not base.is_dir():
        return None, None
    on_ac, pct = None, None
    for d in base.iterdir():
        try:
            t = (d / "type").read_text().strip()
            if t == "Mains":
                on_ac = (d / "online").read_text().strip() == "1"
            elif t == "Battery" and (d / "capacity").exists():
                pct = int((d / "capacity").read_text().strip())
        except (OSError, ValueError):
            continue
    return (None if on_ac is None else not on_ac), pct


def power(g: Guards) -> str | None:
    on_batt, pct = battery()
    if not on_batt:
        return None
    if g.on_battery == "skip":
        return "on battery (set on_battery = \"allow\" to run anyway)"
    if pct is not None and pct < g.min_battery_percent:
        return f"battery at {pct}% (below {g.min_battery_percent}%)"
    return None


def disk(path, min_gb: float) -> str | None:
    if not min_gb:
        return None
    try:
        free = shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        return None
    return f"only {free:.1f} GB free (needs {min_gb:g} GB)" if free < min_gb else None


def network_up(timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection(PROBE, timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_network(max_s: int, interval: int = 15, sleep=time.sleep) -> str | None:
    """After a wake from sleep the tick can fire before Wi-Fi is back ("nodename nor servname provided").
    Wait for it on the WALL CLOCK: a deadline, not a count of sleeps, because a laptop that sleeps
    mid-wait would otherwise turn 10 minutes into a whole night."""
    if os.environ.get("LIGHTSOUT_SKIP_NETWORK") == "1" or max_s <= 0:
        return None
    deadline = time.time() + max_s
    while True:
        if network_up():
            return None
        if time.time() >= deadline:
            return f"no network after {max_s}s"
        sleep(interval)


def check_all(g: Guards, cwd=None) -> str | None:
    """The first reason not to start now, or None."""
    checks = (lambda: busy_process(g.busy_processes), lambda: busy_command(g.busy_command),
              lambda: user_active(g.user_idle_minutes), lambda: power(g),
              lambda: disk(cwd or Path.home(), g.min_free_disk_gb))
    for check in checks:
        reason = check()
        if reason:
            return reason
    return None
