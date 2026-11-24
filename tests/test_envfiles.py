import os
import unittest

from redh import envfiles
from redh.checks import Findings, Survey, check_app, check_host
from redh.install import install

from fakes import Lab


def secrets(findings):
    return [(f.level, f.text, f.details) for f in findings if f.topic == "secrets"]


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.home = self.lab.settings.home
        self.app = os.path.join(self.home, "blog")
        self.lab.write("blog/quadlet/blog.container", """
            [Container]
            Image=docker.io/library/ghost:5
            EnvironmentFile=blog.env
            """)

    def open_root(self):
        """A /opt/podman others may enter, as the strict policy does not want it."""
        os.chmod(self.home, 0o755)
        self.addCleanup(os.chmod, self.home, 0o700)

    def check(self, app="blog"):
        ctx = self.lab.fresh()
        findings = Findings()
        envfiles.check_app(findings, ctx, Survey(ctx), app)
        return secrets(findings)

    def test_a_password_file_counts_like_an_env_file(self):
        """dms keeps its step-ca provisioner password in .ca.pw, next to the renewal script."""
        self.open_root()
        self.lab.write("blog/quadlet/blog.env", "X=1\n", mode=0o600)
        self.lab.write("blog/.ca.pw", "hunter2\n", mode=0o644)
        found = self.check()
        self.assertEqual([f[0] for f in found], ["FAIL", "ok"])
        self.assertIn(".ca.pw (-rw-r--r--", found[0][1])
        self.assertIn("every user of this host can read it", found[0][1])
        os.chmod(os.path.join(self.app, ".ca.pw"), 0o600)
        self.assertEqual([f[:2] for f in self.check()],
                         [("ok", "2 files with secrets only the owner can read: "
                                 ".ca.pw, quadlet/blog.env")])

    def test_readable_by_everyone_fails(self):
        self.open_root()
        path = self.lab.write("blog/quadlet/blog.env", "DB_PASSWORD=hunter2\n", mode=0o644)
        install(self.lab.fresh(), ["blog"])
        [(level, text, fix)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertTrue(text.startswith("quadlet/blog.env (-rw-r--r-- "), text)
        self.assertTrue(text.endswith(": every user of this host can read it"), text)
        copy = os.path.join(self.lab.settings.install_dir("blog"), "blog.env")
        self.assertEqual(fix, [f"chmod go-rwx {path}", f"chmod go-rwx {copy}"])

    def test_a_file_a_container_mounts_gets_the_chown_hint(self):
        data = os.path.join(self.app, "data")
        self.lab.write("blog/quadlet/blog.container", f"""
            [Container]
            Image=docker.io/library/ghost:5
            Volume={data}:/var/lib/ghost/content
            """)
        path = self.lab.write("blog/data/.env", "X=1\n", mode=0o644)
        [(level, _, fix)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertEqual(fix, [f"a container mounts {data}: if it reads this file as a non-root "
                               f"user, give it to that user instead (podman unshare chown UID:GID "
                               f"{path}) and then take the bits away"])

    def test_writable_by_everyone_fails(self):
        self.open_root()
        self.lab.write("blog/.env", "X=1\n", mode=0o606)
        [(level, text, _)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertIn("every user of this host can change it", text)

    def test_open_but_shielded_is_strict_too(self):
        self.open_root()
        self.lab.write("blog/quadlet/blog.env", "X=1\n", mode=0o644)
        os.chmod(self.app, 0o700)
        self.addCleanup(os.chmod, self.app, 0o755)
        [(level, text, _)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertIn(f"it is open to everyone; only {self.app} (drwx------ ", text)

    def test_caddy_reaching_in_through_an_acl_fails(self):
        self.lab.write("blog/.env", "X=1\n", mode=0o644)
        self.lab.system.caddy_user = "caddy"
        self.lab.system.setfacl(["-m", "u:caddy:x", self.home])
        [(level, text, _)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertIn("caddy, the user Caddy runs as, can read it", text)

    def test_private_files_are_fine(self):
        self.lab.write("blog/quadlet/blog.env", "X=1\n", mode=0o600)
        self.lab.write("blog/config/.env", "X=1\n", mode=0o400)
        self.assertEqual(self.check(), [("ok", "2 files with secrets only the owner can read: "
                                               "config/.env, quadlet/blog.env", [])])

    def test_a_group_with_other_members_fails(self):
        self.lab.write("blog/.env", "X=1\n", mode=0o640)
        self.lab.system.groups[os.getgid()] = {"markus", "backup"}
        [(level, text, _)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertIn("(backup, markus) can read it", text)

    def test_a_foreign_owner_fails_unless_it_is_a_container_uid(self):
        path = self.lab.write("blog/.env", "X=1\n", mode=0o600)
        ctx = self.lab.fresh()
        survey = Survey(ctx)
        self.lab.settings.uid = os.getuid() + 1      # the file is no longer the podman user's
        findings = Findings()
        envfiles.check_app(findings, ctx, survey, "blog")
        [(level, text, fix)] = secrets(findings)
        self.assertEqual(level, "FAIL")
        self.assertIn("it belongs to ", text)
        self.assertEqual(fix, [f"chown podman: {path}"])
        survey.subuids = [(os.getuid(), 1)]          # ... but one of its containers' UIDs
        findings = Findings()
        envfiles.check_app(findings, ctx, survey, "blog")
        self.assertEqual(secrets(findings)[0][0], "ok")

    def test_what_is_not_looked_at(self):
        self.lab.write("blog/.env.example", "X=changeme\n", mode=0o644)
        self.lab.write("blog/src/env.js", "export default {}\n", mode=0o644)
        self.lab.write("blog/node_modules/dotenv/.env", "X=1\n", mode=0o644)
        self.lab.write("blog/a/b/c/.env", "X=1\n", mode=0o644)          # deeper than 3 levels
        # 'env' inside a word: paperless exports and the like.
        self.lab.write("blog/export/2023 Rentenversicherung.pdf", "%PDF\n", mode=0o644)
        self.lab.write("blog/export/envivas-brief-thumbnail.webp", "RIFF\n", mode=0o644)
        self.assertEqual(self.check(), [])

    def test_env_or_a_word_for_a_secret_as_a_part_of_the_name(self):
        for name in (".env", ".env.local", "db.env", "env.list", "prod-environment", "Mail.ENV",
                     ".ca.pw", ".ca-pw", "api-key", "mail.key", "secrets.yaml", "db_password",
                     "github.token", "credentials.json", "privkey.pem"):
            self.assertTrue(envfiles.looks_secret(name), name)
        for name in ("Patientenverfuegung.pdf", "envelope.png", ".zshenv", "dbEnv", "env.js",
                     "keyboard.conf", "monkey.txt", "Password Reset.pdf", "ssh_host_ed25519_key.pub",
                     "secret-santa.jpg", "passwords.md", "tokenizer.py"):
            self.assertFalse(envfiles.looks_secret(name), name)

    def test_environment_files_count_whatever_their_name(self):
        self.open_root()
        outside = self.lab.write(os.path.join(self.lab.tmp, "etc", "blog", "secret.conf"),
                                 "X=1\n", mode=0o644)
        self.lab.write("blog/quadlet/blog.container", f"""
            [Container]
            Image=docker.io/library/ghost:5
            EnvironmentFile={outside}
            """)
        [(level, text, _)] = self.check()
        self.assertEqual(level, "FAIL")
        self.assertTrue(text.startswith(f"{outside} ("), text)

    def test_without_root_the_bits_decide(self):
        self.lab.system.open_above = None            # cannot take on 'nobody'
        self.lab.write("blog/.env", "X=1\n", mode=0o644)
        [(level, text, _)] = self.check()
        # The test's own temporary directory is closed, so it counts as shielded.
        self.assertEqual(level, "FAIL")
        self.assertIn("it is open to everyone; only ", text)

    def test_apps_that_are_no_redh_apps_yet_are_checked_too(self):
        self.lab.write("vaultwarden/.env", "ADMIN_TOKEN=x\n", mode=0o644)
        ctx = self.lab.fresh()
        findings = check_app(ctx, "vaultwarden", Survey(ctx))
        self.assertEqual([(f.topic, f.level) for f in findings],
                         [("layout", "info"), ("perms", "ok"), ("secrets", "FAIL")])

    def test_loose_files_in_the_quadlet_directory(self):
        qdir = self.lab.settings.quadlet_dir
        self.lab.write(os.path.join(qdir, "vaultwarden.env"), "X=1\n", mode=0o644)
        token = self.lab.write("vaultwarden/admin-token", "X=1\n", mode=0o644)
        self.lab.write(os.path.join(qdir, "vaultwarden.container"),
                       f"[Container]\nImage=docker.io/vaultwarden/server:1\nEnvironmentFile={token}\n")
        ctx = self.lab.fresh()
        found = [text for level, text, _ in secrets(check_host(ctx, Survey(ctx))) if level == "FAIL"]
        self.assertEqual(sorted(t.split(" (")[0] for t in found),
                         sorted([os.path.join(qdir, "vaultwarden.env"), token]))


if __name__ == "__main__":
    unittest.main()
