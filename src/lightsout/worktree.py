"""`worktree = true`: the agent works in a fresh git worktree on its own branch, never in your checkout.

Your working copy, your uncommitted changes and your main branch are never touched. Whatever the agent
leaves uncommitted at the end is committed on its branch, and the morning report names the branch. Review
it with `git log lightsout/<job>/<stamp>` or `git diff main...lightsout/<job>/<stamp>`, merge what you like,
delete the rest. `lightsout clean` removes old worktrees (the branches stay).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import clock, paths


class WorktreeError(RuntimeError):
    pass


def _git(args, cwd, check=True) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120)
    if check and p.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout.strip()


def create(repo, job: str) -> tuple:
    """(worktree path, branch name, base commit)."""
    repo = Path(repo).expanduser()
    top = _git(["rev-parse", "--show-toplevel"], repo)
    stamp = clock.now().strftime("%Y%m%d-%H%M")
    branch = f"lightsout/{job}/{stamp}"
    dest = paths.worktrees_dir() / f"{Path(top).name}-{job}-{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    base = _git(["rev-parse", "HEAD"], top)
    _git(["worktree", "add", "-b", branch, str(dest), base], top)
    sub = repo.resolve().relative_to(Path(top).resolve())
    return dest / sub, branch, base


def finish(wt, job: str, base: str) -> int:
    """Commit whatever the agent left uncommitted. Returns how many commits the branch has over its base."""
    if _git(["status", "--porcelain"], wt, check=False):
        _git(["add", "-A"], wt)
        _git(["-c", "user.name=lights-out", "-c", "user.email=lights-out@localhost", "commit", "-q", "-m",
              f"lights-out: {job} ({clock.now():%Y-%m-%d %H:%M})"], wt)
    n = _git(["rev-list", "--count", f"{base}..HEAD"], wt, check=False)
    return int(n) if n.isdigit() else 0


def clean(older_than_days: int = 7) -> list:
    """Remove worktrees older than N days. Branches are kept."""
    removed = []
    base = paths.worktrees_dir()
    if not base.is_dir():
        return removed
    cutoff = clock.now().timestamp() - older_than_days * 86400
    for d in sorted(base.iterdir()):
        if d.is_dir() and d.stat().st_mtime < cutoff:
            gitfile = d / ".git"
            if gitfile.is_file():
                main = gitfile.read_text().split("gitdir:", 1)[-1].strip().split("/.git/worktrees/")[0]
                _git(["worktree", "remove", "--force", str(d)], main, check=False)
            removed.append(str(d))
    return removed
