import os
import unittest

from redh.checks import Survey, check_app
from redh.cli import parse_args
from redh.host import Host, Result

from fakes import Lab


class RecordingHost(Host):
    """A Host that pretends to be root and records what it would run."""

    def __init__(self, settings):
        super().__init__(settings)
        self.euid = 0
        self.ran = []

    def _exec(self, argv, extra_env, timeout, cwd):
        self.ran.append((argv, dict(extra_env), cwd))
        return Result(argv, 0, "", "")


class HostTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.lab.settings.uid = 1001
        self.host = RecordingHost(self.lab.settings)

    def test_root_reaches_the_user_manager_through_runuser(self):
        self.host.systemctl("start", "--", "web.service")
        argv, env, cwd = self.host.ran[-1]
        self.assertEqual(argv[:6], ["runuser", "-u", "podman", "--", "env",
                                    f"HOME={self.lab.settings.home}"])
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1001", argv)
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus", argv)
        self.assertEqual(argv[-4:], ["--user", "start", "--", "web.service"])
        self.assertEqual(env, {})
        self.assertEqual(cwd, self.lab.settings.home)

    def test_other_users_run_without_roots_xdg_dirs(self):
        self.host.run_as("caddy", ["caddy", "validate"])
        argv, env, cwd = self.host.ran[-1]
        self.assertEqual(argv[:5], ["runuser", "-u", "caddy", "--", "env"])
        self.assertIn("XDG_DATA_HOME", argv)
        self.assertEqual(argv[-2:], ["caddy", "validate"])
        self.assertEqual(cwd, "/")
        self.host.run_as("root", ["true"])
        self.assertEqual(self.host.ran[-1][0], ["true"])

    def test_generator_gets_its_unit_dirs(self):
        self.host._generator, self.host._generator_searched = "/usr/libexec/podman/quadlet", True
        self.host.generator_dryrun(["/a", "/b"])
        argv, _, _ = self.host.ran[-1]
        self.assertIn("QUADLET_UNIT_DIRS=/a:/b", argv)
        self.assertEqual(argv[-3:], ["/usr/libexec/podman/quadlet", "-user", "-dryrun"])

    def test_root_reads_the_journal_by_uid(self):
        self.host.journal("web.service", since=1000)
        argv, _, _ = self.host.ran[-1]
        self.assertEqual(argv[0], "journalctl")
        self.assertIn("--since=@1000", argv)
        self.assertIn("_SYSTEMD_USER_UNIT=web.service", argv)
        self.assertIn("_UID=1001", argv)
        self.assertNotIn("runuser", argv)

    def test_show_matches_blocks_to_units(self):
        class ShowHost(RecordingHost):
            def _exec(self, argv, extra_env, timeout, cwd):
                return Result(argv, 0, "Id=a.service\nActiveState=active\n\n"
                                       "Id=b.service\nActiveState=failed\n", "")
        states = ShowHost(self.lab.settings).show(["a.service", "b.service"])
        self.assertEqual(states["b.service"]["ActiveState"], "failed")


class CliTest(unittest.TestCase):
    def test_global_options_work_on_both_sides(self):
        self.assertTrue(parse_args(["-v", "check"]).verbose)
        self.assertTrue(parse_args(["check", "-v", "web"]).verbose)
        self.assertFalse(parse_args(["check"]).verbose)
        args = parse_args(["install", "-n", "--restart", "web", "db"])
        self.assertEqual((args.apps, args.dry_run, args.restart), (["web", "db"], True, True))

    def test_list_takes_json(self):
        self.assertTrue(parse_args(["list", "--json"]).json)
        self.assertFalse(parse_args(["list"]).json)

    def test_a_directory_that_is_no_app_is_only_mentioned(self):
        lab = Lab()
        self.addCleanup(lab.cleanup)
        os.makedirs(os.path.join(lab.settings.home, "backups"))
        findings = check_app(lab.ctx, "backups", Survey(lab.ctx))
        self.assertEqual([(f.topic, f.level) for f in findings], [("layout", "info"), ("perms", "ok")])


if __name__ == "__main__":
    unittest.main()
