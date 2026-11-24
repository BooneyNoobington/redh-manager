import os
import unittest

from redh.checks import Survey, check_app, check_host, run_check
from redh.install import install

from fakes import Lab, nextcloud


def lines(findings, level=None):
    return [f"{f.level} {f.topic} {f.text}" for f in findings if level in (None, f.level)]


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        nextcloud(self.lab)

    def check(self, app):
        ctx = self.lab.fresh()
        return check_app(ctx, app, Survey(ctx))

    def test_tidy_app_has_no_problems_once_installed(self):
        findings = self.check("nextcloud")
        self.assertEqual(lines(findings, "FAIL"), [])
        self.assertIn("warn install not installed → redh install nextcloud", lines(findings))
        install(self.lab.ctx, ["nextcloud"])
        self.lab.system.start_all()
        findings = self.check("nextcloud")
        self.assertEqual(lines(findings, "FAIL") + lines(findings, "warn"), [])
        text = "\n".join(lines(findings))
        self.assertIn("generator Quadlet accepts all 3", text)
        self.assertIn("update 1 unit auto-update: nextcloud.container", text)
        self.assertIn("caddy caddy/nextcloud.caddy adapts cleanly in", text)
        self.assertIn("runtime all 2 units active, 1 task (nextcloud-db-volume.service)", text)

    def test_the_usual_mistakes(self):
        self.lab.write("blog/blog.container", """
            [Container]
            Image=ghost:5
            AutoUpdate=registry
            Volume=./content:/var/lib/ghost/content
            PublishPort=2368:2368

            [Install]
            WantedBy=multi-user.target
            """)
        self.lab.write("blog/blog-db.container", """
            [Container]
            Image=docker.io/library/mysql:8
            Volume=/does/not/exist:/var/lib/mysql
            EnvironmentFile=db.env
            """)
        self.lab.write("blog/db.env", "MYSQL_ROOT_PASSWORD=x\n")
        self.lab.write("blog/blog.caddy", """
            blog.example.org {
                reverse_proxy localhost:2368
                reverse_proxy /admin* localhost:2369
            }
            """)
        text = "\n".join(lines(self.check("blog")))
        self.assertIn("warn layout 2 unit files at the top level", text)
        self.assertIn("FAIL units blog.container: Volume=./content:/var/lib/ghost/content is relative", text)
        self.assertIn("FAIL units blog-db.container: Volume=/does/not/exist:/var/lib/mysql — /does/not/exist does not exist", text)
        self.assertIn("FAIL secrets db.env (-rw-r--r-- ", text)
        self.assertIn("it is open to everyone; only ", text)
        self.assertIn("FAIL units blog-db.container: EnvironmentFile=db.env is relative, but", text)
        self.assertIn("FAIL update blog.container: AutoUpdate=registry needs a fully-qualified image, not ghost:5", text)
        self.assertIn("warn update blog-db.container: no AutoUpdate=", text)
        self.assertIn("warn boot blog.container: WantedBy=multi-user.target does not exist for a user manager", text)
        self.assertIn("warn boot blog-db.container has no [Install] section", text)
        self.assertIn("warn caddy snippet at the top level", text)
        self.assertIn("warn install not installed → redh install blog", text)
        self.assertIn("PublishPort=2368:2368 listens on every interface", text)
        self.assertIn("warn caddy blog.caddy:3 proxies to localhost:2369, but no container publishes port 2369", text)

    def test_images_only_this_host_has(self):
        self.lab.write("db/quadlet/db-app.container", """
            [Container]
            Image=localhost/db-app:latest
            AutoUpdate=local
            """)
        image = [line for line in lines(self.check("db")) if " image " in line]
        self.assertEqual(len(image), 2, image)
        self.assertIn("warn image db-app.container: localhost/db-app:latest was built by hand", image[0])
        self.assertIn("FAIL image localhost/db-app:latest is not in podman's image store, "
                      "so db-app.service cannot start", image[1])

        self.lab.system.images["localhost/db-app:latest"] = "sha256:1"
        image = [line for line in lines(self.check("db")) if " image " in line]
        self.assertEqual([line.split(":")[0] for line in image], ["warn image db-app.container"])

        # A .build makes it, but the container names the image instead of the .build.
        self.lab.write("db/quadlet/db-app.build", """
            [Build]
            ImageTag=localhost/db-app
            File=/srv/db/Containerfile
            SetWorkingDirectory=file
            """)
        image = [line for line in lines(self.check("db")) if " image " in line]
        self.assertEqual(len(image), 1, image)
        self.assertIn("is what db-app.build builds", image[0])

        self.lab.write("db/quadlet/db-app.container", """
            [Container]
            Image=db-app.build
            AutoUpdate=local
            """)
        self.assertEqual([line for line in lines(self.check("db")) if " image " in line], [])

    def test_broken_snippet_is_blamed_on_its_own_app(self):
        self.lab.write("nextcloud/caddy/nextcloud.caddy", """
            cloud.example.org {
                BROKEN
            }
            """)
        self.lab.write("wiki/quadlet/wiki.container", "[Container]\nImage=docker.io/x/wiki:1\n")
        self.lab.write("wiki/caddy/nextcloud.caddy", "wiki.example.org {\n}\n")
        text = "\n".join(lines(self.check("nextcloud")))
        self.assertIn("FAIL caddy caddy/nextcloud.caddy does not adapt", text)
        self.assertIn("unrecognized directive: BROKEN", "\n".join(
            d for f in self.check("nextcloud") for d in f.details))
        wiki = "\n".join(lines(self.check("wiki")))
        self.assertIn("ok caddy caddy/nextcloud.caddy adapts cleanly", wiki)

    def test_caddy_cannot_reach_a_snippet_root_can(self):
        nextcloud(self.lab, in_place=True)           # the old way: Caddy reads /opt/podman
        self.lab.system.caddy_user = "caddy"
        self.lab.system.hidden_from_caddy = [self.lab.settings.root]
        ctx = self.lab.fresh()
        survey = Survey(ctx)
        app = check_app(ctx, "nextcloud", survey)
        failure = [f for f in app if f.level == "FAIL" and "cannot reach" in f.text][0]
        self.assertEqual(failure.text, "caddy/nextcloud.caddy: Caddy runs as caddy and cannot "
                                       "reach it, so its sites are not served")
        root = os.path.realpath(self.lab.settings.root)
        self.assertTrue(failure.details[0].startswith(f"caddy cannot enter {root} (d"), failure.details)
        # No ACL advice: under the policy, the fix is installing the snippet into /etc/caddy.
        self.assertNotIn("setfacl", "\n".join(failure.details))
        host = "\n".join(lines(check_host(ctx, survey)))
        # Not root here, so Caddy's validate ran as this user: 'as caddy' is left out.
        self.assertIn("matches nothing, but 1 file as podman — their sites are not served", host)

    def test_snippet_the_imports_miss(self):
        self.lab.write(self.lab.settings.caddyfile,
                       f"import {self.lab.settings.root}/*/Caddyfile\n")
        findings = self.check("nextcloud")
        warning = [f for f in findings if "is not imported" in f.text][0]
        self.assertIn("the imports there:", warning.details)
        self.assertTrue(any(d.endswith("import " + self.lab.settings.root + "/*/Caddyfile  (0 matches)")
                            for d in warning.details), warning.details)

    def test_cross_app_problems(self):
        self.lab.write("wiki/quadlet/nextcloud.network", "[Network]\n")
        self.lab.write("wiki/quadlet/wiki.container", """
            [Container]
            Image=docker.io/x/wiki:1
            PublishPort=127.0.0.1:8080:80
            Network=missing.network
            """)
        ctx = self.lab.fresh()
        survey = Survey(ctx)
        host = "\n".join(lines(check_host(ctx, survey)))
        self.assertIn("FAIL apps nextcloud.network exists in nextcloud, wiki", host)
        self.assertIn("FAIL apps port 8080 is published twice", host)
        wiki = "\n".join(lines(check_app(ctx, "wiki", survey)))
        self.assertIn("FAIL units wiki.container: Network=missing.network, but no app has missing.network", wiki)

    def test_host_flags_the_auto_update_timer(self):
        self.lab.system.timer = ("disabled", "inactive")
        ctx = self.lab.fresh()
        survey = Survey(ctx)
        host = "\n".join(lines(check_host(ctx, survey)))
        self.assertIn("FAIL update 1 unit set AutoUpdate=, but podman-auto-update.timer is disabled/inactive", host)
        self.assertIn("FAIL update AutoUpdate= is set, but podman-auto-update.timer is off",
                      "\n".join(lines(check_app(ctx, "nextcloud", survey))))

    def test_orphaned_install(self):
        install(self.lab.ctx, ["nextcloud"])
        os.rename(os.path.join(self.lab.settings.home, "nextcloud"),
                  os.path.join(self.lab.settings.home, ".nextcloud-old"))
        ctx = self.lab.fresh()
        text = "\n".join(lines(check_host(ctx, Survey(ctx))))
        self.assertIn("is installed, but", text)

    def test_run_check_exit_code_and_summary(self):
        self.lab.system.caddy_user = "root"          # nothing to grant Caddy then
        self.lab.output()
        self.assertEqual(run_check(self.lab.fresh(), ["nextcloud"]), 0)
        out = self.lab.output()
        self.assertTrue(out.startswith("host\n"))
        self.assertIn("1 app checked: 0 problems, 1 warning", out)


if __name__ == "__main__":
    unittest.main()
