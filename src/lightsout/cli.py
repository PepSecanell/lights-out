"""lightsout: run Claude Code jobs unattended overnight on your own laptop, safely."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import __version__, classify, clock, guards, paths, report, schedulers, shift, state, worktree
from .config import ConfigError, load

_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if _TTY or os.environ.get("FORCE_COLOR") else str(text)


def dim(t):
    return c(t, "2")


def ok(t):
    return c(t, "32")


def warn(t):
    return c(t, "33")


def bad(t):
    return c(t, "31")


def bold(t):
    return c(t, "1")


def tilde(p) -> str:
    s, h = str(p), str(Path.home())
    return "~" + s[len(h):] if s.startswith(h + os.sep) else s


def _template() -> str:
    return (Path(__file__).parent / "templates" / "lightsout.toml").read_text(encoding="utf-8")


# ── commands ─────────────────────────────────────────────────────────────────────────────────────

def cmd_init(a) -> int:
    p = Path(a.config)
    if p.exists() and not a.force:
        print(f"{p} already exists (use --force to overwrite)")
        return 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_template(), encoding="utf-8")
    (p.parent / "prompts").mkdir(exist_ok=True)
    play = Path.home() / "lights-out-playground"
    if not play.exists():
        play.mkdir()
        (play / "README.md").write_text("A safe folder for the `hello` job to look at.\n", encoding="utf-8")
    print(ok("✓ ") + f"wrote {p}")
    print(ok("✓ ") + f"made {play} for the hello job")
    print("\nNext:\n  lightsout check          see what will run and when\n  lightsout run hello      try a job right now"
          "\n  lightsout install        let it run every night")
    return 0


def cmd_check(a) -> int:
    cfg = load(a.config)
    now = clock.now()
    print(bold(f"config {tilde(cfg.path)}") + dim(f"  (window {cfg.window.text}, report after {cfg.report_at:%H:%M})"))
    for j in sorted(cfg.jobs, key=lambda j: j.priority):
        src = "command" if j.command else ("prompt_file " + j.prompt_file if j.prompt_file else "prompt")
        bits = [j.schedule.text, src]
        if j.model:
            bits.append(j.model)
        if j.worktree:
            bits.append("worktree")
        cwd = shift.job_cwd(cfg, j)
        flag = "" if cwd.exists() else bad(f"  cwd {cwd} does not exist")
        print(f"  {ok('✓')} {bold(j.name):<28} " + dim(", ".join(bits)) + flag)
        if paths.in_tcc_folder(cwd):
            print(warn(f"      note: {cwd} is in a folder macOS guards from background jobs. "
                       f"If it fails with 'Operation not permitted', give your Python Full Disk Access."))
    print(dim(f"\n{len(cfg.jobs)} job(s). Due right now: {', '.join(j.name for j in shift.due_jobs(cfg, now)) or 'none'}"))
    return 0


def cmd_status(a) -> int:
    cfg = load(a.config)
    now = clock.now()
    night = cfg.window.night_of(now)
    inside = cfg.window.contains(now)
    print(bold(f"lights-out {clock.hm(now)}") + "  " + (ok("inside the window") if inside else dim("outside the window"))
          + dim(f" ({cfg.window.text})"))
    why = shift.blocked(cfg, night)
    print("  blocked:  " + (warn(why) if why else ok("no")))
    h = state.holder("slot")
    print("  slot:     " + (warn(f"{h['owner']} since {str(h['started'])[11:16]}") if h else ok("free")))
    spent = shift.spent_tonight(night)
    cap = f" of ${cfg.nightly_budget_usd:g}" if cfg.nightly_budget_usd else ""
    print(f"  tonight:  ${spent:.2f}{cap}")
    print(f"  scheduler: " + (ok(schedulers.detect() + " installed") if schedulers.installed()
                              else warn("not installed (lightsout install)")))
    print()
    print(dim(f"  {'job':<24}{'schedule':<24}{'last result':<16}last done"))
    for j in sorted(cfg.jobs, key=lambda j: j.priority):
        st = state.job_state(j.name)
        res = st.get("last_result") or "-"
        col = {"ok": ok, "failure": bad, "auth_expired": bad}.get(res, warn if res != "-" else dim)
        if st.get("parked"):
            res, col = "parked", bad
        due = " " + c("due", "36") if shift.due_at(j, now) else ""
        print(f"  {j.name:<24}{j.schedule.text:<24}{col(f'{res:<16}')}{str(st.get('last_done') or '-')[:16].replace('T', ' ')}{due}")
    return 0


def cmd_tick(a) -> int:
    cfg = load(a.config)
    return shift.tick(cfg, log=lambda s: print(s, flush=True))


def cmd_run(a) -> int:
    cfg = load(a.config)
    job = cfg.job(a.job)
    now = clock.now()
    night = cfg.window.night_of(now)
    why = shift.blocked(cfg, night)
    if why and job.is_claude:
        print(warn(f"not started: {why}"))
        return 20
    if not a.force:
        reason = guards.check_all(cfg.guards, shift.job_cwd(cfg, job))
        if reason:
            print(warn(f"not started: {reason}") + dim("  (--force ignores the guards)"))
            return 10
    try:
        slot = state.Lock("slot", owner=job.name).acquire()
    except state.Busy as e:
        print(warn(f"the slot is busy: {e.reason}"))
        return 10
    try:
        res = shift.run_one(cfg, job, night, log=print)
    finally:
        slot.release()
    return {"ok": 0, "usage_limit": 20, "auth_expired": 21, "transient": 22, "skipped": 10}.get(res.kind, 1)


def _render(md: str) -> str:
    out = []
    for line in md.splitlines():
        if line.startswith("# "):
            out.append(bold(line[2:]))
        elif line.startswith("## "):
            t = line[3:]
            col = ok if t.startswith("Done") else bad if t.startswith(("Failed", "Parked", "Claude's login")) else warn
            out.append("\n" + col(bold(t)))
        elif line.startswith("> "):
            out.append(warn("│ ") + line[2:])
        elif line.startswith("  log:"):
            out.append(dim(line))
        elif line == "---":
            out.append(dim("─" * 60))
        else:
            out.append(line.replace("**", ""))
    return "\n".join(out)


def cmd_report(a) -> int:
    cfg = load(a.config)
    import datetime as dt
    night = dt.date.fromisoformat(a.night) if a.night else clock.now().date() - dt.timedelta(days=1)
    if a.write:
        p, md, _ = report.make(cfg, night, send=not a.no_notify)
        print(dim(f"written to {p}"))
    else:
        md, _ = report.build(night, cfg.window.text)
    print(_render(md))
    return 0


def cmd_logs(a) -> int:
    d = paths.logs_dir() / "jobs" / a.job
    logs = sorted(d.glob("*.log")) if d.is_dir() else []
    if not logs:
        print(f"no logs for {a.job} in {d}")
        return 1
    p = logs[-1]
    print(dim(str(p)))
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-a.lines:]:
        if line.startswith("{"):
            try:
                ev = json.loads(line)
            except ValueError:
                print(line)
                continue
            if ev.get("type") == "assistant":
                for part in (ev.get("message") or {}).get("content") or []:
                    if part.get("type") == "text":
                        print(part["text"][:300])
                    elif part.get("type") == "tool_use":
                        print(dim(f"  → {part.get('name')} {json.dumps(part.get('input'))[:120]}"))
            elif ev.get("type") == "result":
                print(bold(f"result: {str(ev.get('result'))[:400]}"))
        else:
            print(line)
    return 0


def cmd_reset(a) -> int:
    load(a.config).job(a.job)
    state.update_job_state(a.job, parked=False, fails=0)
    print(ok("✓ ") + f"{a.job} is back in the rotation")
    return 0


def cmd_lock(a) -> int:
    name = shift.dir_lock_name(a.folder)
    try:
        state.Lock(name, owner=f"hand:{a.owner}", minutes=a.minutes).acquire()
    except state.Busy as e:
        print(warn(f"not locked: {e.reason}"))
        return 10
    print(ok("✓ ") + f"locked {a.folder} for {a.minutes} min. No job will start in it. Renew by running this again.")
    return 0


def cmd_unlock(a) -> int:
    done = state.release_by_owner(shift.dir_lock_name(a.folder), f"hand:{a.owner}")
    print(ok("✓ unlocked") if done else "it was not locked by you")
    return 0


def cmd_install(a) -> int:
    cfg = load(a.config)
    print(schedulers.install(cfg.path.resolve(), cfg.tick_minutes, cfg.claude_bin, a.scheduler, a.dry_run))
    if not a.dry_run and sys.platform == "darwin":
        print(dim("\nA sleeping Mac runs nothing. To wake it for the window, once:\n"
                  f"  sudo pmset repeat wakeorpoweron MTWRFSU {cfg.window.start:%H:%M}:00"))
    return 0


def cmd_uninstall(a) -> int:
    print(schedulers.uninstall(a.scheduler))
    return 0


def cmd_doctor(a) -> int:
    problems = 0

    def line(good, text, hint=""):
        nonlocal problems
        problems += 0 if good else 1
        print((ok("✓ ") if good else bad("✗ ")) + text + (dim(f"  {hint}") if hint and not good else ""))

    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", "needs 3.9+ (3.11+ for TOML configs)")
    try:
        cfg = load(a.config)
        line(True, f"config {cfg.path} ({len(cfg.jobs)} jobs)")
    except ConfigError as e:
        line(False, f"config: {e}")
        cfg = None
    claude = shutil.which(cfg.claude_bin if cfg else "claude")
    line(bool(claude), f"claude found at {claude}" if claude else "claude not found on PATH",
         "install Claude Code: https://docs.claude.com/en/docs/claude-code")
    line(not paths.in_tcc_folder(paths.logs_dir()), f"logs in {paths.logs_dir()}",
         "launchd can't open logs in Downloads/Desktop/Documents")
    line(schedulers.installed(), f"scheduler ({schedulers.detect()}) installed", "run: lightsout install")
    if cfg:
        lim = state.limit_until()
        line(lim is None, "no usage limit pending" if not lim else f"usage limit until {clock.hm(lim)}")
        reason = guards.check_all(cfg.guards)
        print((ok("✓ ") + "guards say go right now") if not reason else (warn("• ") + f"guards say wait: {reason}"))
    print(dim(f"\nstate {paths.home()}"))
    return 1 if problems else 0


def cmd_classify(a) -> int:
    print(json.dumps(classify.classify(a.text, exit_code=1).as_dict()))
    return 0


def cmd_clean(a) -> int:
    for d in worktree.clean(a.days):
        print(f"removed {d}")
    return 0


# ── main ─────────────────────────────────────────────────────────────────────────────────────────

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="lightsout", description=__doc__)
    ap.add_argument("--config", default=str(paths.default_config()), help="default: %(default)s")
    ap.add_argument("--version", action="version", version=f"lights-out {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init", help="write a starter config")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)
    sub.add_parser("check", help="validate the config and list the jobs").set_defaults(fn=cmd_check)
    sub.add_parser("status", help="window, slot, limit, budget, every job's last result").set_defaults(fn=cmd_status)
    sub.add_parser("tick", help="what the scheduler runs: start due jobs if it is a good moment").set_defaults(fn=cmd_tick)
    p = sub.add_parser("run", help="run one job now (still one at a time, still respects the usage limit)")
    p.add_argument("job")
    p.add_argument("--force", action="store_true", help="ignore the busy/battery/idle guards")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("report", help="show last night's report")
    p.add_argument("--night", help="the evening date, YYYY-MM-DD (default: last night)")
    p.add_argument("--write", action="store_true", help="also write it to the reports folder and notify")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(fn=cmd_report)
    p = sub.add_parser("logs", help="the latest log of a job, readable")
    p.add_argument("job")
    p.add_argument("-n", "--lines", type=int, default=60)
    p.set_defaults(fn=cmd_logs)
    p = sub.add_parser("reset", help="put a parked job back in the rotation")
    p.add_argument("job")
    p.set_defaults(fn=cmd_reset)
    p = sub.add_parser("lock", help="keep jobs out of a folder you are working in")
    p.add_argument("folder")
    p.add_argument("--owner", default=os.environ.get("USER", "me"))
    p.add_argument("--minutes", type=int, default=180)
    p.set_defaults(fn=cmd_lock)
    p = sub.add_parser("unlock")
    p.add_argument("folder")
    p.add_argument("--owner", default=os.environ.get("USER", "me"))
    p.set_defaults(fn=cmd_unlock)
    for name, fn in (("install", cmd_install), ("uninstall", cmd_uninstall)):
        p = sub.add_parser(name, help=f"{name} the scheduler entry (launchd, systemd or cron)")
        p.add_argument("--scheduler", default="auto", choices=["auto", "launchd", "systemd", "cron"])
        if name == "install":
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)
    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("classify", help="what kind of error is this text?")
    p.add_argument("text")
    p.set_defaults(fn=cmd_classify)
    p = sub.add_parser("clean", help="remove old job worktrees (branches stay)")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(fn=cmd_clean)
    return ap


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    try:
        return a.fn(a) or 0
    except ConfigError as e:
        print(bad("config error: ") + str(e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
