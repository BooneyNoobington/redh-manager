import os
import stat
import unittest

from redh.install import install, uninstall
from redh.plan import make_plan

from fakes import Lab, nextcloud


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        self.qdir = self.lab.settings.quadlet_dir

    def test_fresh_install_copies_reloads_and_verifies(self):
        rc = install(self.lab.ctx, ["nextcloud"])
        out = self.lab.output()
        self.assertEqual(rc, 0, out)
        installed = os.path.join(self.qdir, "nextcloud", "nextcloud.container")
        self.assertTrue(os.path.isfile(installed))
        self.assertIn("+ nextcloud.container", out)
        self.assertIn("Quadlet accepts all 3", out)
        self.assertIn("daemon-reload", out)
        self.assertIn("systemd knows all 3 units", out)
        self.assertIn("not running → redh start nextcloud", out)
        self.assertEqual(self.lab.system.units["nextcloud.service"]["LoadState"], "loaded")
        # Second run: nothing to do.
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0)
        self.assertIn("already up to date", self.lab.output())

    def test_modes_follow_the_source(self):
        self.lab.write("nextcloud/quadlet/secrets.env", "PW=x\n", mode=0o640)
        install(self.lab.ctx, ["nextcloud"])
        mode = stat.S_IMODE(os.stat(os.path.join(self.qdir, "nextcloud", "secrets.env")).st_mode)
        self.assertEqual(mode, 0o640)

    def test_changes_removals_and_restart_hint(self):
        install(self.lab.ctx, ["nextcloud"])
        self.lab.system.start_all()
        unit = os.path.join(self.lab.settings.home, "nextcloud/quadlet/nextcloud.container")
        with open(unit) as fh:
            text = fh.read()
        self.lab.write(unit, text.replace("nextcloud:30", "nextcloud:31")
                       .replace("Volume=nextcloud-db.volume:/db\n", ""))
        os.unlink(os.path.join(self.lab.settings.home, "nextcloud/quadlet/nextcloud-db.volume"))
        self.lab.output()
        rc = install(self.lab.fresh(), ["nextcloud"])
        out = self.lab.output()
        self.assertEqual(rc, 0, out)
        self.assertIn("~ nextcloud.container", out)
        self.assertIn("- nextcloud-db.volume", out)
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "nextcloud", "nextcloud-db.volume")))
        self.assertIn("running on the old definition: nextcloud.service → redh restart nextcloud", out)

    def test_old_flat_copies_are_replaced(self):
        flat = self.lab.write(os.path.join(self.qdir, "nextcloud.network"), "[Network]\n")
        plan = make_plan(self.lab.ctx, "nextcloud")
        self.assertEqual([(p, same) for p, _, same in plan.legacy], [(flat, True)])
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        self.assertFalse(os.path.exists(flat))

    def test_differing_flat_copy_blocks_without_force(self):
        flat = self.lab.write(os.path.join(self.qdir, "nextcloud.network"), "[Network]\nSubnet=10.9.0.0/24\n")
        self.assertEqual(install(self.lab.ctx, ["nextcloud"]), 1)
        out = self.lab.output()
        self.assertIn("differs from the source", out)
        self.assertIn("nothing installed", out)
        self.assertTrue(os.path.exists(flat))
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"], force=True), 0, self.lab.output())
        self.assertFalse(os.path.exists(flat))

    def test_conflict_with_another_app_always_blocks(self):
        self.lab.write(os.path.join(self.qdir, "gitea", "nextcloud.network"), "[Network]\n")
        self.assertEqual(install(self.lab.ctx, ["nextcloud"], force=True), 1)
        self.assertIn("installed for gitea", self.lab.output())

    def test_generator_error_blocks(self):
        self.lab.system.generator_errors["nextcloud.container"] = "unsupported key 'Imgae' in group 'Container'"
        self.assertEqual(install(self.lab.ctx, ["nextcloud"]), 1)
        out = self.lab.output()
        self.assertIn("nextcloud.container does not generate nextcloud.service", out)
        self.assertIn("unsupported key 'Imgae'", out)
        self.assertNotIn("Loading source unit file", out)
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "nextcloud")))

    def test_dry_run_writes_nothing(self):
        self.assertEqual(install(self.lab.ctx, ["nextcloud"], dry_run=True), 0)
        self.assertIn("dry run", self.lab.output())
        self.assertFalse(os.path.exists(self.qdir))
        self.assertNotIn(["systemctl", "--user", "daemon-reload"], self.lab.system.calls)

    def test_top_level_layout_installs_units_only(self):
        self.lab.write("legacy/legacy.container", "[Container]\nImage=docker.io/library/redis:7\n")
        self.lab.write("legacy/legacy.container.d/10-env.conf", "[Container]\nEnvironment=A=1\n")
        os.makedirs(os.path.join(self.lab.settings.home, "legacy", "data"))
        self.lab.write("legacy/data/dump.rdb", "binary")
        self.assertEqual(install(self.lab.fresh(), ["legacy"]), 0, self.lab.output())
        installed = sorted(os.listdir(os.path.join(self.qdir, "legacy")))
        self.assertEqual(installed, ["legacy.container", "legacy.container.d"])

    def test_uninstall_stops_and_removes(self):
        install(self.lab.ctx, ["nextcloud"])
        self.lab.system.start_all()
        self.lab.output()
        self.assertEqual(uninstall(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "nextcloud")))
        self.assertNotIn("nextcloud.service", self.lab.system.units)
        self.assertTrue(os.path.isdir(os.path.join(self.lab.settings.home, "nextcloud")))


if __name__ == "__main__":
    unittest.main()
