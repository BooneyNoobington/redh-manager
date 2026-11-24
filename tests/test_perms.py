import os
import pwd
import stat
import unittest

from redh import perms
from redh.checks import Findings, Survey, check_app, check_host
from redh.install import install

from fakes import Lab


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class PolicyTest(unittest.TestCase):
    """/opt/podman and its apps are podman's alone; Caddy gets nothing below it."""

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        s = self.lab.settings
        self.root = s.root
        self.blog = os.path.join(self.root, "blog")
        self.data = os.path.join(self.blog, "data")
        os.makedirs(self.data)
        self.lab.write("blog/quadlet/blog.container", f"""
            [Container]
            Image=docker.io/library/ghost:5
            Volume={self.data}:/var/lib/ghost/content
            """)
        self.lab.write("blog/caddy/Caddyfile", "blog.example.org {\n    reverse_proxy 127.0.0.1:2368\n}\n")
        os.makedirs(os.path.join(self.root, "redhdb"))
        self.lab.write(s.caddyfile, f"import {s.caddy_dir}/*.caddy\n")
        self.lab.system.caddy_user = "caddy"
        install(self.lab.ctx, ["blog"])
        self.lab.output()

    def breaches(self, *dirs):
        ctx = self.lab.fresh()
        return {b.path: b for b in perms.evaluate(ctx, Survey(ctx), list(dirs or [self.root]))[0]}

    def fix(self, apps=("blog", "redhdb")):
        ctx = self.lab.fresh()
        ok = perms.fix(ctx, Survey(ctx), list(apps))
        return ok, self.lab.output()

    def open_root(self):
        os.chmod(self.root, 0o755)
        self.addCleanup(os.chmod, self.root, 0o700)

    def test_closed_and_owned_is_fine(self):
        self.assertEqual(self.breaches(self.root, self.blog), {})
        ctx = self.lab.fresh()
        found = [(f.level, f.text) for f in check_host(ctx, Survey(ctx)) if f.topic == "perms"]
        self.assertEqual(found, [("ok", "only its owner gets in")])
        self.assertEqual(self.fix()[1], "fix\n  ok    nothing to fix\n")

    def test_an_open_root_and_open_apps_get_closed(self):
        self.open_root()
        found = self.breaches(self.root, self.blog)
        self.assertEqual(found[self.root].problems, ["every user can list it", "every user can enter it"])
        self.assertEqual(found[self.root].commands, [["chmod", "700", self.root]])
        self.assertEqual(found[self.blog].commands, [["chmod", "700", self.blog]])
        ok, out = self.fix()
        self.assertTrue(ok, out)
        self.assertEqual(mode(self.root), 0o700)
        self.assertEqual(mode(self.blog), 0o700)
        self.assertEqual(self.breaches(self.root, self.blog), {})

    def test_acl_entries_for_caddy_are_removed(self):
        self.lab.system.setfacl(["-m", "u:caddy:rx", self.root])     # what the old --fix did
        [breach] = self.breaches().values()
        self.assertEqual(breach.problems, ["Caddy (caddy) can list it", "Caddy (caddy) can enter it"])
        self.assertEqual(breach.commands, [["chmod", "700", self.root],
                                           ["setfacl", "-x", "u:caddy", self.root]])
        self.fix()
        self.assertEqual(self.lab.system.acl[self.root], {})
        self.assertEqual(self.breaches(), {})

    def test_a_masked_entry_is_still_one_too_many(self):
        self.lab.system.setfacl(["-m", "u:caddy:x", self.root])
        os.chmod(self.root, 0o700)                                   # mask now ---: no access...
        [breach] = self.breaches().values()
        self.assertEqual(breach.problems, ["an ACL entry still grants Caddy (caddy) x"])
        self.fix()
        self.assertEqual(self.lab.system.acl[self.root], {})

    def test_what_caddy_still_opens_fails_and_stays_open(self):
        html = os.path.join(self.root, "step-ca", "html")
        self.lab.write("step-ca/html/root_ca.crt", "CERT\n", mode=0o644)
        self.lab.write("step-ca/caddy/Caddyfile",
                       f"http://westfall.internal {{\n    root * {html}\n    file_server\n}}\n")
        install(self.lab.fresh(), ["step-ca"])
        for path in (self.root, os.path.join(self.root, "step-ca")):
            self.lab.system.setfacl(["-m", "u:caddy:x", path])       # how the site works today
        ctx = self.lab.fresh()
        findings = Findings()
        perms.check_app(findings, ctx, Survey(ctx), "step-ca")
        self.assertEqual([(f.level, f.text, f.details) for f in findings][0],
                         ("FAIL", f"Caddy serves {html}, but nothing below {self.root} is Caddy's",
                          [f"move it out of {self.root}"]))
        ok, out = self.fix(apps=("step-ca",))
        self.assertIn(f"skip  {self.root}: Caddy's configuration still opens 1 path below it "
                      f"({html}); move it out first", out)
        self.assertNotIn("chmod 700", out)
        self.assertEqual(self.lab.system.acl[self.root], {"caddy": "x"})     # the site still works
        # Moved out of /opt/podman: now --fix closes everything.
        westfall = os.path.join(self.lab.tmp, "srv", "westfall")
        os.makedirs(westfall)
        self.lab.write("step-ca/caddy/Caddyfile",
                       f"http://westfall.internal {{\n    root * {westfall}\n    file_server\n}}\n")
        install(self.lab.fresh(), ["step-ca"])
        ok, out = self.fix(apps=("step-ca",))
        self.assertIn(f"ok    setfacl -x u:caddy {self.root}", out)
        self.assertEqual(self.lab.system.acl[self.root], {})
        self.assertEqual(self.breaches(self.root, os.path.join(self.root, "step-ca")), {})

    def test_a_ca_root_belongs_in_the_trust_store(self):
        cert = self.lab.write("step-ca/certs/root_ca.crt", "CERT\n")
        self.lab.write("blog/caddy/Caddyfile",
                       f"blog.example.org {{\n    tls {{\n        ca_root {cert}\n    }}\n}}\n")
        install(self.lab.fresh(), ["blog"])
        ctx = self.lab.fresh()
        # Reported with the app whose directory holds it.
        found = [(f.text, f.details) for f in check_app(ctx, "step-ca", Survey(ctx))
                 if f.level == "FAIL"]
        self.assertIn((f"Caddy reads {cert}, but nothing below {self.root} is Caddy's",
                       ["a CA root belongs in the system trust store"]), found)

    def test_a_snippet_read_in_place_holds_back_closing(self):
        self.lab.write(self.lab.settings.caddyfile, f"import {self.root}/*/caddy/Caddyfile\n")
        self.lab.system.setfacl(["-m", "u:caddy:rx", self.root])
        ok, out = self.fix()
        self.assertIn(f"skip  {self.root}: Caddy's configuration still opens", out)
        ctx = self.lab.fresh()
        caddy = [f.text for f in check_app(ctx, "blog", Survey(ctx)) if f.topic == "caddy"]
        self.assertIn(f"Caddy reads caddy/Caddyfile straight from {self.blog}, but nothing below "
                      f"{self.root} is Caddy's", caddy)

    def test_a_directory_a_running_container_mounts_keeps_its_bits(self):
        self.open_root()
        vault = os.path.join(self.root, "vaultwarden")
        os.makedirs(vault)
        self.lab.system.run_container("vaultwarden", sources=[vault])
        ok, out = self.fix(apps=("vaultwarden",))
        self.assertEqual(mode(vault), 0o755)
        self.assertIn(f"skip  {vault}: a container mounts it", out)

    def test_an_owner_other_than_podman(self):
        self.lab.settings.uid = os.getuid() + 1
        me = pwd.getpwuid(os.getuid()).pw_name
        [breach] = self.breaches(self.blog).values()
        self.assertEqual(breach.problems[0], f"it belongs to {me}, not podman")
        self.assertEqual(breach.commands[0], ["chown", "podman:", self.blog])

    def test_without_caddy_redh_cannot_tell_and_holds_back(self):
        self.open_root()
        self.lab.settings.caddy = "no-such-caddy"
        ok, out = self.fix(apps=())
        self.assertIn(f"skip  {self.root}: no-such-caddy not found, so redh cannot tell", out)
        self.assertEqual(mode(self.root), 0o755)

    def test_without_root_only_owners_are_checked(self):
        self.lab.system.open_above = None
        ctx = self.lab.fresh()
        findings = [f for f in check_host(ctx, Survey(ctx)) if f.topic == "perms"]
        self.assertEqual([(f.level, f.text) for f in findings],
                         [("info", "only root can test who gets in (ACLs, groups); owners checked")])


if __name__ == "__main__":
    unittest.main()
