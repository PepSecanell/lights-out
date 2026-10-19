"""Shared test scaffolding: an isolated environment and a tiny config writer."""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAKE_CLAUDE = str(HERE / "fake_claude.py")

_ENV_KEYS = ["LIGHTSOUT_NOW", "LIGHTSOUT_HOME", "LIGHTSOUT_BACKOFF_SCALE", "LIGHTSOUT_NO_NOTIFY",
             "LIGHTSOUT_SKIP_NETWORK", "LIGHTSOUT_CONFIG", "FAKE_CLAUDE_SCRIPT", "FAKE_CLAUDE_CALLS",
             "FAKE_CLAUDE_DELAY"]


class IsolatedTestCase(unittest.TestCase):
    """Restores os.environ afterwards, gives every test a temp dir and a state home inside it."""

    def setUp(self):
        self._saved_env = dict(os.environ)
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        self.home = self.tmp / "home"
        os.environ["LIGHTSOUT_HOME"] = str(self.home)
        os.environ["LIGHTSOUT_BACKOFF_SCALE"] = "0"
        os.environ["LIGHTSOUT_NO_NOTIFY"] = "1"
        os.environ["LIGHTSOUT_NOW"] = "2026-10-18T23:05"
        self.script = self.tmp / "script"
        self.calls = self.tmp / "calls"
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(self.script)
        os.environ["FAKE_CLAUDE_CALLS"] = str(self.calls)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    # -- helpers
    def at(self, iso):
        os.environ["LIGHTSOUT_NOW"] = iso

    def set_script(self, *lines):
        self.script.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def call_count(self):
        if not self.calls.exists():
            return 0
        return len(self.calls.read_text().splitlines())

    def calls_argv(self):
        import json
        if not self.calls.exists():
            return []
        return [json.loads(x) for x in self.calls.read_text().splitlines()]

    def write_config(self, jobs, night="", guards=None, name="cfg.toml"):
        """jobs: list of dicts (values: str, int, float, bool, list of str). Returns the loaded Config."""
        from lightsout import config
        p = self.write_config_file(jobs, night, guards, name)
        return config.load(p)

    def write_config_file(self, jobs, night="", guards=None, name="cfg.toml"):
        g = {"wait_for_network_s": 0, "on_battery": "allow", "min_free_disk_gb": 0}
        g.update(guards or {})
        out = ["[night]", 'window = "22:00-06:00"', f'claude_bin = "{FAKE_CLAUDE}"', "notify = false"]
        if night:
            out.append(night)
        out.append("[guards]")
        out += [f"{k} = {_v(v)}" for k, v in g.items()]
        for j in jobs:
            out.append("[[job]]")
            out += [f"{k} = {_v(v)}" for k, v in j.items()]
        p = self.tmp / name
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
        return p

    def make_proj(self, name="proj"):
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        return d


def _v(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_v(x) for x in v) + "]"
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def have_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                        *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return r.stdout.strip()


def make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "a.txt").write_text("one\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def T(s):
    return dt.datetime.fromisoformat(s).astimezone()
