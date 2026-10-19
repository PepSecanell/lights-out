"""Install the tick: a launchd agent (macOS), a systemd user timer (Linux), or a crontab line (anything else).

All three run `python -m lightsout tick` every `tick_minutes`. They are given a PATH that contains the
folder `claude` was found in, because background jobs start with a bare PATH and "claude: command not
found" at 23:00 is a silly way to lose a night.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

LABEL = "dev.lightsout.tick"
CRON_TAG = "# lights-out tick"


def detect() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if shutil.which("systemctl") and Path("/run/systemd/system").exists():
        return "systemd"
    return "cron"


def _path_env(claude_bin: str) -> str:
    dirs = []
    found = shutil.which(claude_bin)
    if found:
        dirs.append(str(Path(found).resolve().parent))
        dirs.append(str(Path(found).parent))
    dirs += [str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin",
             "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    out = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return ":".join(out)


def _argv(config: Path) -> list:
    return [sys.executable, "-m", "lightsout", "--config", str(config), "tick"]


# ── launchd ──────────────────────────────────────────────────────────────────────────────────────

def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_plist(config: Path, minutes: int, claude_bin: str) -> bytes:
    logs = paths.logs_dir()
    env = {"PATH": _path_env(claude_bin), "PYTHONUNBUFFERED": "1"}
    if os.environ.get("LIGHTSOUT_HOME"):
        env["LIGHTSOUT_HOME"] = os.environ["LIGHTSOUT_HOME"]
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": _argv(config),
        "StartInterval": minutes * 60,
        "RunAtLoad": True,                     # also fires at login, the most reliable trigger on a laptop
        "StandardOutPath": str(logs / "tick.log"),
        "StandardErrorPath": str(logs / "tick.log"),
        "EnvironmentVariables": env,
        "ProcessType": "Background",
        "LowPriorityIO": True,
    })


# ── systemd ──────────────────────────────────────────────────────────────────────────────────────

def systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def systemd_units(config: Path, minutes: int, claude_bin: str) -> dict:
    cmd = " ".join(f'"{a}"' if " " in a else a for a in _argv(config))
    service = (f"[Unit]\nDescription=lights-out tick\n\n[Service]\nType=oneshot\n"
               f"Environment=PATH={_path_env(claude_bin)}\nExecStart={cmd}\n")
    timer = (f"[Unit]\nDescription=lights-out tick every {minutes} min\n\n[Timer]\nOnBootSec=2min\n"
             f"OnUnitActiveSec={minutes}min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return {"lightsout.service": service, "lightsout.timer": timer}


# ── cron ─────────────────────────────────────────────────────────────────────────────────────────

def cron_line(config: Path, minutes: int, claude_bin: str) -> str:
    cmd = " ".join(f"'{a}'" for a in _argv(config))
    log = paths.logs_dir() / "tick.log"
    return f"*/{minutes} * * * * PATH={_path_env(claude_bin)} {cmd} >> '{log}' 2>&1 {CRON_TAG}"


def _crontab() -> list:
    p = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return p.stdout.splitlines() if p.returncode == 0 else []


def _set_crontab(lines: list) -> None:
    subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)


# ── install / uninstall ──────────────────────────────────────────────────────────────────────────

def install(config: Path, minutes: int, claude_bin: str, kind: str = "auto", dry_run: bool = False) -> str:
    kind = detect() if kind == "auto" else kind
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    if kind == "launchd":
        data = launchd_plist(config, minutes, claude_bin)
        if dry_run:
            return f"would write {plist_path()}:\n{data.decode()}"
        plist_path().parent.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        plist_path().write_bytes(data)
        p = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path())], capture_output=True, text=True)
        if p.returncode != 0:
            subprocess.run(["launchctl", "load", "-w", str(plist_path())], capture_output=True)
        return f"launchd agent {LABEL} installed ({plist_path()}), ticks every {minutes} min"
    if kind == "systemd":
        units = systemd_units(config, minutes, claude_bin)
        if dry_run:
            return "\n".join(f"would write {systemd_dir() / n}:\n{t}" for n, t in units.items())
        systemd_dir().mkdir(parents=True, exist_ok=True)
        for n, t in units.items():
            (systemd_dir() / n).write_text(t)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "lightsout.timer"], check=False)
        return (f"systemd user timer installed in {systemd_dir()}, ticks every {minutes} min. To keep it running "
                f"while you are logged out: loginctl enable-linger $USER")
    if kind == "cron":
        line = cron_line(config, minutes, claude_bin)
        if dry_run:
            return f"would add to your crontab:\n{line}"
        _set_crontab([x for x in _crontab() if CRON_TAG not in x] + [line])
        return f"crontab line added, ticks every {minutes} min"
    raise ValueError(f"unknown scheduler {kind!r}")


def uninstall(kind: str = "auto") -> str:
    kind = detect() if kind == "auto" else kind
    if kind == "launchd":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        plist_path().unlink(missing_ok=True)
        return "launchd agent removed"
    if kind == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", "lightsout.timer"], check=False)
        for n in ("lightsout.service", "lightsout.timer"):
            (systemd_dir() / n).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        return "systemd timer removed"
    _set_crontab([x for x in _crontab() if CRON_TAG not in x])
    return "crontab line removed"


def installed(kind: str = "auto") -> bool:
    kind = detect() if kind == "auto" else kind
    if kind == "launchd":
        return plist_path().exists()
    if kind == "systemd":
        return (systemd_dir() / "lightsout.timer").exists()
    return any(CRON_TAG in x for x in _crontab())
