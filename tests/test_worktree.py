import unittest

from helpers import IsolatedTestCase, git, have_git, make_repo
from lightsout import worktree


@unittest.skipUnless(have_git(), "git not available")
class WorktreeTests(IsolatedTestCase):
    def test_create_makes_branch_and_checkout(self):
        repo = make_repo(self.tmp / "repo")
        wt, branch, base = worktree.create(repo, "nightly")
        self.assertEqual(base, git(repo, "rev-parse", "HEAD"))
        self.assertEqual(branch, "lightsout/nightly/20261018-2305")
        self.assertTrue((wt / "a.txt").exists())
        self.assertEqual(git(wt, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertTrue(str(wt).startswith(str(self.home)))

    def test_subdirectory_cwd(self):
        repo = make_repo(self.tmp / "repo")
        (repo / "sub").mkdir()
        (repo / "sub" / "f.txt").write_text("x")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "sub")
        wt, _, _ = worktree.create(repo / "sub", "j")
        self.assertEqual(wt.name, "sub")
        self.assertTrue((wt / "f.txt").exists())

    def test_not_a_repo(self):
        with self.assertRaises(worktree.WorktreeError):
            worktree.create(self.make_proj("plain"), "j")

    def test_finish_commits_leftovers(self):
        repo = make_repo(self.tmp / "repo")
        wt, branch, base = worktree.create(repo, "j")
        (wt / "new.txt").write_text("hi")
        self.assertEqual(worktree.finish(wt, "j", base), 1)
        self.assertEqual(git(repo, "log", "-1", "--format=%an", branch), "lights-out")
        self.assertEqual(git(wt, "status", "--porcelain"), "")

    def test_finish_nothing_and_already_committed(self):
        repo = make_repo(self.tmp / "repo")
        wt, branch, base = worktree.create(repo, "j")
        self.assertEqual(worktree.finish(wt, "j", base), 0)
        (wt / "x.txt").write_text("1")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", "agent commit")
        (wt / "y.txt").write_text("2")
        self.assertEqual(worktree.finish(wt, "j", base), 2)

    def test_clean_removes_only_old_worktrees_and_keeps_branch(self):
        import os
        repo = make_repo(self.tmp / "repo")
        wt, branch, _ = worktree.create(repo, "j")
        self.assertEqual(worktree.clean(7), [])
        old = 1_000_000_000
        os.utime(wt, (old, old))
        removed = worktree.clean(7)
        self.assertEqual(len(removed), 1)
        self.assertFalse(wt.exists())
        self.assertIn(branch, git(repo, "branch", "--list", branch))

    def test_clean_without_dir(self):
        self.assertEqual(worktree.clean(), [])


if __name__ == "__main__":
    unittest.main()
