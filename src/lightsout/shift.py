"""The tick: what the scheduler (launchd, systemd or cron) runs every few minutes, all day.

Almost every tick is a cheap no-op. One that lands inside the night window, with a job due, nothing
known to be blocking (usage limit, expired login, nightly budget) and every guard saying "go", takes
THE SLOT and runs the due jobs one after another, best priority first. Between jobs it checks again,
so starting a call or plugging out the charger stops the next job from starting.

Why one tick instead of one scheduler entry per job: launchd silently skips a calendar job that carries
a weekday when the Mac is asleep at that moment (it does not catch it up on wake, it waits a week).
A frequent tick plus "is it due, was it missed" logic here catches every missed run, on every OS, the
same way.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import classify as C
from . import clock, guards, paths, report, runner, state, worktree
from .config import Config, Job

AUTH_PAUSE_MIN = 60


def _since(st: dict, key: str):
    return clock.parse_iso(st.get(key))


def due_at(job: Job, now: dt.datetime):
    """When this job became due (a datetime), or None when it isn't due now."""
    if not job.enabled:
        return None
    st = state.job_state(job.name)
    if st.get("parked"):
        return None
    last = _since(st, "last_done")
    s = job.schedule
    if s.kind == "manual":
        return None
    if s.kind == "every":
        if last is None:
            return now
        nxt = last + dt.timedelta(minutes=s.every_min)
        return nxt if nxt <= now else None
    occ = s.last_due(now)
    if occ is None or (now - occ) > dt.timedelta(hours=job.catch_up_hours):
        return None
    return occ if last is None or last < occ else None


def due_jobs(cfg: Config, now=None) -> list:
    now = now or clock.now()
    out = [(j, t) for j in cfg.jobs for t in [due_at(j, now)] if t is not None]
    return [j for j, t in sorted(out, key=lambda x: (x[0].priority, x[1]))]


def spent_tonight(night: dt.date) -> float:
    return round(sum(float(e.get("cost_usd") or 0) for e in report.load(night)), 4)


def auth_pause_until():
    d = state.read_json(paths.state_dir() / "auth.json") or {}
    t = clock.parse_iso(d.get("retry_after"))
    return t if t and t > clock.now() else None


def blocked(cfg: Config, night: dt.date):
    """A reason no job may start right now, or None."""
    lim = state.limit_until()
    if lim:
        return f"usage limit reached, nothing starts before {clock.hm(lim)}"
    ap = auth_pause_until()
    if ap:
        return f"Claude's login expired; trying again at {clock.hm(ap)} (run `claude` once to log in)"
    if cfg.nightly_budget_usd and spent_tonight(night) >= cfg.nightly_budget_usd:
        return f"tonight's budget of ${cfg.nightly_budget_usd:g} is used up"
    return None


def note_skip(night: dt.date, job: Job, reason: str) -> None:
    """File a "not started" once per reason, not once per tick: a two-hour call is one line in the report."""
    if state.job_state(job.name).get("last_skip") != reason:
        state.update_job_state(job.name, last_skip=reason)
        report.record(night, job.name, "skipped", reason)


def job_cwd(cfg: Config, job: Job) -> Path:
    return cfg.resolve(job.cwd) if job.cwd else cfg.path.parent


def dir_lock_name(p) -> str:
    return "dir:" + str(Path(p).expanduser().resolve())


def run_one(cfg: Config, job: Job, night: dt.date, log=print) -> runner.RunResult:
    """Run one job now (the caller holds the slot) and file the outcome."""
    now = clock.now()
    cwd = job_cwd(cfg, job)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    log_path = paths.logs_dir() / "jobs" / job.name / f"{stamp}.log"
    state.update_job_state(job.name, last_started=clock.iso(now), last_skip=None)
    try:
        dirlock = state.Lock(dir_lock_name(cwd), owner=f"job:{job.name}").acquire()
    except state.Busy as e:
        report.record(night, job.name, "skipped", f"its folder is locked by {e.holder.get('owner') if e.holder else 'someone'}")
        log(f"  {job.name}: folder locked ({e.reason}), skipped")
        return runner.RunResult(kind="skipped", skipped=True)
    branch, base, run_cwd = "", "", cwd
    try:
        if job.worktree:
            try:
                run_cwd, branch, base = worktree.create(cwd, job.name)
            except worktree.WorktreeError as e:
                res = runner.RunResult(kind=C.FAILURE, detail=f"could not make a git worktree: {e}")
                return _file(cfg, job, night, res, log_path, log)
        log(f"  {job.name}: started{' on ' + branch if branch else ''}")
        res = runner.run(cfg, job, log_path, cwd=run_cwd)
        if branch:
            res.branch = branch
            try:
                n = worktree.finish(run_cwd, job.name, base)
                res.notes.append(f"{n} commit(s) on {branch}")
            except worktree.WorktreeError as e:
                res.notes.append(f"could not commit on {branch}: {e}")
        return _file(cfg, job, night, res, log_path, log)
    finally:
        dirlock.release()


def _first_line(s: str, n: int = 160) -> str:
    for line in (s or "").splitlines():
        if line.strip():
            return line.strip()[:n]
    return ""


def _file(cfg, job, night, res, log_path, log) -> runner.RunResult:
    now = clock.now()
    st = state.job_state(job.name)
    outcome, detail = res.kind, res.detail
    if res.kind == C.OK:
        state.update_job_state(job.name, last_done=clock.iso(now), last_ok=clock.iso(now), fails=0,
                               last_result="ok")
        detail = _first_line(res.result_text)
    elif res.kind == C.FAILURE:
        fails = int(st.get("fails") or 0) + 1
        parked = fails >= job.give_up_after
        state.update_job_state(job.name, last_done=clock.iso(now), fails=fails, parked=parked, last_result="failure")
        if parked:
            outcome = "parked"
            detail = f"failed {fails} times in a row, parked. Last error: {detail}"
    elif res.kind == C.AUTH:
        state.write_json(paths.state_dir() / "auth.json",
                         {"retry_after": clock.iso(now + dt.timedelta(minutes=AUTH_PAUSE_MIN)), "detail": detail})
        state.update_job_state(job.name, last_result="auth_expired")
    else:
        state.update_job_state(job.name, last_result=res.kind)       # limit / transient: due again next tick
    report.record(night, job.name, outcome, detail, minutes=res.minutes, cost_usd=res.cost_usd, retries=res.retries,
                  branch=res.branch, resume_after=clock.iso(res.resume_after), log=str(log_path),
                  notes="; ".join(res.notes))
    retr = f", {res.retries} retr{'y' if res.retries == 1 else 'ies'}" if res.retries else ""
    extra = f" ({report.fmt_min(res.minutes)}, ${res.cost_usd:.2f}{retr})"
    if outcome == C.LIMIT:
        said = f"hit the usage limit; every job waits until {clock.hm(res.resume_after)}"
    else:
        said = {"ok": "done", "failure": "FAILED", "parked": "PARKED", "auth_expired": "login expired",
                "transient": "still failing after retries, tries again next tick"}.get(outcome, outcome)
        if detail:
            said += f": {detail[:120]}"
    log(f"  {job.name}: {said}{extra}")
    return res


def tick(cfg: Config, log=print) -> int:
    now = clock.now()
    night = cfg.window.night_of(now)
    rn = report.due(cfg, now)
    if rn:
        p, _, summary = report.make(cfg, rn)
        log(f"morning report written: {p}  ({summary})")
    if not cfg.window.contains(now):
        log(f"{clock.hm(now)} outside the window {cfg.window.text}: nothing to start")
        return 0
    why = blocked(cfg, night)
    if why:
        log(f"{clock.hm(now)} waiting: {why}")
        return 0
    jobs = due_jobs(cfg, now)
    if not jobs:
        log(f"{clock.hm(now)} nothing due")
        return 0
    try:
        ticklock = state.Lock("tick", owner="tick").acquire()
    except state.Busy:
        log(f"{clock.hm(now)} another tick is still working; this one leaves")
        return 0
    try:
        net = guards.wait_for_network(cfg.guards.wait_for_network_s)
        if net:
            for j in jobs:
                note_skip(night, j, net)
            log(f"{clock.hm(now)} {net}")
            return 0
        for job in jobs:
            now = clock.now()
            if not cfg.window.contains(now):
                log(f"{clock.hm(now)} the window closed; the rest waits for tomorrow")
                break
            why = blocked(cfg, night)
            if why:
                log(f"{clock.hm(now)} stopping: {why}")
                break
            if due_at(job, now) is None:                   # a hand run did it meanwhile
                continue
            reason = guards.check_all(cfg.guards, job_cwd(cfg, job))
            if reason:
                for j in jobs[jobs.index(job):]:
                    note_skip(night, j, reason)
                log(f"{clock.hm(now)} not starting: {reason}")
                break
            try:
                slot = state.Lock("slot", owner=job.name).acquire()
            except state.Busy as e:
                log(f"{clock.hm(now)} the slot is busy ({e.reason}); my turn at a later tick")
                break
            try:
                run_one(cfg, job, night, log)
            finally:
                slot.release()
    finally:
        ticklock.release()
    return 0
