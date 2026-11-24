import os
import unittest

from redh.install import install
from redh.prune import prune

from fakes import Lab, nextcloud


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        install(self.lab.ctx, ["nextcloud"])
        self.lab.system.start_all()
        self.lab.system.objects |= {("network", "systemd-nextcloud"),
                                    ("volume", "systemd-nextcloud-db")}
        self.lab.output()
        self.app_dir = os.path.join(self.lab.settings.home, "nextcloud")

    def test_dry_run_changes_nothing(self):
        self.assertEqual(prune(self.lab.fresh(), "nextcloud", dry_run=True), 0)
        out = self.lab.output()
        self.assertIn("podman    volumes: systemd-nextcloud-db", out)
        self.assertIn(f"delete    {self.app_dir}/", out)
        self.assertTrue(os.path.isdir(self.app_dir))
        self.assertEqual(self.lab.system.units["nextcloud.service"]["ActiveState"], "active")

    def test_prune_removes_everything(self):
        self.assertEqual(prune(self.lab.fresh(), "nextcloud", yes=True), 0, self.lab.output())
        self.assertFalse(os.path.exists(self.app_dir))
        self.assertFalse(os.path.exists(self.lab.settings.install_dir("nextcloud")))
        self.assertEqual(self.lab.system.objects, set())
        self.assertNotIn("nextcloud.service", self.lab.system.units)

    def test_keep_volumes(self):
        prune(self.lab.fresh(), "nextcloud", yes=True, keep_volumes=True)
        self.assertEqual(self.lab.system.objects, {("volume", "systemd-nextcloud-db")})

    def test_nothing_is_deleted_while_a_unit_runs(self):
        self.lab.system.stuck.add("nextcloud.service")
        self.assertEqual(prune(self.lab.fresh(), "nextcloud", yes=True), 1)
        self.assertIn("nothing deleted", self.lab.output())
        self.assertTrue(os.path.isdir(self.app_dir))

    def test_dependents_block(self):
        self.lab.write("wiki/quadlet/wiki.container", """
            [Container]
            Image=docker.io/x/wiki:1
            Network=nextcloud.network
            """)
        install(self.lab.fresh(), ["wiki"])
        self.lab.output()
        self.assertEqual(prune(self.lab.fresh(), "nextcloud", yes=True), 1)
        self.assertIn("other apps depend on nextcloud: wiki.service (wiki)", self.lab.output())
        self.assertTrue(os.path.isdir(self.app_dir))

    def test_refuses_a_symlink(self):
        target = os.path.join(self.lab.tmp, "elsewhere")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.lab.settings.home, "linked"))
        with self.assertRaises(SystemExit):
            prune(self.lab.fresh(), "linked", yes=True)
        self.assertTrue(os.path.isdir(target))


if __name__ == "__main__":
    unittest.main()
