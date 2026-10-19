"""Load and check lightsout.toml (or .json).

One file describes the night: when jobs may start, what makes the laptop "busy", and the jobs.
Every value has a safe default, so a config can be as short as one [[job]].
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib as _toml
except ImportError:  # pragma: no cover
    try:
        import tomli as _toml  # type: ignore
    except ImportError:
        _toml = None


class ConfigError(ValueError):
    pass


DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ── schedules ────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Schedule:
    """`daily 23:00`, `weekly mon,thu 01:30`, `every 2h`, `every 45m`, or `manual`."""
    kind: str                      # daily | weekly | every | manual
    at: dt.time | None = None
    days: tuple = ()               # 0=mon .. 6=sun, weekly only
    every_min: int = 0
    text: str = ""

    def last_due(self, now: dt.datetime) -> dt.datetime | None:
        """The latest moment at or before `now` when this schedule said "run". None for every/manual."""
        if self.kind in ("every", "manual"):
            return None
        for back in range(0, 8):
            day = (now - dt.timedelta(days=back)).date()
            t = dt.datetime.combine(day, self.at, tzinfo=now.tzinfo)
            if t > now:
                continue
            if self.kind == "weekly" and day.weekday() not in self.days:
                continue
            return t
        return None


def parse_schedule(text: str) -> Schedule:
    s = (text or "").strip().lower()
    if s in ("", "manual", "never"):
        return Schedule("manual", text=s or "manual")
    m = re.fullmatch(r"every\s+(\d+)\s*(m|min|minutes?|h|hours?)", s)
    if m:
        n = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        if n < 10:
            raise ConfigError(f"schedule {text!r}: the shortest interval is 10 minutes")
        return Schedule("every", every_min=n, text=s)
    if s == "hourly":
        return Schedule("every", every_min=60, text=s)
    m = re.fullmatch(r"(?:daily\s+)?(\d{1,2}):(\d{2})", s)
    if m:
        return Schedule("daily", at=_hm(m.group(1), m.group(2), text), text=s)
    m = re.fullmatch(r"weekly\s+([a-z,\s]+?)\s+(\d{1,2}):(\d{2})", s)
    if m:
        days = []
        for d in re.split(r"[,\s]+", m.group(1).strip()):
            if d[:3] not in DAYS:
                raise ConfigError(f"schedule {text!r}: unknown day {d!r} (use mon,tue,...)")
            days.append(DAYS.index(d[:3]))
        return Schedule("weekly", at=_hm(m.group(2), m.group(3), text), days=tuple(sorted(set(days))), text=s)
    raise ConfigError(f"schedule {text!r} not understood. Use 'daily 23:00', 'weekly mon,thu 01:30', "
                      f"'every 2h', 'every 45m' or 'manual'.")


def _hm(h, m, text) -> dt.time:
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ConfigError(f"schedule {text!r}: bad time")
    return dt.time(h, m)


# ── the night window ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Window:
    start: dt.time
    end: dt.time
    text: str = ""

    def contains(self, t: dt.datetime) -> bool:
        x = t.time()
        if self.start == self.end:
            return True                                  # "00:00-00:00" = any time
        if self.start < self.end:
            return self.start <= x < self.end
        return x >= self.start or x < self.end           # crosses midnight

    def night_of(self, t: dt.datetime) -> dt.date:
        """The evening a moment belongs to. Everything before the window opens counts as last night,
        so the morning report after a 22:00-06:00 window is about the evening before."""
        return t.date() if t.time() >= self.start else t.date() - dt.timedelta(days=1)

    def closes_at(self, t: dt.datetime) -> dt.datetime:
        end = dt.datetime.combine(t.date(), self.end, tzinfo=t.tzinfo)
        return end if end > t else end + dt.timedelta(days=1)


def parse_window(text: str) -> Window:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", text or "")
    if not m:
        raise ConfigError(f"window {text!r} not understood. Use 'HH:MM-HH:MM', e.g. '22:00-06:00'.")
    return Window(_hm(m.group(1), m.group(2), text), _hm(m.group(3), m.group(4), text), text.strip())


# ── the config ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class Guards:
    busy_processes: list = field(default_factory=list)   # never START a job while one of these runs
    busy_command: str = ""                                # shell command; exit 0 means "I'm busy"
    user_idle_minutes: int = 0                            # 0 = off; else wait until keyboard/mouse idle this long (macOS)
    on_battery: str = "skip"                              # skip | allow
    min_battery_percent: int = 30                         # with on_battery = "allow"
    min_free_disk_gb: float = 2.0
    wait_for_network_s: int = 600                         # after a wake from sleep Wi-Fi can take a while


@dataclass
class Job:
    name: str
    schedule: Schedule
    prompt: str = ""
    prompt_file: str = ""
    command: list = field(default_factory=list)           # a plain command instead of a Claude prompt
    cwd: str = ""
    priority: int = 5                                     # lower goes first
    model: str = ""
    effort: str = ""
    allowed_tools: list = field(default_factory=list)
    disallowed_tools: list = field(default_factory=list)
    permission_mode: str = ""
    extra_args: list = field(default_factory=list)
    worktree: bool = False                                # run in a fresh git worktree on its own branch
    max_budget_usd: float = 0.0
    timeout_minutes: int = 60
    retries: int = 3                                      # retries of TEMPORARY errors inside one firing
    give_up_after: int = 3                                # real failures in a row before the job is parked
    catch_up_hours: float = 12.0                          # a missed daily run still counts this long after
    enabled: bool = True

    @property
    def is_claude(self) -> bool:
        return not self.command


@dataclass
class Config:
    path: Path
    window: Window
    jobs: list
    guards: Guards
    report_at: dt.time = dt.time(7, 30)
    nightly_budget_usd: float = 0.0                       # 0 = no cap
    allowed_models: list = field(default_factory=list)    # e.g. ["sonnet", "haiku"]; empty = any
    keep_awake: bool = True
    tick_minutes: int = 10
    notify: bool = True
    report_command: str = ""                              # run with the report path as $1
    token_file: str = ""                                  # optional long-lived CLAUDE_CODE_OAUTH_TOKEN file
    claude_bin: str = "claude"

    def job(self, name: str) -> Job:
        for j in self.jobs:
            if j.name == name:
                return j
        raise ConfigError(f"no job called {name!r} (have: {', '.join(j.name for j in self.jobs) or 'none'})")

    def resolve(self, p: str) -> Path:
        q = Path(p).expanduser()
        return q if q.is_absolute() else (self.path.parent / q)


_JOB_KEYS = set(Job.__dataclass_fields__) - {"schedule"} | {"schedule"}
_NIGHT_KEYS = {"window", "report_at", "nightly_budget_usd", "allowed_models", "keep_awake", "tick_minutes", "notify",
               "report_command", "token_file", "claude_bin"}


def _read_raw(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    if _toml is None:
        raise ConfigError("reading TOML needs Python 3.11+ (or `pip install tomli`); a .json config works anywhere")
    try:
        return _toml.loads(text)
    except Exception as e:
        raise ConfigError(f"{path}: {e}") from e


def load(path) -> Config:
    path = Path(path).expanduser()
    if not path.exists():
        raise ConfigError(f"no config at {path}. Make one with: lightsout init")
    raw = _read_raw(path)
    night = dict(raw.get("night") or {})
    unknown = set(night) - _NIGHT_KEYS
    if unknown:
        raise ConfigError(f"[night]: unknown key(s) {', '.join(sorted(unknown))}")
    g = dict(raw.get("guards") or {})
    unknown = set(g) - set(Guards.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"[guards]: unknown key(s) {', '.join(sorted(unknown))}")
    guards = Guards(**g)
    if guards.on_battery not in ("skip", "allow"):
        raise ConfigError("[guards] on_battery must be 'skip' or 'allow'")

    jobs, seen = [], set()
    for i, rj in enumerate(raw.get("job") or raw.get("jobs") or []):
        rj = dict(rj)
        name = str(rj.get("name") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ConfigError(f"job #{i + 1}: needs a name of letters, digits, '-', '_' or '.'")
        if name in seen:
            raise ConfigError(f"two jobs are called {name!r}")
        seen.add(name)
        unknown = set(rj) - _JOB_KEYS
        if unknown:
            raise ConfigError(f"job {name!r}: unknown key(s) {', '.join(sorted(unknown))}")
        rj["schedule"] = parse_schedule(str(rj.get("schedule", "manual")))
        if isinstance(rj.get("command"), str):
            rj["command"] = ["/bin/sh", "-c", rj["command"]]
        job = Job(**rj)
        sources = sum(bool(x) for x in (job.prompt, job.prompt_file, job.command))
        if sources != 1:
            raise ConfigError(f"job {name!r}: give exactly one of prompt, prompt_file or command")
        if job.prompt_file and not (path.parent / Path(job.prompt_file).expanduser()).exists() \
                and not Path(job.prompt_file).expanduser().exists():
            raise ConfigError(f"job {name!r}: prompt_file {job.prompt_file!r} not found")
        if job.permission_mode == "bypassPermissions" and not job.worktree:
            raise ConfigError(f"job {name!r}: bypassPermissions is only allowed together with worktree = true, so "
                              f"an unattended agent with every permission can't touch your working copy")
        jobs.append(job)

    cfg = Config(
        path=path,
        window=parse_window(night.pop("window", "22:00-06:00")),
        jobs=jobs,
        guards=guards,
        **{k: v for k, v in night.items() if k != "report_at"},
    )
    if "report_at" in night:
        cfg.report_at = _hm(*str(night["report_at"]).split(":"), night["report_at"])
    for j in jobs:
        if j.is_claude:
            check_model(cfg, j.model, j.name)
    return cfg


def check_model(cfg: Config, model: str, job: str) -> None:
    """With allowed_models set, a job asking for anything else is a config error, not a surprise bill."""
    if not cfg.allowed_models:
        return
    if not model:
        raise ConfigError(f"job {job!r}: allowed_models is set, so the job must name its model")
    if not any(a.lower() in model.lower() for a in cfg.allowed_models):
        raise ConfigError(f"job {job!r}: model {model!r} is not in allowed_models {cfg.allowed_models}")
