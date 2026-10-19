import os
import stat
import unittest

from helpers import FAKE_CLAUDE, IsolatedTestCase
from lightsout import classify as C
from lightsout import runner, state


class RunnerTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.proj = self.make_proj()
        self.log = self.tmp / "run.log"

    def cfg(self, job=None, night=""):
        j = {"name": "j", "prompt": "do it", "cwd": str(self.proj)}
        j.update(job or {})
        cfg = self.write_config([j], night=night)
        return cfg, cfg.job("j")

    def go(self, job=None, night=""):
        cfg, j = self.cfg(job, night)
        return runner.run(cfg, j, self.log, cwd=self.proj)

    def test_ok(self):
        self.set_script("ok:All fixed")
        r = self.go()
        self.assertTrue(r.ok)
        self.assertEqual((r.attempts, r.retries, r.turns), (1, 0, 6))
        self.assertAlmostEqual(r.cost_usd, 0.42)
        self.assertEqual(r.result_text, "All fixed")
        self.assertEqual(r.login, "normal")

    def test_argv_and_prompt(self):
        self.set_script("ok:x")
        self.go({"model": "sonnet", "effort": "high", "max_budget_usd": 2.5,
                 "allowed_tools": ["Read", "Edit"], "extra_args": ["--foo"]})
        c = self.calls_argv()[0]
        a = c["argv"]
        self.assertEqual(c["prompt"], "do it")
        for flag, val in (("--model", "sonnet"), ("--effort", "high"), ("--max-budget-usd", "2.5")):
            self.assertEqual(a[a.index(flag) + 1], val)
        self.assertEqual(a[a.index("--allowedTools") + 1:a.index("--allowedTools") + 3], ["Read", "Edit"])
        self.assertEqual(a[-1], "--foo")
        self.assertEqual(os.path.realpath(c["cwd"]), str(self.proj))

    def test_transient_then_ok_resumes_session(self):
        self.set_script("transient", "ok:finished")
        r = self.go()
        self.assertTrue(r.ok)
        self.assertEqual((r.attempts, r.retries), (2, 1))
        first, second = self.calls_argv()
        self.assertNotIn("--resume", first["argv"])
        self.assertIn("--resume", second["argv"])
        self.assertIn("carry on", second["prompt"])
        self.assertAlmostEqual(r.cost_usd, 0.54)          # costs add up across attempts

    def test_transient_exhausts_retries(self):
        self.set_script("transient", "transient", "transient", "transient", "transient")
        r = self.go({"retries": 2})
        self.assertEqual(r.kind, C.TRANSIENT)
        self.assertEqual((r.attempts, r.retries), (3, 2))
        self.assertEqual(self.call_count(), 3)

    def test_backoff_schedule_passed_to_sleep(self):
        self.set_script("transient", "transient", "ok:x")
        waits = []
        cfg, j = self.cfg()
        os.environ["LIGHTSOUT_BACKOFF_SCALE"] = "2"
        r = runner.run(cfg, j, self.log, cwd=self.proj, sleep=waits.append)
        self.assertTrue(r.ok)
        self.assertEqual(waits, [60, 180])

    def test_real_failure_not_retried(self):
        self.set_script("fail:tests still red")
        r = self.go()
        self.assertEqual(r.kind, C.FAILURE)
        self.assertIn("tests still red", r.detail)
        self.assertEqual(self.call_count(), 1)

    def test_usage_limit_writes_file_and_blocks_next_run(self):
        self.set_script("limit:3am")
        r = self.go()
        self.assertEqual(r.kind, C.LIMIT)
        lim = state.limit_until()
        self.assertIsNotNone(lim)
        self.assertEqual((lim.hour, lim.minute), (3, C.LIMIT_MARGIN_MIN))
        before = self.call_count()
        r2 = self.go()
        self.assertTrue(r2.skipped)
        self.assertEqual(r2.kind, C.LIMIT)
        self.assertEqual(self.call_count(), before)

    def test_usage_limit_does_not_block_plain_commands(self):
        self.set_script("limit:3am")
        self.go()
        cfg, j = self.cfg({"prompt": "", "command": "echo hi"})
        r = runner.run(cfg, j, self.log, cwd=self.proj)
        self.assertTrue(r.ok)

    def test_auth_without_token(self):
        self.set_script("auth")
        r = self.go()
        self.assertEqual(r.kind, C.AUTH)
        self.assertEqual(r.attempts, 1)

    def _token(self, mode=0o600, text="x" * 30):
        p = self.tmp / "tok"
        p.write_text(text)
        os.chmod(p, mode)
        return p

    def test_token_rejected_falls_back_once(self):
        tok = self._token()
        self.set_script("auth", "ok:fine")
        r = self.go(night=f'token_file = "{tok}"')
        self.assertTrue(r.ok)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.login, "normal (token rejected)")
        self.assertTrue(any("token rejected" in n for n in r.notes))

    def test_token_fallback_only_once(self):
        tok = self._token()
        self.set_script("auth", "auth", "auth")
        r = self.go(night=f'token_file = "{tok}"')
        self.assertEqual(r.kind, C.AUTH)
        self.assertEqual(r.attempts, 2)

    def test_read_token_rules(self):
        cfg, _ = self.cfg(night=f'token_file = "{self._token()}"')
        self.assertEqual(runner.read_token(cfg), "x" * 30)
        cfg, _ = self.cfg(night=f'token_file = "{self._token(0o644)}"')
        self.assertIsNone(runner.read_token(cfg))                 # readable by others
        cfg, _ = self.cfg(night=f'token_file = "{self._token(0o600, "short")}"')
        self.assertIsNone(runner.read_token(cfg))
        exp = self._token(0o600, "export CLAUDE_CODE_OAUTH_TOKEN='" + "y" * 25 + "'")
        cfg, _ = self.cfg(night=f'token_file = "{exp}"')
        self.assertEqual(runner.read_token(cfg), "y" * 25)
        cfg, _ = self.cfg(night=f'token_file = "{self.tmp / "absent"}"')
        self.assertIsNone(runner.read_token(cfg))

    def test_env_hides_frozen_clock_and_marks_unattended(self):
        env = runner._env("tok-value")
        self.assertNotIn("LIGHTSOUT_NOW", env)
        self.assertEqual(env["LIGHTSOUT"], "1")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-value")

    def test_no_time_left(self):
        r = self.go({"timeout_minutes": 0})
        self.assertEqual(r.kind, C.FAILURE)
        self.assertEqual(self.call_count(), 0)

    def test_missing_binary_is_failure(self):
        cfg, j = self.cfg(night=f'')
        cfg.claude_bin = str(self.tmp / "does-not-exist")
        r = runner.run(cfg, j, self.log, cwd=self.proj)
        self.assertEqual(r.kind, C.FAILURE)
        self.assertEqual(r.exit, 127)

    def test_command_jobs(self):
        ok = self.go({"prompt": "", "command": "echo hello"})
        self.assertTrue(ok.ok)
        self.assertEqual(ok.result_text, "hello")
        bad = self.go({"prompt": "", "command": "echo broke; exit 3"})
        self.assertEqual(bad.kind, C.FAILURE)
        self.assertEqual(bad.exit, 3)
        tr = self.go({"prompt": "", "command": "echo ECONNRESET; exit 1", "retries": 1})
        self.assertEqual((tr.kind, tr.retries), (C.TRANSIENT, 1))

    def test_prompt_file(self):
        (self.tmp / "p.md").write_text("from file")
        self.set_script("ok:x")
        self.go({"prompt": "", "prompt_file": "p.md"})
        self.assertEqual(self.calls_argv()[0]["prompt"], "from file")

    def test_as_dict_serialisable(self):
        import json
        self.set_script("limit:3am")
        json.dumps(self.go().as_dict())


if __name__ == "__main__":
    unittest.main()
