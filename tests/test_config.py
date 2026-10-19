import datetime as dt
import json
import unittest

from helpers import IsolatedTestCase, T
from lightsout import config
from lightsout.config import ConfigError, parse_schedule, parse_window


class ScheduleTests(unittest.TestCase):
    def test_daily_forms(self):
        for text in ("daily 23:00", "23:00"):
            s = parse_schedule(text)
            self.assertEqual((s.kind, s.at), ("daily", dt.time(23, 0)))

    def test_weekly_days_sorted_and_deduped(self):
        s = parse_schedule("weekly thu,mon,mon 01:30")
        self.assertEqual(s.kind, "weekly")
        self.assertEqual(s.days, (0, 3))
        self.assertEqual(s.at, dt.time(1, 30))

    def test_weekly_long_day_names(self):
        self.assertEqual(parse_schedule("weekly monday,friday 09:00").days, (0, 4))

    def test_every(self):
        self.assertEqual(parse_schedule("every 2h").every_min, 120)
        self.assertEqual(parse_schedule("every 45m").every_min, 45)
        self.assertEqual(parse_schedule("hourly").every_min, 60)

    def test_manual(self):
        for text in ("manual", "", "never"):
            self.assertEqual(parse_schedule(text).kind, "manual")

    def test_errors(self):
        for bad in ("every 5m", "daily 25:00", "daily 12:60", "weekly xyz 10:00", "whenever", "weekly 10:00"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_schedule(bad)

    def test_last_due_daily(self):
        s = parse_schedule("daily 23:00")
        self.assertEqual(s.last_due(T("2026-10-18T23:05")), T("2026-10-18T23:00"))
        self.assertEqual(s.last_due(T("2026-10-18T22:59")), T("2026-10-17T23:00"))

    def test_last_due_weekly(self):
        s = parse_schedule("weekly mon 01:00")      # 2026-10-19 is a Monday
        self.assertEqual(s.last_due(T("2026-10-19T02:00")), T("2026-10-19T01:00"))
        self.assertEqual(s.last_due(T("2026-10-21T12:00")), T("2026-10-19T01:00"))
        self.assertEqual(s.last_due(T("2026-10-19T00:30")), T("2026-10-12T01:00"))

    def test_last_due_none_for_every_and_manual(self):
        self.assertIsNone(parse_schedule("every 1h").last_due(T("2026-10-18T23:00")))
        self.assertIsNone(parse_schedule("manual").last_due(T("2026-10-18T23:00")))


class WindowTests(unittest.TestCase):
    def test_crossing_midnight(self):
        w = parse_window("22:00-06:00")
        for t, want in (("23:30", True), ("00:00", True), ("05:59", True), ("06:00", False),
                        ("12:00", False), ("21:59", False), ("22:00", True)):
            self.assertEqual(w.contains(T("2026-10-18T" + t)), want, t)

    def test_same_day(self):
        w = parse_window("09:00-17:00")
        self.assertTrue(w.contains(T("2026-10-18T09:00")))
        self.assertFalse(w.contains(T("2026-10-18T17:00")))

    def test_equal_means_always(self):
        self.assertTrue(parse_window("00:00-00:00").contains(T("2026-10-18T13:00")))

    def test_night_of(self):
        w = parse_window("22:00-06:00")
        self.assertEqual(w.night_of(T("2026-10-19T01:00")), dt.date(2026, 10, 18))
        self.assertEqual(w.night_of(T("2026-10-18T23:00")), dt.date(2026, 10, 18))

    def test_closes_at(self):
        w = parse_window("22:00-06:00")
        self.assertEqual(w.closes_at(T("2026-10-18T23:00")), T("2026-10-19T06:00"))
        self.assertEqual(w.closes_at(T("2026-10-19T01:00")), T("2026-10-19T06:00"))

    def test_bad(self):
        for bad in ("", "22-06", "25:00-06:00", "nonsense"):
            with self.assertRaises(ConfigError, msg=bad):
                parse_window(bad)


class LoadTests(IsolatedTestCase):
    def test_minimal_defaults(self):
        cfg = self.write_config([{"name": "a", "prompt": "hi"}])
        j = cfg.job("a")
        self.assertEqual(j.schedule.kind, "manual")
        self.assertEqual((j.retries, j.give_up_after, j.priority, j.timeout_minutes), (3, 3, 5, 60))
        self.assertTrue(j.is_claude)
        self.assertEqual(cfg.window.text, "22:00-06:00")
        self.assertEqual(cfg.report_at, dt.time(7, 30))

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            config.load(self.tmp / "nope.toml")

    def test_report_at_and_night_keys(self):
        cfg = self.write_config([{"name": "a", "prompt": "x", "model": "sonnet"}],
                                night='report_at = "08:15"\nnightly_budget_usd = 5.0\nallowed_models = ["sonnet"]')
        self.assertEqual(cfg.report_at, dt.time(8, 15))
        self.assertEqual(cfg.nightly_budget_usd, 5.0)
        self.assertEqual(cfg.allowed_models, ["sonnet"])

    def test_unknown_keys_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self.write_config([{"name": "a", "prompt": "x", "bogus": 1}])
        with self.assertRaisesRegex(ConfigError, "night"):
            self.write_config([{"name": "a", "prompt": "x"}], night="nope = 1")
        with self.assertRaisesRegex(ConfigError, "guards"):
            self.write_config([{"name": "a", "prompt": "x"}], guards={"nope": 1})

    def test_bad_on_battery(self):
        with self.assertRaises(ConfigError):
            self.write_config([{"name": "a", "prompt": "x"}], guards={"on_battery": "maybe"})

    def test_job_names(self):
        for bad in ("", "has space", "-lead", "a/b"):
            with self.assertRaises(ConfigError, msg=bad):
                self.write_config([{"name": bad, "prompt": "x"}])
        with self.assertRaisesRegex(ConfigError, "two jobs"):
            self.write_config([{"name": "a", "prompt": "x"}, {"name": "a", "prompt": "y"}])

    def test_exactly_one_source(self):
        with self.assertRaisesRegex(ConfigError, "exactly one"):
            self.write_config([{"name": "a"}])
        with self.assertRaisesRegex(ConfigError, "exactly one"):
            self.write_config([{"name": "a", "prompt": "x", "command": "echo"}])

    def test_command_string_becomes_shell(self):
        j = self.write_config([{"name": "a", "command": "echo hi"}]).job("a")
        self.assertEqual(j.command, ["/bin/sh", "-c", "echo hi"])
        self.assertFalse(j.is_claude)

    def test_prompt_file(self):
        with self.assertRaisesRegex(ConfigError, "not found"):
            self.write_config([{"name": "a", "prompt_file": "missing.md"}])
        (self.tmp / "p.md").write_text("hello")
        cfg = self.write_config([{"name": "a", "prompt_file": "p.md"}])
        self.assertEqual(cfg.resolve("p.md"), self.tmp / "p.md")

    def test_bypass_permissions_needs_worktree(self):
        with self.assertRaisesRegex(ConfigError, "worktree"):
            self.write_config([{"name": "a", "prompt": "x", "permission_mode": "bypassPermissions"}])
        cfg = self.write_config([{"name": "a", "prompt": "x", "permission_mode": "bypassPermissions",
                                  "worktree": True}])
        self.assertTrue(cfg.job("a").worktree)

    def test_allowed_models(self):
        night = 'allowed_models = ["sonnet", "haiku"]'
        with self.assertRaisesRegex(ConfigError, "must name its model"):
            self.write_config([{"name": "a", "prompt": "x"}], night=night)
        with self.assertRaisesRegex(ConfigError, "not in allowed_models"):
            self.write_config([{"name": "a", "prompt": "x", "model": "opus"}], night=night)
        self.write_config([{"name": "a", "prompt": "x", "model": "claude-sonnet-4"}], night=night)

    def test_allowed_models_exempts_command_jobs(self):
        self.write_config([{"name": "c", "command": "echo hi"}], night='allowed_models = ["sonnet"]')

    def test_unknown_job_lookup(self):
        cfg = self.write_config([{"name": "a", "prompt": "x"}])
        with self.assertRaises(ConfigError):
            cfg.job("zzz")

    def test_json_config(self):
        p = self.tmp / "c.json"
        p.write_text(json.dumps({"night": {"window": "20:00-04:00"},
                                 "job": [{"name": "a", "prompt": "x", "schedule": "daily 21:00"}]}))
        cfg = config.load(p)
        self.assertEqual(cfg.window.start, dt.time(20, 0))
        self.assertEqual(cfg.job("a").schedule.kind, "daily")

    def test_invalid_toml(self):
        p = self.tmp / "bad.toml"
        p.write_text("this is = = not toml")
        with self.assertRaises(ConfigError):
            config.load(p)


if __name__ == "__main__":
    unittest.main()
