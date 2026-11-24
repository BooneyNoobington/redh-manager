import os
import stat
import unittest

from redh import perms, sites
from redh.checks import Survey, check_app, check_host
from redh.install import install, uninstall
from redh.prune import prune

from fakes import Lab, nextcloud


def text(findings, topic=None):
    return "\n".join(f"{f.level} {f.topic} {f.text}" for f in findings if topic in (None, f.topic))


class SiteTest(unittest.TestCase):
    """Caddy snippets go to /etc/caddy/Caddyfile.d, like the units go to the Quadlet directory."""

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)
        self.lab.system.caddy_user = "caddy"
        self.installed = os.path.join(self.lab.settings.caddy_dir, "nextcloud.caddy")
        self.source = os.path.join(self.lab.settings.root, "nextcloud", "caddy", "nextcloud.caddy")

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def test_install_copies_validates_and_reloads(self):
        rc = install(self.lab.ctx, ["nextcloud"])
        out = self.lab.output()
        self.assertEqual(rc, 0, out)
        content = self.read(self.installed)
        self.assertEqual(content.splitlines()[0],
                         f"# Installed by redh from {self.source} - edit that file, then: "
                         f"redh install nextcloud")
        self.assertIn("reverse_proxy 127.0.0.1:8080", content)
        self.assertEqual(stat.S_IMODE(os.stat(self.installed).st_mode), 0o640)
        self.assertEqual(self.lab.system.reloads, ["caddy.service"])
        self.assertIn(f"+ {self.installed}", out)
        self.assertIn("ok    reloaded caddy.service", out)
        # Nothing new: nothing written, no reload.
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0)
        self.assertEqual(self.lab.system.reloads, ["caddy.service"])

    def test_caddy_rejecting_a_change_puts_the_old_file_back(self):
        install(self.lab.ctx, ["nextcloud"])
        before = self.read(self.installed)
        self.lab.write(self.source, "cloud.example.org {\n    BROKEN\n}\n")
        self.lab.output()
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 1)
        out = self.lab.output()
        self.assertIn(f"~ {self.installed}", out)
        self.assertIn("FAIL  Caddy rejects the new snippet; the old one is back in place", out)
        self.assertIn("unrecognized directive: BROKEN", out)
        self.assertEqual(self.read(self.installed), before)
        self.assertEqual(self.lab.system.reloads, ["caddy.service"])      # only the first time

    def test_a_caddyfile_that_does_not_import_the_directory(self):
        self.lab.write(self.lab.settings.caddyfile, "{\n}\n")
        self.assertEqual(install(self.lab.ctx, ["nextcloud"]), 0)
        out = self.lab.output()
        self.assertIn("does not import", out)
        self.assertIn(f"add to {self.lab.settings.caddyfile}: "
                      f"import {self.lab.settings.caddy_dir}/*.caddy", out)
        self.assertEqual(self.lab.system.reloads, [])
        ctx = self.lab.fresh()
        self.assertIn(f"does not import {self.lab.settings.caddy_dir}, where redh installs the "
                      f"snippets", text(check_host(ctx, Survey(ctx)), "caddy"))

    def test_an_app_that_is_only_a_site(self):
        self.lab.write("westfall/caddy/Caddyfile", "http://westfall.internal {\n    respond ok\n}\n")
        self.assertEqual(install(self.lab.ctx, ["westfall"]), 0, self.lab.output())
        self.assertTrue(os.path.isfile(os.path.join(self.lab.settings.caddy_dir, "westfall.caddy")))
        self.assertFalse(os.path.exists(self.lab.settings.install_dir("westfall")))

    def test_a_snippet_removed_from_the_source_is_taken_out(self):
        source = self.lab.write("step-ca/caddy/Caddyfile", "http://westfall.internal {\n}\n")
        install(self.lab.ctx, ["step-ca"])
        installed = os.path.join(self.lab.settings.caddy_dir, "step-ca.caddy")
        self.assertTrue(os.path.isfile(installed))
        os.unlink(source)                       # moved into its own repository
        self.lab.output()
        self.assertEqual(install(self.lab.fresh(), ["step-ca"]), 0, self.lab.output())
        self.assertFalse(os.path.exists(installed))
        self.assertEqual(self.lab.system.reloads, ["caddy.service"] * 2)

    def test_someone_elses_file_of_the_same_name_blocks(self):
        os.makedirs(self.lab.settings.caddy_dir)
        self.lab.write(self.installed, "# mine, by hand\n")
        self.assertEqual(install(self.lab.ctx, ["nextcloud"]), 1)
        self.assertIn("nextcloud.caddy belongs to no redh app, not nextcloud", self.lab.output())
        self.assertEqual(self.read(self.installed), "# mine, by hand\n")

    def test_uninstall_and_prune_take_the_snippet_away(self):
        install(self.lab.ctx, ["nextcloud"])
        self.assertEqual(uninstall(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        self.assertFalse(os.path.exists(self.installed))
        self.assertEqual(self.lab.system.reloads, ["caddy.service"] * 2)
        install(self.lab.fresh(), ["nextcloud"])
        self.lab.output()
        self.assertEqual(prune(self.lab.fresh(), "nextcloud", yes=True), 0, self.lab.output())
        self.assertFalse(os.path.exists(self.installed))

    def test_check_follows_the_installed_copy(self):
        ctx = self.lab.fresh()
        before = check_app(ctx, "nextcloud", Survey(ctx))
        self.assertIn("warn install not installed → redh install nextcloud", text(before))
        install(self.lab.fresh(), ["nextcloud"])
        self.lab.write(self.source, "cloud.example.org {\n    reverse_proxy 127.0.0.1:8081\n}\n")
        ctx = self.lab.fresh()
        after = text(check_app(ctx, "nextcloud", Survey(ctx)))
        self.assertIn("is out of date → redh install nextcloud", after)
        self.assertNotIn("straight from", after)

    def test_the_isolated_check_uses_the_source_not_the_installed_copy(self):
        install(self.lab.ctx, ["nextcloud"])
        self.lab.write(self.source, "cloud.example.org {\n    BROKEN\n}\n")
        ctx = self.lab.fresh()
        found = text(check_app(ctx, "nextcloud", Survey(ctx)), "caddy")
        self.assertIn("FAIL caddy caddy/nextcloud.caddy does not adapt in", found)

    def test_caddy_reading_in_place_is_pointed_out(self):
        nextcloud(self.lab, in_place=True)
        ctx = self.lab.fresh()
        found = text(check_app(ctx, "nextcloud", Survey(ctx)), "caddy")
        self.assertIn("FAIL caddy Caddy reads caddy/nextcloud.caddy straight from", found)

    def test_installed_snippets_close_opt_podman_again(self):
        root = self.lab.settings.root
        self.lab.system.setfacl(["-m", "u:caddy:rx", root])          # left by an earlier --fix
        install(self.lab.fresh(), ["nextcloud"])
        ctx = self.lab.fresh()
        [breach] = perms.evaluate(ctx, Survey(ctx), [root])[0]
        self.assertEqual(breach.commands[-1], ["setfacl", "-x", "u:caddy", root])
        perms.fix(self.lab.fresh(), Survey(self.lab.fresh()), ["nextcloud"])
        self.assertEqual(self.lab.system.acl[root], {})
        ctx = self.lab.fresh()
        self.assertEqual(perms.evaluate(ctx, Survey(ctx), [root])[0], [])

    def test_dest_names(self):
        self.assertEqual(sites.dest_name("blog", "/b", "/b/Caddyfile"), "blog.caddy")
        self.assertEqual(sites.dest_name("blog", "/b", "/b/blog.caddy"), "blog.caddy")
        self.assertEqual(sites.dest_name("blog", "/b", "/b/admin.caddy"), "blog.admin.caddy")
        self.assertEqual(sites.dest_name("blog", "/b", "/b/sub/x.Caddyfile"), "blog.sub.x.caddy")


if __name__ == "__main__":
    unittest.main()
