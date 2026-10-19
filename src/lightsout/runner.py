"""Run one job: a headless `claude -p` session (or a plain command) with retries that know what went wrong.

  * transient errors: up to `retries` more tries in the same firing, waiting 30 s, 90 s, 240 s. When the
    session had already got somewhere, the retry RESUMES it (`--resume <id>`) instead of starting over.
  * usage limit: written to state/limit.json so no job starts anything before the reset. Not a failure.
  * login expired: if a long-lived token file is configured and it was rejected, the run is repeated once
    with the normal login. Otherwise reported, not counted as a failure.
  * the whole firing has one wall-clock deadline (timeout_minutes). Waits are measured on the wall clock,
    not by counting sleeps: a laptop that sleeps mid-wait must not turn "15 minutes" into 20 hours.
  * on macOS the child runs under `caffeinate -i`, so the Mac stays awake while a job works and only then.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import classify as C
from . import clock, state
from .config import Config, Job

BACKOFF_S = (30, 90, 240)
RESUME_PROMPT = ("A temporary API error interrupted this session. Check what is already on disk, then carry on "
                 "with the task exactly where it stopped until it is finished. Do not redo finished work.")


@dataclass
class RunResult:
    kind: str = C.OK
    detail: str = ""
    resume_after: object = None
    exit: int | None = None
    minutes: float = 0.0
    cost_usd: float = 0.0
    turns: int | None = None
    session_id: str | None = None
    result_text: str = ""
    attempts: int = 0
    retries: int = 0
    login: str = "normal"
    skipped: bool = False
    branch: str = ""
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind == C.OK

    def as_dict(self) -> dict:
        d = asdict(self)
        d["resume_after"] = clock.iso(self.resume_after)
        return d


def read_token(cfg: Config) -> str | None:
    """The optional long-lived login (`claude setup-token`). Read from a file only you can read; never
    printed, logged or copied anywhere else."""
    if not cfg.token_file:
        return None
    p = Path(cfg.token_file).expanduser()
    try:
        if p.stat().st_mode & 0o077:
            return None                                   # readable by others: refuse to use it
        tok = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if tok.startswith("export "):
        tok = tok.split("=", 1)[-1].strip().strip("'\"")
    return tok if len(tok) >= 20 and not any(c.isspace() for c in tok) else None


def _env(token: str | None) -> dict:
    env = dict(os.environ)
    env.pop("LIGHTSOUT_NOW", None)
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    env["LIGHTSOUT"] = "1"                                # lets a job's own hooks know it runs unattended
    return env


def _spawn(cmd, stdin_text, log_path, cwd, env, timeout_s, append, keep_awake) -> tuple:
    if keep_awake and sys.platform == "darwin" and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-i"] + list(cmd)
    t0 = time.time()
    timed_out = False
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a" if append else "w", encoding="utf-8") as out:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=out, stderr=subprocess.STDOUT,
                                 cwd=str(cwd) if cwd else None, env=env, start_new_session=True, text=True)
        except OSError as e:
            out.write(f"lightsout: could not start {cmd[0]!r}: {e}\n")
            return 127, False, 0.0
        try:
            if stdin_text:
                p.stdin.write(stdin_text)
            p.stdin.close()
        except BrokenPipeError:
            pass
        try:
            p.wait(timeout=max(1, timeout_s))
        except subprocess.TimeoutExpired:
            timed_out = True
            for sig in (signal.SIGTERM, signal.SIGKILL):          # the whole process group, not just claude
                try:
                    os.killpg(p.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    p.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    continue
    return p.returncode, timed_out, round((time.time() - t0) / 60, 2)


def claude_cmd(cfg: Config, job: Job) -> list:
    cmd = [cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose"]
    if job.model:
        cmd += ["--model", job.model]
    if job.effort:
        cmd += ["--effort", job.effort]
    if job.permission_mode:
        cmd += ["--permission-mode", job.permission_mode]
    if job.allowed_tools:
        cmd += ["--allowedTools", *job.allowed_tools]
    if job.disallowed_tools:
        cmd += ["--disallowedTools", *job.disallowed_tools]
    if job.max_budget_usd:
        cmd += ["--max-budget-usd", str(job.max_budget_usd)]
    return cmd + list(job.extra_args)


def prompt_text(cfg: Config, job: Job) -> str:
    if job.prompt:
        return job.prompt
    return cfg.resolve(job.prompt_file).read_text(encoding="utf-8")


def run(cfg: Config, job: Job, log_path, *, cwd=None, sleep=time.sleep, backoff=BACKOFF_S) -> RunResult:
    """Never raises for a failed run: look at .kind."""
    res = RunResult()
    lim = state.limit_until()
    if lim and job.is_claude:
        res.kind, res.skipped, res.resume_after = C.LIMIT, True, lim
        res.detail = f"usage limit already reached; nothing starts before {clock.hm(lim)}"
        return res
    scale = float(os.environ.get("LIGHTSOUT_BACKOFF_SCALE", "1"))
    deadline = time.time() + job.timeout_minutes * 60
    token = read_token(cfg) if job.is_claude else None
    res.login = "token" if token else "normal"
    base = claude_cmd(cfg, job) if job.is_claude else list(job.command)
    stdin = prompt_text(cfg, job) if job.is_claude else ""
    resume = None
    first = True
    while True:
        left = deadline - time.time()
        if left < 30:
            res.kind, res.detail = C.FAILURE, "no time left inside the job's timeout"
            break
        cmd = base + (["--resume", resume] if resume else [])
        res.attempts += 1
        code, timed_out, mins = _spawn(cmd, stdin, log_path, cwd, _env(token), left, not first, cfg.keep_awake)
        first = False
        res.exit, res.minutes = code, round(res.minutes + mins, 2)
        if job.is_claude:
            info = C.read_result(log_path)
            res.session_id = info.get("session_id") or res.session_id
            res.cost_usd = round(res.cost_usd + float(info.get("cost_usd") or 0), 4)
            res.turns = info.get("turns", res.turns)
            res.result_text = (info.get("result_text") or "")[:300]
            text = info.get("result_text") or info.get("stray_text") or ""
            v = C.classify(text, exit_code=code or 0, is_error=info.get("is_error") if "is_error" in info else None,
                           api_status=info.get("api_status"), timed_out=timed_out)
        else:
            out = Path(log_path).read_text(encoding="utf-8", errors="replace")
            v = C.scan(out[-4000:], code or 0) if not timed_out else C.Verdict(C.FAILURE, "stopped at the job's time limit")
            res.result_text = "\n".join(out.strip().splitlines()[-3:])[:300]
        res.kind, res.detail, res.resume_after = v.kind, v.detail, v.resume_after
        if v.kind == C.AUTH and token:
            res.notes.append("long-lived token rejected: repeated once with the normal login")
            token, res.login = None, "normal (token rejected)"
            continue                                          # a different login, not a retry
        if v.kind == C.TRANSIENT and res.retries < job.retries:
            wait = backoff[min(res.retries, len(backoff) - 1)] * scale
            res.retries += 1
            res.notes.append(f"temporary error ({v.detail[:60]}), retry {res.retries}/{job.retries} after {wait:g}s")
            if time.time() + wait >= deadline - 30:
                break
            sleep(wait)
            if job.is_claude and res.session_id and (res.turns or 0) > 1:
                resume, stdin = res.session_id, RESUME_PROMPT     # it got somewhere: carry on that session
            continue
        break
    if res.kind == C.LIMIT:
        state.set_limit(res.resume_after, job.name, res.detail)
    return res
