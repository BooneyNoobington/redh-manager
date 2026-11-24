import json
import unittest

from redh import lifecycle
from redh.host import Result
from redh.install import install

from fakes import Lab, nextcloud


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        install(self.lab.ctx, ["nextcloud"])
        self.lab.output()
        self.ctx = self.lab.fresh()

    def test_start_reports_every_unit(self):
        self.assertTrue(lifecycle.start_app(self.ctx, "nextcloud"))
        out = self.lab.output()
        self.assertIn("ok    nextcloud.service", out)
        self.assertIn("active (running) — healthy", out)
        self.assertIn("nextcloud-network.service    active (exited)", out)
        self.assertIn("→ 3 ok, 0 failed", out)
        start = [c for c in self.lab.system.calls if c[2:3] == ["start"]]
        self.assertEqual(len(start), 1, "one systemctl call for the whole app")

    def test_failure_shows_where_and_why(self):
        self.lab.system.fail_start.add("nextcloud.service")
        self.assertFalse(lifecycle.start_app(self.ctx, "nextcloud"))
        out = self.lab.output()
        self.assertIn("FAIL  nextcloud.service", out)
        self.assertIn("nextcloud.service            failed, result exit-code, exit status 125", out)
        self.assertIn("Error: something went wrong", out)        # the journal tail
        self.assertIn("→ 2 ok, 1 failed", out)

    def test_crash_loop_is_a_failure(self):
        self.lab.system.crash_loop.add("nextcloud.service")
        self.assertFalse(lifecycle.start_app(self.ctx, "nextcloud"))
        self.assertIn("crash loop", self.lab.output())

    def test_already_running_and_stop(self):
        lifecycle.start_app(self.ctx, "nextcloud")
        lifecycle.start_app(self.ctx, "nextcloud")
        self.assertIn("already running", self.lab.output())
        self.assertTrue(lifecycle.stop_app(self.ctx, "nextcloud"))
        out = self.lab.output()
        self.assertIn("inactive (dead)", out)
        self.assertIn("→ 3 ok, 0 failed", out)

    def test_stuck_unit_fails_stop(self):
        lifecycle.start_app(self.ctx, "nextcloud")
        self.lab.system.stuck.add("nextcloud.service")
        self.assertFalse(lifecycle.stop_app(self.ctx, "nextcloud"))
        self.assertIn("still running", self.lab.output())

    def test_stop_names_units_of_other_apps_it_takes_down(self):
        self.lab.write("wiki/quadlet/wiki.container", """
            [Container]
            Image=docker.io/library/wiki:2
            Network=nextcloud.network
            """)
        install(self.lab.fresh(), ["wiki"])
        self.lab.system.start_all()
        self.lab.output()
        lifecycle.stop_app(self.lab.fresh(), "nextcloud")
        out = self.lab.output()
        self.assertIn("systemd stops wiki.service as well: it requires nextcloud-network.service", out)

    def test_status_and_list(self):
        lifecycle.start_app(self.ctx, "nextcloud")
        self.lab.output()
        self.assertTrue(lifecycle.status_app(self.ctx, "nextcloud"))
        out = self.lab.output()
        self.assertIn("active (running), since 2026-10-05 13:10, healthy", out)
        lifecycle.list_apps(self.ctx)
        listing = self.lab.output().splitlines()
        self.assertEqual(listing[0].split(),
                         ["APP", "LAYOUT", "UNITS", "ACTIVE", "RUNNING", "INSTALLED", "CADDY"])
        self.assertEqual(listing[1].split(), ["nextcloud", "quadlet/", "3", "2/2", "1", "in", "sync", "1"])
        self.assertEqual(listing[-1], "1 container running.")

    def test_list_as_json(self):
        lifecycle.start_app(self.ctx, "nextcloud")
        self.lab.system.run_container("scratch", image="docker.io/library/busybox:1")
        self.lab.output()
        self.assertEqual(lifecycle.list_apps(self.ctx, as_json=True), 0)
        listing = json.loads(self.lab.output())
        self.assertEqual(listing, {
            "root": self.lab.settings.root,
            "apps": [{"name": "nextcloud", "layout": "quadlet/", "units": 3, "services": 2, "active": 2,
                      "running": 1, "installed": "in sync", "caddy": 1}],
            "containers": {"running": 2, "in_no_app": [
                {"name": "scratch", "image": "docker.io/library/busybox:1", "unit": None}]},
            "warnings": [],
        })

    def test_list_without_podman_says_what_it_cannot_tell(self):
        podman = self.lab.system.podman

        def broken(args):
            return Result(args, 125, "", "Error: cannot connect to Podman") if args[0] == "ps" else podman(args)

        self.lab.system.podman = broken
        lifecycle.start_app(self.ctx, "nextcloud")
        self.lab.output()
        lifecycle.list_apps(self.ctx, as_json=True)
        listing = json.loads(self.lab.output())
        self.assertEqual((listing["apps"][0]["running"], listing["containers"]), (None, None))
        self.assertEqual(listing["warnings"],
                         ["cannot tell which containers run: podman ps failed: Error: cannot connect to Podman"])
        lifecycle.list_apps(self.ctx)
        table = self.lab.output().splitlines()
        self.assertIn("cannot tell which containers run", table[0])
        self.assertEqual(table[2].split(), ["nextcloud", "quadlet/", "3", "2/2", "?", "in", "sync", "1"])


if __name__ == "__main__":
    unittest.main()
