import datetime as dt
import os
import unittest

from helpers import IsolatedTestCase
from lightsout import report

NIGHT = dt.date(2026, 10, 18)


class ReportTests(IsolatedTestCase):
    def rec(self, at, job, outcome, detail="", **kw):
        self.at(at)
        return report.record(NIGHT, job, outcome, detail, **kw)

    def test_record_and_load_skip_corrupt_lines(self):
        self.rec("2026-10-18T23:05", "a", "ok", "x" * 1000)
        with open(report.events_file(NIGHT), "a") as f:
            f.write("{broken\n")
        self.rec("2026-10-18T23:06", "b", "failure", "bad")
        ev = report.load(NIGHT)
        self.assertEqual([e["job"] for e in ev], ["a", "b"])
        self.assertEqual(len(ev[0]["detail"]), 400)
        self.assertEqual(report.load(dt.date(2000, 1, 1)), [])

    def test_empty_night(self):
        md, summary = report.build(NIGHT, "22:00-06:00")
        self.assertIn("Nothing ran", md)
        self.assertEqual(summary, "Last night: nothing ran")

    def test_sections_and_summary(self):
        self.rec("2026-10-18T23:05", "good", "ok", "fixed it", minutes=3, cost_usd=0.42, log="/l/good.log")
        self.rec("2026-10-18T23:10", "bad", "failure", "tests red", log="/l/bad.log")
        self.rec("2026-10-18T23:15", "stuck", "parked", "failed 3 times")
        md, summary = report.build(NIGHT, "22:00-06:00")
        for name in ("good", "bad", "stuck", "fixed it", "tests red"):
            self.assertIn(name, md)
        self.assertLess(md.index("good"), md.index("bad"))          # ok section first
        self.assertIn("/l/bad.log", md)                             # failures point at their log
        self.assertEqual(summary, "Last night: 1 done, 1 failed, 1 parked")
        self.assertIn("Mon 19 Oct 2026", md)                        # morning date

    def test_skipped_then_ok_ends_ok(self):
        self.rec("2026-10-18T23:05", "a", "skipped", "busy")
        self.rec("2026-10-18T23:35", "a", "ok", "done")
        self.rec("2026-10-18T23:50", "a", "skipped", "busy again")
        _, summary = report.build(NIGHT)
        self.assertEqual(summary, "Last night: 1 done")

    def test_last_event_wins_otherwise(self):
        self.rec("2026-10-18T23:05", "a", "usage_limit", "limit", resume_after="2026-10-19T01:03:00+00:00")
        self.rec("2026-10-19T01:10", "a", "ok", "done")
        _, summary = report.build(NIGHT)
        self.assertEqual(summary, "Last night: 1 done")

    def test_usage_limit_banner_says_not_failed(self):
        self.rec("2026-10-18T23:05", "a", "usage_limit", "limit", resume_after="2026-10-19T01:03:00")
        md, _ = report.build(NIGHT)
        self.assertIn("Usage limit reached", md)
        self.assertIn("01:03", md)
        self.assertIn("Nothing was marked failed", md)

    def test_retries_and_waits_and_branch_shown(self):
        self.rec("2026-10-18T23:05", "a", "skipped", "busy")
        self.rec("2026-10-18T23:30", "a", "ok", "done", retries=2, branch="lightsout/a/x", cost_usd=1.5)
        md, _ = report.build(NIGHT)
        self.assertIn("2 retries", md)
        self.assertIn("waited 1x", md)
        self.assertIn("lightsout/a/x", md)
        self.assertIn("$1.50", md)

    def test_fmt_min(self):
        self.assertEqual(report.fmt_min(0.3), "under a minute")
        self.assertEqual(report.fmt_min(12), "12 min")
        self.assertEqual(report.fmt_min(120), "2.0 h")

    def test_due_rules(self):
        cfg = self.write_config([{"name": "a", "prompt": "x"}])
        self.assertIsNone(report.due(cfg, self._t("2026-10-19T07:00")))        # before report_at
        self.assertIsNone(report.due(cfg, self._t("2026-10-19T05:00")))        # still in window
        self.assertEqual(report.due(cfg, self._t("2026-10-19T07:40")), NIGHT)
        report.make(cfg, NIGHT)
        self.assertIsNone(report.due(cfg, self._t("2026-10-19T07:45")))        # already written

    def _t(self, s):
        return dt.datetime.fromisoformat(s).astimezone()

    def test_make_writes_file_and_runs_report_command(self):
        marker = self.tmp / "marker"
        cfg = self.write_config([{"name": "a", "prompt": "x"}],
                                night=f'report_command = "cp \\"$1\\" {marker}"')
        self.rec("2026-10-18T23:05", "a", "ok", "yay")
        p, md, summary = report.make(cfg, NIGHT)
        self.assertEqual(p, report.report_path(dt.date(2026, 10, 19)))
        self.assertEqual(p.read_text(), md)
        self.assertEqual(marker.read_text(), md)

    def test_notify_respects_env(self):
        from unittest import mock
        with mock.patch("lightsout.report.subprocess.run") as run:
            report.notify("t", "m")                       # LIGHTSOUT_NO_NOTIFY=1 from setUp
            run.assert_not_called()
            os.environ["LIGHTSOUT_NO_NOTIFY"] = "0"
            report.notify("t", 'say "hi"')
            self.assertLessEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
