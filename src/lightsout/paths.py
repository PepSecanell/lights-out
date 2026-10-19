"""Where lights-out keeps its config, state and logs.

macOS: state in ~/Library/Application Support/lightsout, logs in ~/Library/Logs/lightsout.
Linux: state in $XDG_STATE_HOME/lightsout (~/.local/state/lightsout), logs in <state>/logs.

Logs never go inside ~/Downloads, ~/Desktop or ~/Documents: macOS privacy protection (TCC) stops
launchd from opening a log file there, and the agent then dies at start with exit code 78 before it
writes a single line. That one cost a whole night of jobs once, so it is a rule here.

LIGHTSOUT_HOME moves everything (state and logs) under one folder; the tests and the demo use it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
TCC_PROTECTED = ("Downloads", "Desktop", "Documents")


def home() -> Path:
    env = os.environ.get("LIGHTSOUT_HOME")
    if env:
        return Path(env).expanduser()
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "lightsout"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "lightsout"


def state_dir() -> Path:
    return home() / "state"


def logs_dir() -> Path:
    if os.environ.get("LIGHTSOUT_HOME") or not IS_MAC:
        return home() / "logs"
    return Path.home() / "Library" / "Logs" / "lightsout"


def reports_dir() -> Path:
    return home() / "reports"


def worktrees_dir() -> Path:
    return home() / "worktrees"


def default_config() -> Path:
    env = os.environ.get("LIGHTSOUT_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "lightsout" / "lightsout.toml"


def in_tcc_folder(p: Path) -> bool:
    """True when a path sits in a folder macOS protects from background jobs."""
    if not IS_MAC:
        return False
    try:
        rel = Path(p).expanduser().resolve().relative_to(Path.home().resolve())
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] in TCC_PROTECTED
