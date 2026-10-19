import os
import plistlib
import sys
import unittest
from pathlib import Path
from unittest import mock

from helpers import IsolatedTestCase
from lightsout import schedulers as S


class SchedulerTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.tmp / "my config.toml"

    def test_plist_contents(self):
        data = plistlib.loads(S.launchd_plist(self.cfg, 10, "claude"))
        self.assertEqual(data["Label"], S.LABEL)
        self.assertEqual(data["StartInterval"], 600)
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["ProgramArguments"],
                         [sys.executable, "-m", "lightsout", "--config", str(self.cfg), "tick"])
        self.assertEqual(data["EnvironmentVariables"]["LIGHTSOUT_HOME"], str(self.home))
        self.assertIn("/usr/bin", data["EnvironmentVariables"]["PATH"].split(":"))
        self.assertTrue(data["StandardOutPath"].startswith(str(self.home)))

    def test_plist_logs_outside_tcc_folders(self):
        os.environ.pop("LIGHTSOUT_HOME")
        with mock.patch("lightsout.paths.IS_MAC", True):
            data = plistlib.loads(S.launchd_plist(self.cfg, 10, "claude"))
        self.assertIn("/Library/Logs/lightsout", data["StandardOutPath"])
        for bad in ("Downloads", "Desktop", "Documents"):
            self.assertNotIn(f"/{bad}/", data["StandardOutPath"])

    def test_path_env_puts_claude_dir_first_and_dedups(self):
        d = self.tmp / "bin"
        d.mkdir()
        c = d / "claude"
        c.write_text("#!/bin/sh\n")
        c.chmod(0o755)
        parts = S._path_env(str(c)).split(":")
        self.assertEqual(parts[0], str(d.resolve()))
        self.assertEqual(len(parts), len(set(parts)))

    def test_systemd_units(self):
        u = S.systemd_units(self.cfg, 15, "claude")
        self.assertEqual(set(u), {"lightsout.service", "lightsout.timer"})
        self.assertIn("OnUnitActiveSec=15min", u["lightsout.timer"])
        self.assertIn("Persistent=true", u["lightsout.timer"])
        self.assertIn("Type=oneshot", u["lightsout.service"])
        self.assertIn(f'"{self.cfg}"', u["lightsout.service"])      # path with a space is quoted
        self.assertIn("Environment=PATH=", u["lightsout.service"])

    def test_cron_line(self):
        line = S.cron_line(self.cfg, 10, "claude")
        self.assertTrue(line.startswith("*/10 * * * * PATH="))
        self.assertTrue(line.endswith(S.CRON_TAG))
        self.assertIn(f"'{self.cfg}'", line)
        self.assertIn("tick.log", line)
        self.assertIn("2>&1", line)

    def test_dry_run_each_kind_writes_nothing(self):
        fake_home = self.tmp / "fakehome"
        with mock.patch.object(Path, "home", return_value=fake_home), \
                mock.patch("lightsout.schedulers.subprocess.run") as run:
            for kind, needle in (("launchd", "would write"), ("systemd", "lightsout.timer"),
                                 ("cron", "would add to your crontab")):
                out = S.install(self.cfg, 10, "claude", kind=kind, dry_run=True)
                self.assertIn(needle, out)
            run.assert_not_called()
        self.assertFalse(fake_home.exists())

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            S.install(self.cfg, 10, "claude", kind="windows", dry_run=True)

    def test_detect(self):
        with mock.patch.object(S.sys, "platform", "darwin"):
            self.assertEqual(S.detect(), "launchd")
        with mock.patch.object(S.sys, "platform", "linux"), \
                mock.patch("lightsout.schedulers.shutil.which", return_value=None):
            self.assertEqual(S.detect(), "cron")

    def test_install_cron_replaces_tagged_line_without_real_crontab(self):
        existing = ["0 1 * * * other", f"*/5 * * * * old {S.CRON_TAG}"]
        with mock.patch.object(S, "_crontab", return_value=existing), \
                mock.patch.object(S, "_set_crontab") as setc:
            S.install(self.cfg, 10, "claude", kind="cron")
        lines = setc.call_args[0][0]
        self.assertEqual(lines[0], "0 1 * * * other")
        self.assertEqual(len(lines), 2)
        self.assertNotIn("*/5", lines[1])

    def test_uninstall_cron_keeps_other_lines(self):
        existing = ["0 1 * * * other", f"*/5 * * * * old {S.CRON_TAG}"]
        with mock.patch.object(S, "_crontab", return_value=existing), \
                mock.patch.object(S, "_set_crontab") as setc:
            S.uninstall("cron")
        self.assertEqual(setc.call_args[0][0], ["0 1 * * * other"])

    def test_installed_cron(self):
        with mock.patch.object(S, "_crontab", return_value=[f"x {S.CRON_TAG}"]):
            self.assertTrue(S.installed("cron"))
        with mock.patch.object(S, "_crontab", return_value=["x"]):
            self.assertFalse(S.installed("cron"))


if __name__ == "__main__":
    unittest.main()
