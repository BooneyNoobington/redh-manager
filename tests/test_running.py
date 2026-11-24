import argparse
import os
import unittest

from redh import lifecycle
from redh.cli import dispatch
from redh.install import install
from redh.prune import prune
from redh.running import running

from fakes import Lab, nextcloud


class RunningTest(unittest.TestCase):
    """Containers redh did not start: hand-installed Quadlet files, 'podman run'."""

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        home = self.lab.settings.home
        for app in ("vaultwarden", "redhdb", "yattee-server"):
            os.makedirs(os.path.join(home, app, "data"))
        flat = self.lab.write(os.path.join(self.lab.settings.quadlet_dir, "vw.container"),
                              "[Container]\nImage=docker.io/vaultwarden/server:1\n")
        sys = self.lab.system
        sys.units["vw.service"] = {**sys.blank(), "ActiveState": "active", "SourcePath": flat}
        sys.run_container("vaultwarden", unit="vw.service", image="docker.io/vaultwarden/server:1",
                          sources=[os.path.join(home, "vaultwarden", "data")])
        sys.run_container("systemd-redhdb", image="docker.io/library/mariadb:11")
        sys.run_container("portainer_agent", image="docker.io/portainer/agent:2")

    def test_containers_are_matched_to_apps(self):
        found = {c.name: (c.app, c.why) for c in running(self.lab.ctx)}
        self.assertEqual(found, {"vaultwarden": ("vaultwarden", "mount"),
                                 "systemd-redhdb": ("redhdb", "name"),
                                 "portainer_agent": (None, "")})

    def test_list_shows_what_runs_and_what_belongs_nowhere(self):
        lifecycle.list_apps(self.lab.ctx)
        lines = self.lab.output().splitlines()
        rows = {line.split()[0]: line.split() for line in lines[1:4]}
        self.assertEqual(rows["redhdb"][4], "1")
        self.assertEqual(rows["vaultwarden"][4], "1")
        self.assertEqual(rows["yattee-server"][4], "-")
        self.assertIn("3 containers running, 1 of them in no app:", lines)
        self.assertIn("  portainer_agent (docker.io/portainer/agent:2), no systemd unit", lines)

    def test_stop_all_fails_while_anything_runs(self):
        args = argparse.Namespace(command="stop", apps=[], all=True)
        self.assertEqual(dispatch(self.lab.ctx, args), 1)
        out = self.lab.output()
        self.assertIn("FAIL  vaultwarden (docker.io/vaultwarden/server:1), unit vw.service [vaultwarden]", out)
        self.assertIn("systemctl --user -M podman@ stop vw.service", out)
        self.assertIn("runuser -u podman -- podman stop portainer_agent", out)
        self.assertIn("→ 3 containers still running", out)
        self.lab.system.containers.clear()
        self.assertEqual(dispatch(self.lab.fresh(), args), 0)
        self.assertIn("ok    no container of podman is running", self.lab.output())

    def test_status_of_an_app_redh_did_not_install(self):
        lifecycle.status_app(self.lab.ctx, "vaultwarden", running(self.lab.ctx))
        out = self.lab.output()
        self.assertIn("not installed by redh", out)
        self.assertIn("running: vaultwarden (docker.io/vaultwarden/server:1), unit vw.service", out)
        self.assertIn("from " + os.path.join(self.lab.settings.quadlet_dir, "vw.container"), out)

    def test_prune_does_not_delete_under_a_running_container(self):
        self.assertEqual(prune(self.lab.ctx, "vaultwarden", yes=True), 1)
        out = self.lab.output()
        self.assertIn("vaultwarden  still running — started by vw.service, which redh did not install", out)
        self.assertIn("nothing deleted", out)
        self.assertTrue(os.path.isdir(os.path.join(self.lab.settings.home, "vaultwarden")))

    def test_stop_names_containers_its_units_do_not_cover(self):
        nextcloud(self.lab)
        install(self.lab.fresh(), ["nextcloud"])
        self.lab.system.start_all()
        self.lab.system.run_container(
            "nextcloud-cron", sources=[os.path.join(self.lab.settings.home, "nextcloud", "data")])
        self.lab.output()
        self.assertFalse(lifecycle.stop_app(self.lab.fresh(), "nextcloud"))
        out = self.lab.output()
        self.assertIn("ok    nextcloud.service", out)
        self.assertIn("FAIL  nextcloud-cron", out)
        self.assertIn("still running — started without a systemd unit", out)
        self.assertNotIn("systemd-nextcloud ", out)     # the unit's own container did stop


if __name__ == "__main__":
    unittest.main()
