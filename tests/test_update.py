import unittest

from redh import lifecycle
from redh.checks import Survey, check_host
from redh.install import install
from redh.update import update, update_app

from fakes import Lab, nextcloud

NEXTCLOUD = "docker.io/library/nextcloud:30"
MARIADB = "docker.io/library/mariadb:11"


def yattee(lab):
    """A locally built app, like yattee-server: a .build unit and a container using it."""
    lab.write("yattee-server/quadlet/yattee.build", """
        [Build]
        ImageTag=localhost/yattee-server:latest
        SetWorkingDirectory=unit
        """)
    lab.write("yattee-server/quadlet/yattee.container", """
        [Container]
        Image=yattee.build
        AutoUpdate=local
        PublishPort=127.0.0.1:8085:8085

        [Install]
        WantedBy=default.target
        """)


class TaskTest(unittest.TestCase):
    """.build and .volume units end 'inactive (dead)' when they worked."""

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        yattee(self.lab)
        install(self.lab.ctx, ["yattee-server"])
        self.lab.system.start_all()
        self.lab.output()

    def test_restart_reports_a_finished_build_as_ok(self):
        self.assertTrue(lifecycle.restart_app(self.lab.fresh(), "yattee-server"))
        out = self.lab.output()
        self.assertIn("ok    yattee-build.service  finished", out)
        self.assertNotIn("FAIL", out)
        self.assertIn("→ 2 ok, 0 failed", out.split("start yattee-server")[1])

    def test_start_of_a_running_app_does_not_build_again(self):
        builds = self.lab.system.builds
        self.assertTrue(lifecycle.start_app(self.lab.fresh(), "yattee-server"))
        self.assertEqual(self.lab.system.builds, builds)
        self.assertNotIn("starting", self.lab.output())

    def test_status_and_list(self):
        lifecycle.status_app(self.lab.fresh(), "yattee-server")
        self.assertIn("ok    yattee-build.service  finished, since", self.lab.output())
        lifecycle.list_apps(self.lab.fresh())
        row = [line.split() for line in self.lab.output().splitlines() if line.startswith("yattee")][0]
        self.assertEqual(row[3], "1/1")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        self.lab.write("nextcloud/quadlet/nextcloud-db.container", f"""
            [Container]
            Image={MARIADB}
            Network=nextcloud.network
            """)
        install(self.lab.ctx, ["nextcloud"])
        self.lab.system.start_all()
        self.lab.output()

    def calls(self, verb):
        return [c[4:] for c in self.lab.system.calls if c[:3] == ["systemctl", "--user", verb]]

    def test_only_the_unit_with_a_new_image_restarts(self):
        self.lab.system.registry[NEXTCLOUD] = "sha256:aaaaaaaaaaaa9999"
        self.assertTrue(update_app(self.lab.fresh(), "nextcloud"))
        out = self.lab.output()
        self.assertRegex(out, rf"ok    {NEXTCLOUD}\s+new image \w{{12}} → aaaaaaaaaaaa")
        self.assertRegex(out, rf"ok    {MARIADB}\s+up to date")
        self.assertEqual(self.calls("restart"), [["nextcloud.service"]])
        self.assertIn("nextcloud.service            active (running) — restarted", out)
        self.assertIn("nextcloud-db.service         active (running) — already running", out)

    def test_nothing_new_restarts_nothing(self):
        self.assertTrue(update_app(self.lab.fresh(), "nextcloud"))
        self.assertIn("every container already runs its newest image", self.lab.output())
        self.assertEqual(self.calls("restart"), [])

    def test_a_failed_pull_is_a_failure(self):
        self.lab.system.pull_errors[MARIADB] = "unauthorized: authentication required"
        self.assertFalse(update_app(self.lab.fresh(), "nextcloud"))
        self.assertRegex(self.lab.output(), rf"FAIL  {MARIADB}\s+pull failed: Error: initializing "
                                            rf"source docker://{MARIADB}: unauthorized")

    def test_a_failed_restart_says_how_to_go_back(self):
        self.lab.system.registry[NEXTCLOUD] = "sha256:bbbbbbbbbbbb0000"
        old = self.lab.system.images[NEXTCLOUD]
        self.lab.system.fail_start.add("nextcloud.service")
        self.assertFalse(update_app(self.lab.fresh(), "nextcloud"))
        out = self.lab.output()
        self.assertIn("to go back to the images it ran before:", out)
        self.assertIn(f"runuser -u podman -- podman tag {old.split(':')[-1][:12]} {NEXTCLOUD}", out)
        self.assertIn("redh restart nextcloud", out)

    def test_a_built_image_is_rebuilt_and_its_container_restarted(self):
        yattee(self.lab)
        install(self.lab.fresh(), ["yattee-server"])
        self.lab.system.start_all()
        self.lab.output()
        self.assertTrue(update_app(self.lab.fresh(), "yattee-server"))
        out = self.lab.output()
        self.assertRegex(out, r"ok    yattee-build.service  built: new image \d{12} → \d{12}")
        self.assertIn("yattee.service        active (running) — restarted", out)
        self.assertNotIn("FAIL", out)

    def test_dry_run_shows_what_auto_update_sees(self):
        self.lab.system.registry[NEXTCLOUD] = "sha256:cccccccccccc1111"
        self.assertEqual(update(self.lab.fresh(), ["nextcloud"], dry_run=True), 0)
        out = self.lab.output()
        self.assertIn(f"nextcloud.service     update available: {NEXTCLOUD} (registry)", out)
        self.assertIn("nextcloud-db.service  podman auto-update leaves it alone", out)
        self.assertIn("dry run: nothing pulled, built or restarted", out)
        self.assertEqual(self.calls("restart"), [])
        self.assertNotIn(["podman", "pull", "--quiet", NEXTCLOUD], self.lab.system.calls)

    def test_a_failed_automatic_run_shows_up(self):
        self.lab.system.units["podman-auto-update.service"] = {
            **self.lab.system.blank(), "ActiveState": "failed", "Result": "exit-code",
            "ExecMainStatus": "125", "ExecMainStartTimestamp": "Mon 2026-10-05 00:00:03 CEST"}
        ctx = self.lab.fresh()
        findings = check_host(ctx, Survey(ctx))
        failure = [f for f in findings if f.topic == "update" and f.level == "FAIL"][0]
        self.assertEqual(failure.text, "last automatic update 2026-10-05 00:00 failed "
                                       "(exit-code, exit status 125)")
        self.assertIn("Error: something went wrong", failure.details[0])
        self.assertEqual(failure.details[-1], "redh update APP  does the same by hand")
        update(self.lab.fresh(), ["nextcloud"])
        self.assertTrue(self.lab.output().startswith("FAIL  last automatic update 2026-10-05 00:00"))


if __name__ == "__main__":
    unittest.main()
