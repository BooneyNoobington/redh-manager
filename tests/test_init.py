import os
import stat
import unittest

from redh.caddy import site_addresses
from redh.checks import Survey, check_app, run_check
from redh.init import init, parent_domain
from redh.install import install

from fakes import Lab, nextcloud


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class InitTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.root = self.lab.settings.root

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def read(self, *parts):
        with open(self.path(*parts)) as fh:
            return fh.read()

    def refused(self, *args, **kwargs):
        with self.assertRaises(SystemExit) as cm:
            init(self.lab.ctx, *args, **kwargs)
        return str(cm.exception.code)

    def test_bare_skeleton_marks_what_only_a_person_knows(self):
        self.assertEqual(init(self.lab.ctx, "web"), 0)
        out = self.lab.output()
        self.assertIn("+ quadlet/web.container", out)
        self.assertIn("+ caddy/Caddyfile", out)
        self.assertIn("3 lines marked TODO(redh init) to fill in", out)
        self.assertIn("quadlet/web.container:8  the image, fully qualified", out)
        self.assertIn("the port web listens on inside the container", out)
        self.assertIn("caddy/Caddyfile:4  the site's address", out)

        for parts in [(), ("quadlet",), ("caddy",)]:
            self.assertEqual(mode(self.path("web", *parts)), 0o700, parts)
        for parts in [("quadlet", "web.container"), ("caddy", "Caddyfile")]:
            self.assertEqual(mode(self.path("web", *parts)), 0o600, parts)
        self.assertFalse(os.path.exists(self.path("web", "data")))

        unit = self.read("web", "quadlet", "web.container")
        self.assertIn("\nImage=\n", unit)
        self.assertIn("PublishPort=127.0.0.1:8000:80", unit)
        self.assertIn("\n#Volume=", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("web.example.org {\n\treverse_proxy 127.0.0.1:8000\n}",
                      self.read("web", "caddy", "Caddyfile"))

        # check lists the placeholders, install refuses while any is left.
        findings = check_app(self.lab.fresh(), "web", Survey(self.lab.ctx))
        init_fails = [f for f in findings if f.topic == "init"]
        self.assertEqual(len(init_fails), 1)
        self.assertEqual(init_fails[0].level, "FAIL")
        self.assertEqual(len(init_fails[0].details), 4)
        self.assertEqual(install(self.lab.fresh(), ["web"]), 1)
        self.assertIn("nothing installed", self.lab.output())
        self.assertFalse(os.path.exists(os.path.join(self.lab.settings.quadlet_dir, "web")))

    def test_with_everything_given_it_checks_clean_and_installs(self):
        nextcloud(self.lab)
        rc = init(self.lab.ctx, "web", image="docker.io/library/nginx:1", port="80",
                  domain="www.example.org", data="/usr/share/nginx/html", env=True)
        out = self.lab.output()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("TODO", out)
        unit = self.read("web", "quadlet", "web.container")
        self.assertNotIn("TODO", unit)
        self.assertIn("Image=docker.io/library/nginx:1", unit)
        self.assertIn(f"Volume={self.path('web', 'data')}:/usr/share/nginx/html:Z", unit)
        self.assertIn("\nEnvironmentFile=web.env", unit)
        self.assertEqual(mode(self.path("web", "data")), 0o700)
        self.assertEqual(mode(self.path("web", "quadlet", "web.env")), 0o600)

        rc = run_check(self.lab.fresh(), ["web"])
        out = self.lab.output()
        self.assertEqual(rc, 0, out)
        self.assertIn("www.example.org", self.read("web", "caddy", "Caddyfile"))
        self.assertIn("published by web.container", out)
        self.assertIn("file with secrets only the owner can read: quadlet/web.env", out)

        self.assertEqual(install(self.lab.fresh(), ["web"]), 0, self.lab.output())
        self.assertTrue(os.path.isfile(os.path.join(self.lab.settings.caddy_dir, "web.caddy")))
        self.assertEqual(self.lab.system.units["web.service"]["LoadState"], "loaded")

    def test_host_port_skips_published_and_listening_ones(self):
        self.lab.write("api/quadlet/api.container", """
            [Container]
            Image=docker.io/x/api:1
            PublishPort=127.0.0.1:8001-8002:80
            """)
        self.lab.system.listening = {8000, 8004}
        init(self.lab.ctx, "web", port="3000", site=False)
        self.assertIn("PublishPort=127.0.0.1:8003:3000", self.read("web", "quadlet", "web.container"))
        self.assertIn("host port 8003", self.lab.output())
        self.assertFalse(os.path.exists(self.path("web", "caddy")))

        message = self.refused("blog", port="8001:2368")
        self.assertIn("port 8001 is taken: api publishes it (api.container)", message)

    def test_no_caddy_and_no_port_publishes_nothing(self):
        init(self.lab.ctx, "worker", image="ghcr.io/x/worker:2", site=False)
        unit = self.read("worker", "quadlet", "worker.container")
        self.assertNotIn("PublishPort", unit)
        self.assertNotIn("TODO", unit)

    def test_site_follows_the_domain_of_the_other_sites(self):
        nextcloud(self.lab)
        self.lab.write(os.path.join(self.lab.settings.caddy_dir, "git.caddy"),
                       "forge.internal {\n\treverse_proxy 127.0.0.1:3000\n}\n")
        self.lab.write(os.path.join(self.lab.settings.caddy_dir, "start.caddy"),
                       "start.internal, www.start.internal {\n\treverse_proxy :8085\n}\n")
        init(self.lab.ctx, "wiki", image="docker.io/x/wiki:1", port="80")
        self.assertIn("wiki.internal {", self.read("wiki", "caddy", "Caddyfile"))
        self.assertIn("site wiki.internal, like 2 other sites", self.lab.output())

    def test_dry_run_creates_nothing(self):
        init(self.lab.ctx, "web", dry_run=True)
        out = self.lab.output()
        self.assertIn("dry run: nothing created", out)
        self.assertIn("3 lines marked TODO(redh init)", out)
        self.assertFalse(os.path.exists(self.path("web")))

    def test_an_empty_directory_is_filled_and_a_path_names_the_app(self):
        os.mkdir(self.path("web"), 0o755)
        self.assertEqual(init(self.lab.ctx, self.path("web") + "/", site=False), 0)
        self.assertEqual(mode(self.path("web")), 0o700)
        self.assertTrue(os.path.isfile(self.path("web", "quadlet", "web.container")))

    def test_refusals(self):
        self.lab.write("web/notes.txt", "mine\n")
        self.assertIn("already exists", self.refused("web"))
        os.symlink("/etc", self.path("etc"))
        self.assertIn("is not a directory", self.refused("etc"))
        self.assertIn("cannot be an app", self.refused("../x"))
        self.assertIn("fully-qualified", self.refused("blog", image="ghost:5"))
        self.assertIn("docker.io/library/ghost:5", self.refused("blog", image="ghost:5"))
        self.assertIn("--domain takes", self.refused("blog", domain="a.org {\n}"))
        self.assertIn("--data takes", self.refused("blog", data="data"))
        self.assertIn("--port takes", self.refused("blog", port="http"))
        self.lab.write("api/quadlet/blog.container", "[Container]\nImage=docker.io/x/y:1\n")
        self.assertIn("api", self.refused("blog"))
        self.assertFalse(os.path.exists(self.path("blog")))


class SiteAddressTest(unittest.TestCase):
    def test_only_site_blocks_count(self):
        text = """
        {
            email admin@example.org
        }
        (common) {
            encode gzip
        }
        forge.internal, https://code.internal:443 {
            @api path /api/*
            handle @api {
                reverse_proxy 127.0.0.1:3000
            }
        }
        # old.internal {
        :8443 {
            respond "hi"
        }
        """
        self.assertEqual(site_addresses(text), ["forge.internal", "https://code.internal:443", ":8443"])

    def test_parent_domain(self):
        self.assertEqual(parent_domain("forge.internal"), "internal")
        self.assertEqual(parent_domain("https://*.home.arpa:443"), "home.arpa")
        self.assertEqual(parent_domain("cloud.example.org"), "example.org")
        self.assertIsNone(parent_domain(":8443"))
        self.assertIsNone(parent_domain("127.0.0.1"))
        self.assertIsNone(parent_domain("localhost"))


if __name__ == "__main__":
    unittest.main()
