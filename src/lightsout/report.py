"""The morning report: one plain-English page about last night, and one notification.

Every job firing writes one event line (state/events-<night>.jsonl). After `report_at` the tick turns last
night's events into reports/<morning date>.md: what finished, what failed and why, what was waiting for the
usage limit, what was skipped because you were busy, and what needs you.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import clock, paths

OUTCOMES = {
    "ok": "Done",
    "failure": "Failed",
    "parked": "Parked after failing in a row (run `lightsout reset <job>` once fixed)",
    "usage_limit": "Waiting for the usage limit (not a failure)",
    "auth_expired": "Claude's login had expired (run `claude` once and log in)",
    "transient": "Temporary errors, still failing after the retries; tries again next firing",
    "skipped": "Not started",
}
ORDER = list(OUTCOMES)


def fmt_min(m: float) -> str:
    if m < 1:
        return "under a minute"
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def events_file(night: dt.date) -> Path:
    return paths.state_dir() / f"events-{night.isoformat()}.jsonl"


def record(night: dt.date, job: str, outcome: str, detail: str = "", **extra) -> dict:
    d = {"at": clock.iso(clock.now()), "job": job, "outcome": outcome, "detail": (detail or "")[:400], **extra}
    p = events_file(night)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return d


def load(night: dt.date) -> list:
    out = []
    try:
        for line in events_file(night).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _last_per_job(ev: list) -> dict:
    """A job can fire many times a night (a busy skip, then a retry, then success). The report shows how each
    job ENDED the night, plus the useful history (how many skips, retries) in the line."""
    by: dict = {}
    for e in ev:
        by.setdefault(e["job"], []).append(e)
    return by


def build(night: dt.date, window_text: str = "") -> tuple:
    """(markdown, one-line summary)."""
    ev = load(night)
    morning = night + dt.timedelta(days=1)
    by = _last_per_job(ev)
    final = {j: es[-1] for j, es in by.items()}
    for j, es in by.items():                                  # a job that ran ok tonight ended ok, whatever came after
        oks = [e for e in es if e["outcome"] == "ok"]
        if oks and final[j]["outcome"] == "skipped":
            final[j] = oks[-1]
    cost = sum(float(e.get("cost_usd") or 0) for e in ev)
    mins = sum(float(e.get("minutes") or 0) for e in ev)
    count = {k: sum(1 for e in final.values() if e["outcome"] == k) for k in ORDER}
    L = [f"# Night report, morning of {morning:%a %d %b %Y}", ""]
    if window_text:
        L += [f"Window {window_text}. {len(final)} job(s), {fmt_min(mins)} of agent time, ${cost:.2f} reported cost.", ""]
    if not ev:
        L += ["Nothing ran. No job was due, or every check said wait.", ""]
    limit = [e for e in ev if e["outcome"] == "usage_limit" and e.get("resume_after")]
    if limit:
        L += [f"> Usage limit reached at {str(limit[0]['at'])[11:16]}. Every job paused until "
              f"{str(max(e['resume_after'] for e in limit))[11:16]}, then carried on. Nothing was marked failed for it.", ""]
    for key in ORDER:
        sel = [(j, e) for j, e in final.items() if e["outcome"] == key]
        if not sel:
            continue
        L += [f"## {OUTCOMES[key]}", ""]
        for j, e in sorted(sel, key=lambda x: x[1]["at"]):
            es = by[j]
            bits = []
            if e.get("minutes"):
                bits.append(fmt_min(float(e["minutes"])))
            if e.get("cost_usd"):
                bits.append(f"${float(e['cost_usd']):.2f}")
            retries = sum(int(x.get("retries") or 0) for x in es)
            if retries:
                bits.append(f"{retries} retr{'y' if retries == 1 else 'ies'}")
            skips = sum(1 for x in es if x["outcome"] == "skipped")
            if skips and key != "skipped":
                bits.append(f"waited {skips}x")
            if e.get("branch"):
                bits.append(f"branch `{e['branch']}`")
            line = f"- **{j}** {str(e['at'])[11:16]}"
            if bits:
                line += " (" + ", ".join(bits) + ")"
            if e.get("detail"):
                line += f": {e['detail']}"
            L.append(line)
            if e.get("log") and key != "ok":
                L.append(f"  log: `{e['log']}`")
        L.append("")
    parts = [f"{count[k]} {w}" for k, w in (("ok", "done"), ("failure", "failed"), ("parked", "parked"),
                                           ("usage_limit", "waiting for the limit"), ("auth_expired", "login expired"),
                                           ("transient", "temporary errors"), ("skipped", "not started")) if count[k]]
    summary = "Last night: " + (", ".join(parts) if parts else "nothing ran")
    L += ["---", summary, ""]
    return "\n".join(L), summary


def notify(title: str, msg: str) -> None:
    if os.environ.get("LIGHTSOUT_NO_NOTIFY") == "1":
        return
    try:
        if sys.platform == "darwin":
            s = msg.replace("\\", "\\\\").replace('"', "'")
            subprocess.run(["/usr/bin/osascript", "-e", f'display notification "{s[:230]}" with title "{title}"'],
                           capture_output=True, timeout=15)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, msg[:230]], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def report_path(morning: dt.date) -> Path:
    return paths.reports_dir() / f"{morning.isoformat()}.md"


def make(cfg, night: dt.date, send: bool = True) -> tuple:
    md, summary = build(night, cfg.window.text)
    p = report_path(night + dt.timedelta(days=1))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(md, encoding="utf-8")
    if send and cfg.notify:
        notify("lights-out", summary)
    if send and cfg.report_command:
        try:
            subprocess.run(["/bin/sh", "-c", cfg.report_command, "report", str(p)], timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return p, md, summary


def due(cfg, now=None):
    """The night to report on, when its report is due and not written yet; else None."""
    now = now or clock.now()
    if cfg.window.contains(now) or now.time() < cfg.report_at:
        return None
    night = now.date() - dt.timedelta(days=1)                # events are filed under the evening they began
    return None if report_path(night + dt.timedelta(days=1)).exists() else night
