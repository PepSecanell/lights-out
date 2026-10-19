import datetime as dt
import json
import subprocess
import unittest

from helpers import IsolatedTestCase, T, git, have_git, make_repo
from lightsout import report, shift, state


def events(night="2026-10-18"):
    return report.load(dt.date.fromisoformat(night))


class ShiftBase(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.proj = self.make_proj()
        self.out = []

    def job(self, name, **kw):
        d = {"name": name, "prompt": name, "cwd": str(self.proj), "schedule": "daily 23:00"}
        d.update(kw)
        return d

    def tick(self, cfg, when):
        self.at(when)
        return shift.tick(cfg, log=self.out.append)

    def outcomes(self, night="2026-10-18"):
        return [(e["job"], e["outcome"]) for e in events(night)]


class DueLogicTests(ShiftBase):
    def test_daily_due_after_time_not_before(self):
        cfg = self.write_config([self.job("a")])
        j = cfg.job("a")
        state.update_job_state("a", last_done="2026-10-17T23:10:00")
        self.assertIsNone(shift.due_at(j, T("2026-10-18T22:30")))
        self.assertEqual(shift.due_at(j, T("2026-10-18T23:05")), T("2026-10-18T23:00"))

    def test_catch_up_window(self):
        cfg = self.write_config([self.job("a", catch_up_hours=2.0)])
        j = cfg.job("a")
        self.assertIsNotNone(shift.due_at(j, T("2026-10-19T00:30")))
        self.assertIsNone(shift.due_at(j, T("2026-10-19T01:30")))

    def test_not_due_twice(self):
        cfg = self.write_config([self.job("a")])
        state.update_job_state("a", last_done="2026-10-18T23:10:00")
        self.assertIsNone(shift.due_at(cfg.job("a"), T("2026-10-18T23:30")))
        self.assertIsNotNone(shift.due_at(cfg.job("a"), T("2026-10-19T23:30")))

    def test_weekly(self):
        cfg = self.write_config([self.job("a", schedule="weekly mon 01:00")])
        self.assertIsNotNone(shift.due_at(cfg.job("a"), T("2026-10-19T01:30")))     # Monday
        self.assertIsNone(shift.due_at(cfg.job("a"), T("2026-10-20T01:30")))        # Tuesday: catch-up over

    def test_every(self):
        cfg = self.write_config([self.job("a", schedule="every 2h")])
        j = cfg.job("a")
        self.assertIsNotNone(shift.due_at(j, T("2026-10-18T23:00")))                # never ran
        state.update_job_state("a", last_done="2026-10-18T22:00:00")
        self.assertIsNone(shift.due_at(j, T("2026-10-18T23:59")))
        self.assertIsNotNone(shift.due_at(j, T("2026-10-19T00:00")))

    def test_manual_disabled_parked_never_due(self):
        cfg = self.write_config([self.job("m", schedule="manual"), self.job("d", enabled=False),
                                 self.job("p")])
        state.update_job_state("p", parked=True)
        self.assertEqual(shift.due_jobs(cfg, T("2026-10-18T23:30")), [])

    def test_priority_order(self):
        cfg = self.write_config([self.job("late", priority=9), self.job("first", priority=1),
                                 self.job("mid", priority=5)])
        self.assertEqual([j.name for j in shift.due_jobs(cfg, T("2026-10-18T23:30"))], ["first", "mid", "late"])


class TickBasicsTests(ShiftBase):
    def test_outside_window_does_nothing(self):
        cfg = self.write_config([self.job("a", schedule="every 1h")])
        self.tick(cfg, "2026-10-18T12:00")
        self.assertEqual(self.call_count(), 0)

    def test_job_runs_once_and_state_recorded(self):
        cfg = self.write_config([self.job("a")])
        self.set_script("ok:Fixed 3 tests")
        self.tick(cfg, "2026-10-18T23:05")
        self.tick(cfg, "2026-10-18T23:15")
        self.assertEqual(self.call_count(), 1)
        st = state.job_state("a")
        self.assertEqual((st["last_result"], st["fails"]), ("ok", 0))
        ev = events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["detail"], "Fixed 3 tests")
        self.assertIsNone(state.holder("slot"))            # slot released
        self.assertIsNone(state.holder("tick"))

    def test_morning_after_midnight_belongs_to_evening_night(self):
        cfg = self.write_config([self.job("a")])
        self.tick(cfg, "2026-10-19T01:10")                 # missed at 23:00, caught up
        self.assertEqual(self.outcomes("2026-10-18"), [("a", "ok")])

    def test_window_edges(self):
        cfg = self.write_config([self.job("a", schedule="every 1h")])
        self.tick(cfg, "2026-10-19T06:00")                 # window just closed
        self.assertEqual(self.call_count(), 0)
        self.tick(cfg, "2026-10-19T05:59")
        self.assertEqual(self.call_count(), 1)

    def test_tick_lock_held_leaves(self):
        cfg = self.write_config([self.job("a")])
        with state.Lock("tick", "tick"):
            self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.call_count(), 0)

    def test_slot_busy_leaves_job_for_later(self):
        cfg = self.write_config([self.job("a")])
        with state.Lock("slot", "hand-run"):
            self.tick(cfg, "2026-10-18T23:05")
            self.assertEqual(self.call_count(), 0)
        self.tick(cfg, "2026-10-18T23:15")
        self.assertEqual(self.call_count(), 1)


class FullNightTests(ShiftBase):
    def test_transient_then_ok_then_limit_pauses_all_until_reset_then_resumes(self):
        cfg = self.write_config([self.job("tests", priority=1), self.job("docs", priority=7),
                                 self.job("lint", priority=8)])
        self.set_script("transient", "ok:Fixed 3 failing tests", "limit:1am")
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.call_count(), 3)             # transient + resume, then docs hit the limit
        self.assertEqual(self.outcomes(), [("tests", "ok"), ("docs", "usage_limit")])
        self.assertEqual(events()[0]["retries"], 1)
        lim = state.limit_until()
        self.assertEqual((lim.day, lim.hour), (19, 1))
        # still limited: nothing starts, and the limit is not a failure
        self.tick(cfg, "2026-10-18T23:20")
        self.tick(cfg, "2026-10-19T00:30")
        self.assertEqual(self.call_count(), 3)
        self.assertTrue(any("usage limit" in x for x in self.out))
        self.assertEqual(state.job_state("docs").get("fails", 0), 0)
        # after the reset the waiting jobs run, in priority order
        self.tick(cfg, "2026-10-19T01:10")
        self.assertEqual(self.call_count(), 5)
        self.assertEqual(self.outcomes()[-2:], [("docs", "ok"), ("lint", "ok")])
        self.assertIsNone(state.limit_until())

    def test_morning_report_after_night(self):
        cfg = self.write_config([self.job("tests"), self.job("docs", priority=7)])
        self.set_script("ok:fine", "fail:red")
        self.tick(cfg, "2026-10-18T23:05")
        self.tick(cfg, "2026-10-19T07:40")
        p = report.report_path(dt.date(2026, 10, 19))
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("tests", text)
        self.assertIn("docs", text)
        self.tick(cfg, "2026-10-19T07:50")                 # written once only
        self.assertEqual(sum("morning report written" in x for x in self.out), 1)

    def test_auth_pause_then_retry_after_an_hour(self):
        cfg = self.write_config([self.job("a", schedule="every 1h")])
        self.set_script("auth", "ok:back")
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.outcomes(), [("a", "auth_expired")])
        self.assertTrue((self.home / "state" / "auth.json").exists())
        self.assertEqual(state.job_state("a").get("fails", 0), 0)
        self.tick(cfg, "2026-10-18T23:40")                 # still paused
        self.assertEqual(self.call_count(), 1)
        self.assertTrue(any("login expired" in x for x in self.out))
        self.tick(cfg, "2026-10-19T00:10")                 # pause over
        self.assertEqual(self.call_count(), 2)
        self.assertEqual(self.outcomes()[-1], ("a", "ok"))

    def test_budget_cap_stops_later_jobs(self):
        cfg = self.write_config([self.job("a", priority=1), self.job("b", priority=2), self.job("c", priority=3)],
                                night="nightly_budget_usd = 0.5")
        self.tick(cfg, "2026-10-18T23:05")                 # each fake run costs 0.42
        self.assertEqual(self.call_count(), 2)
        self.assertEqual(self.outcomes(), [("a", "ok"), ("b", "ok")])
        self.assertTrue(any("budget" in x for x in self.out))
        self.tick(cfg, "2026-10-18T23:30")
        self.assertEqual(self.call_count(), 2)
        self.assertEqual(shift.spent_tonight(dt.date(2026, 10, 18)), 0.84)

    def test_parking_after_failures_and_reset(self):
        cfg = self.write_config([self.job("a", give_up_after=2)])
        self.set_script("fail:one", "fail:two", "ok:fixed")
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(state.job_state("a")["fails"], 1)
        self.tick(cfg, "2026-10-18T23:30")                 # failed jobs are not retried the same night
        self.assertEqual(self.call_count(), 1)
        self.tick(cfg, "2026-10-19T23:05")
        st = state.job_state("a")
        self.assertTrue(st["parked"])
        self.assertEqual(self.outcomes("2026-10-19")[-1], ("a", "parked"))
        self.tick(cfg, "2026-10-20T23:05")
        self.assertEqual(self.call_count(), 2)             # parked: not run
        state.update_job_state("a", parked=False, fails=0)  # what `lightsout reset a` does
        self.tick(cfg, "2026-10-21T23:05")
        self.assertEqual(self.call_count(), 3)
        self.assertEqual(state.job_state("a")["last_result"], "ok")

    def test_success_resets_fail_counter(self):
        cfg = self.write_config([self.job("a", give_up_after=3)])
        self.set_script("fail:x", "ok:y")
        self.tick(cfg, "2026-10-18T23:05")
        self.tick(cfg, "2026-10-19T23:05")
        self.assertEqual(state.job_state("a")["fails"], 0)

    def test_transient_exhausted_is_not_a_failure(self):
        cfg = self.write_config([self.job("a", retries=1, give_up_after=1)])
        self.set_script("transient", "transient")
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.outcomes(), [("a", "transient")])
        self.assertFalse(state.job_state("a").get("parked"))
        self.tick(cfg, "2026-10-18T23:20")                 # due again at the next tick
        self.assertEqual(self.outcomes()[-1], ("a", "ok"))


class GuardTests(ShiftBase):
    def test_busy_command_skips_and_records(self):
        cfg = self.write_config([self.job("a"), self.job("b", priority=9)], guards={"busy_command": "echo recording; true"})
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.outcomes(), [("a", "skipped"), ("b", "skipped")])
        self.assertIn("busy_command", events()[0]["detail"])
        self.assertIn("recording", events()[0]["detail"])
        # no longer busy: still due, runs
        cfg2 = self.write_config([self.job("a"), self.job("b", priority=9)], name="cfg2.toml")
        self.tick(cfg2, "2026-10-18T23:20")
        self.assertEqual(self.call_count(), 2)
        self.assertEqual(self.outcomes()[-2:], [("a", "ok"), ("b", "ok")])

    def test_busy_command_nonzero_means_free(self):
        cfg = self.write_config([self.job("a")], guards={"busy_command": "false"})
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.outcomes(), [("a", "ok")])

    def test_low_disk_guard(self):
        cfg = self.write_config([self.job("a")], guards={"min_free_disk_gb": 100000000})
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.outcomes(), [("a", "skipped")])
        self.assertIn("GB free", events()[0]["detail"])

    def test_folder_lock_held_by_hand_skips_job(self):
        cfg = self.write_config([self.job("a")])
        state.Lock(shift.dir_lock_name(self.proj), "me-by-hand", minutes=60).acquire()
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.call_count(), 0)
        ev = events()
        self.assertEqual((ev[0]["job"], ev[0]["outcome"]), ("a", "skipped"))
        self.assertIn("me-by-hand", ev[0]["detail"])
        self.assertIsNone(state.holder("slot"))
        self.assertIsNone(state.job_state("a").get("last_done"))
        # hand lock expires -> job runs
        self.tick(cfg, "2026-10-19T00:30")
        self.assertEqual(self.outcomes()[-1], ("a", "ok"))

    def test_jobs_in_different_folders_unaffected_by_a_lock(self):
        other = self.make_proj("other")
        cfg = self.write_config([self.job("a"), self.job("b", cwd=str(other), priority=9)])
        state.Lock(shift.dir_lock_name(self.proj), "hand", minutes=60).acquire()
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.outcomes(), [("a", "skipped"), ("b", "ok")])

    def test_folder_lock_released_after_job(self):
        cfg = self.write_config([self.job("a")])
        self.tick(cfg, "2026-10-18T23:05")
        self.assertIsNone(state.holder(shift.dir_lock_name(self.proj)))


@unittest.skipUnless(have_git(), "git not available")
class WorktreeShiftTests(ShiftBase):
    def test_worktree_job_commits_on_own_branch_main_untouched(self):
        repo = make_repo(self.tmp / "repo")
        head = git(repo, "rev-parse", "HEAD")
        branch0 = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        (repo / "wip.txt").write_text("my uncommitted work\n")
        cfg = self.write_config([self.job("w", cwd=str(repo), worktree=True)])
        self.set_script("edit:new.txt:agent was here")
        self.tick(cfg, "2026-10-18T23:05")
        ev = events()[0]
        self.assertEqual(ev["outcome"], "ok")
        br = ev["branch"]
        self.assertTrue(br.startswith("lightsout/w/"))
        self.assertIn("1 commit", ev["notes"])
        # the branch has the agent's file
        self.assertEqual(git(repo, "show", f"{br}:new.txt"), "agent was here")
        self.assertEqual(git(repo, "rev-list", "--count", f"{head}..{br}"), "1")
        # the main checkout is exactly as before
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), branch0)
        self.assertFalse((repo / "new.txt").exists())
        self.assertEqual((repo / "wip.txt").read_text(), "my uncommitted work\n")
        self.assertEqual(git(repo, "status", "--porcelain"), "?? wip.txt")
        # the agent ran in the worktree, not in the repo
        self.assertIn("worktrees", self.calls_argv()[0]["cwd"])

    def test_worktree_without_changes_has_zero_commits(self):
        repo = make_repo(self.tmp / "repo")
        cfg = self.write_config([self.job("w", cwd=str(repo), worktree=True)])
        self.set_script("ok:nothing to do")
        self.tick(cfg, "2026-10-18T23:05")
        self.assertIn("0 commit", events()[0]["notes"])

    def test_worktree_in_non_repo_is_a_failure(self):
        cfg = self.write_config([self.job("w", worktree=True)])
        self.tick(cfg, "2026-10-18T23:05")
        self.assertEqual(self.call_count(), 0)
        self.assertEqual(self.outcomes(), [("w", "failure")])
        self.assertIn("worktree", events()[0]["detail"])


if __name__ == "__main__":
    unittest.main()
