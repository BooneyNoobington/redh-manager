import gzip
import io
import json
import os
import shutil
import stat
import tarfile
import unittest
from unittest import mock

from redh import transfer
from redh.install import install
from redh.transfer import Destination, Person, export_app, import_app, parse_destination

from fakes import Lab, nextcloud

VOLUME = "systemd-nextcloud-db"


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class TransferCase(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.home = os.path.join(self.lab.tmp, "home", "alex")
        os.makedirs(self.home)
        self.me = Person("alex", os.getuid(), os.getgid(), self.home)
        for patch in (mock.patch.object(transfer, "invoker", lambda host: self.me),
                      mock.patch.object(transfer.shutil, "which", lambda name: f"/usr/bin/{name}")):
            patch.start()
            self.addCleanup(patch.stop)
        self.downloads = self.me.downloads
        self.app = os.path.join(self.lab.settings.root, "nextcloud")

    def nextcloud(self, running=True):
        nextcloud(self.lab)
        self.lab.write("nextcloud/quadlet/secrets.env", "PW=x\n", mode=0o600)
        self.lab.write("nextcloud/data/index.html", "<h1>hi</h1>\n", mode=0o640)
        os.symlink("/var/www/html/index.html", os.path.join(self.app, "data", "link"))
        os.chmod(self.app, 0o700)
        self.lab.system.objects.add(("volume", VOLUME))
        self.lab.system.volumes[VOLUME] = "the database"
        install(self.lab.ctx, ["nextcloud"])
        if running:
            self.lab.system.start_all()
        self.lab.output()
        self.lab.system.calls.clear()

    def archives(self, directory=None):
        directory = directory or self.downloads
        return sorted(n for n in os.listdir(directory) if n.endswith(transfer.SUFFIX))

    def verbs(self):
        """What happened, in order: systemd's stop and start, tar, rsync."""
        found = []
        for call in self.lab.system.calls:
            if call[:2] == ["systemctl", "--user"] and call[2] in ("stop", "start", "restart"):
                found.append(call[2])
            elif call[:3] == ["podman", "unshare", "tar"]:
                found.append("tar")
            elif "rsync" in call:
                found.append("rsync")
        return [v for i, v in enumerate(found) if i == 0 or found[i - 1] != v]

    def export(self, *args, **kwargs):
        rc = export_app(self.lab.fresh(), "nextcloud", *args, **kwargs)
        return rc, self.lab.output()


class DestinationTest(unittest.TestCase):
    def test_the_forms(self):
        me = Person("alex", 1, 1, "/home/alex")
        for text, expected in (
                (None, Destination(directory="/home/alex/Downloads")),
                ("server.internal", Destination(remote="alex@server.internal:Downloads/")),
                ("new@server.internal", Destination(remote="new@server.internal:Downloads/")),
                ("new@server.internal:/home/new/Downloads",
                 Destination(remote="new@server.internal:/home/new/Downloads/")),
                ("server:backups/", Destination(remote="alex@server:backups/")),
                ("/srv/moved", Destination(directory="/srv/moved"))):
            self.assertEqual(parse_destination(text, me), expected, text)

    def test_nothing_that_rsync_or_ssh_would_read_differently(self):
        me = Person("alex", 1, 1, "/home/alex")
        for text in ("-oProxyCommand=x", "-e", "a b", "host::module", "-u@host", "@host:x"):
            if text == "@host:x":                 # no user given: mine
                self.assertEqual(parse_destination(text, me).remote, "alex@host:x/")
                continue
            with self.assertRaises(SystemExit, msg=text):
                parse_destination(text, me)


class ExportTest(TransferCase):
    def test_a_running_app_is_stopped_packed_and_started_again(self):
        self.nextcloud()
        rc, out = self.export()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.verbs(), ["stop", "tar", "start"])
        self.assertEqual(self.lab.system.units["nextcloud.service"]["ActiveState"], "active")
        [name] = self.archives()
        self.assertRegex(name, r"^nextcloud-\d{8}-\d{6}\.redh\.tar\.gz$")
        self.assertEqual(mode(os.path.join(self.downloads, name)), 0o600)
        self.assertIn("alex's alone (0600)", out)
        self.assertEqual(os.listdir(self.lab.settings.root).count(".redh-export"), 0)
        self.assertFalse([n for n in os.listdir(self.lab.settings.root) if n.startswith(".redh")])
        self.assertEqual([n for n in os.listdir(self.downloads) if n.endswith(".part")], [])

        with tarfile.open(os.path.join(self.downloads, name)) as tar:
            names = tar.getnames()
            manifest = json.load(tar.extractfile(transfer.MANIFEST))
        self.assertEqual(names[0], transfer.MANIFEST)
        self.assertIn(f"{transfer.VOLUMES}/{VOLUME}.tar", names)
        self.assertIn("nextcloud/quadlet/secrets.env", names)
        self.assertEqual((manifest["app"], manifest["volumes"], manifest["live"]),
                         ("nextcloud", [VOLUME], False))

    def test_a_stopped_app_stays_stopped(self):
        self.nextcloud(running=False)
        rc, _ = self.export()
        self.assertEqual(rc, 0)
        self.assertEqual(self.verbs(), ["tar"])
        self.assertNotEqual(self.lab.system.units["nextcloud.service"]["ActiveState"], "active")

    def test_live_leaves_it_running_and_says_so(self):
        self.nextcloud()
        rc, out = self.export(live=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.verbs(), ["tar"])
        self.assertIn("packing while it runs", out)

    def test_what_will_not_stop_is_not_exported_and_comes_back(self):
        self.nextcloud()
        self.lab.system.stuck.add("nextcloud.service")
        rc, out = self.export()
        self.assertEqual(rc, 1)
        self.assertIn("nothing was exported (--live packs it anyway)", out)
        self.assertEqual(self.archives(), [])
        self.assertNotIn("tar", self.verbs())
        self.assertEqual(self.verbs()[-1], "start")

    def test_a_failed_tar_leaves_nothing_and_the_app_comes_back(self):
        self.nextcloud()
        self.lab.system.tar_fails = True
        rc, out = self.export()
        self.assertEqual(rc, 1)
        self.assertIn("packing failed, nothing exported", out)
        self.assertIn("Permission denied", out)
        self.assertEqual(os.listdir(self.downloads), [])
        self.assertEqual(self.verbs(), ["stop", "tar", "start"])
        self.assertFalse([n for n in os.listdir(self.lab.settings.root) if n.startswith(".redh")])

    def test_sending_runs_rsync_as_the_person_after_the_app_is_back(self):
        self.nextcloud()
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/run/agent"}):
            rc, out = self.export("new@server.internal:/srv/in")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.verbs(), ["stop", "tar", "start", "rsync"])
        rsync = self.lab.system.calls[-1]
        self.assertIn("SSH_AUTH_SOCK=/run/agent", rsync)
        self.assertIn(f"HOME={self.home}", rsync)
        self.assertEqual(rsync[-1], "new@server.internal:/srv/in/")
        self.assertIn("--chmod=F600", rsync)
        self.assertIn("sent to new@server.internal:/srv/in/", out)
        # No copy of the secrets stays behind.
        self.assertEqual(os.listdir(self.downloads), [])

    def test_sending_to_this_very_host_keeps_the_archive(self):
        """rsync to myself delivers into Downloads; deleting 'the local copy' must not hit it."""
        self.nextcloud()
        self.lab.system.rsync_copies_to = self.downloads
        rc, out = self.export("server.internal")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.archives()), 1)
        self.assertEqual(os.listdir(self.downloads), self.archives())

    def test_when_sending_fails_the_archive_stays_with_the_command(self):
        self.nextcloud()
        self.lab.system.rsync_rc = 255
        rc, out = self.export("server.internal")
        self.assertEqual(rc, 1)
        self.assertIn("could not send it (rsync: exit status 255)", out)
        [name] = self.archives()
        self.assertIn(os.path.join(self.downloads, name), out)
        self.assertFalse(os.path.exists(os.path.join(self.downloads, transfer.OUTGOING)))
        self.assertEqual(self.lab.system.units["nextcloud.service"]["ActiveState"], "active")

    def test_behind_sudo_rsync_runs_as_the_person_and_may_lack_the_agent(self):
        os.makedirs(os.path.join(self.downloads, transfer.OUTGOING))
        path = os.path.join(self.downloads, transfer.OUTGOING, "x.redh.tar.gz")
        with open(path, "w") as fh:
            fh.write("x")
        self.lab.system.rsync_rc = 255
        self.lab.host.euid = 0
        self.addCleanup(setattr, self.lab.host, "euid", os.geteuid())
        with mock.patch.dict(os.environ):
            os.environ.pop("SSH_AUTH_SOCK", None)
            self.assertFalse(transfer.send(self.lab.ctx, self.me, path, "alex@host:Downloads/"))
        self.assertEqual(self.lab.system.calls[-1][:4], ["runuser", "-u", "alex", "--"])
        self.assertIn("sudo drops SSH_AUTH_SOCK", self.lab.output())

    def test_what_stays_behind_is_named(self):
        self.lab.write("redhdb/quadlet/redhdb-app.container", """
            [Container]
            Image=localhost/redhdb-app:latest
            Network=other.network
            Volume=%h/redhdb/db:/db
            Volume=%h/elsewhere:/x
            Volume=/srv/media:/media:ro
            EnvironmentFile=%h/redhdb/redhdb.env
            """)
        rc = export_app(self.lab.ctx, "redhdb", dry_run=True)
        out = self.lab.output()
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.lab.settings.home}/elsewhere is outside", out)
        self.assertIn("/srv/media is outside", out)
        self.assertNotIn("redhdb/db is outside", out)
        self.assertNotIn("redhdb.env is outside", out)
        self.assertIn("Network=other.network belongs to another app", out)
        self.assertIn("localhost/redhdb-app:latest was built by hand; build it there too", out)
        self.assertNotIn("systemd/", out)

        self.lab.write("redhdb/systemd/clean.service", """
            [Service]
            Type=oneshot
            ExecStartPre=/usr/bin/podman image prune --force
            ExecStart=%h/redhdb/clean.sh
            ExecStartPost=-/usr/local/bin/notify-done
            """)
        export_app(self.lab.fresh(), "redhdb", dry_run=True)
        out = self.lab.output()
        self.assertIn("systemd/clean.service: /usr/local/bin/notify-done is outside", out)
        self.assertNotIn("/usr/bin/podman is outside", out)
        self.assertNotIn("clean.sh is outside", out)
        self.assertIn("dry run: nothing stopped, packed or sent", out)
        self.assertFalse(os.path.exists(self.downloads))


class ImportTest(TransferCase):
    def exported(self):
        """An archive of nextcloud in Downloads, and this host without nextcloud."""
        self.nextcloud(running=False)
        self.assertEqual(self.export()[0], 0)
        self.lab.system.objects.discard(("volume", VOLUME))
        self.lab.system.volumes.clear()
        shutil.rmtree(self.app)
        shutil.rmtree(os.path.join(self.lab.settings.quadlet_dir, "nextcloud"))
        self.lab.system.calls.clear()
        return os.path.join(self.downloads, self.archives()[0])

    def without_padding(self, path):
        """As GNU tar writes it: nothing after the gzip stream (Apple's tar pads with zeros)."""
        with open(path, "rb") as fh:
            data = gzip.decompress(fh.read())
        with open(path, "wb") as fh:
            fh.write(gzip.compress(data))

    def import_(self, *args, **kwargs):
        rc = import_app(self.lab.fresh(), *args, **kwargs)
        return rc, self.lab.output()

    def test_round_trip(self):
        path = self.exported()
        rc, out = self.import_()
        self.assertEqual(rc, 0, out)
        self.assertEqual(mode(self.app), 0o700)
        self.assertEqual(mode(os.path.join(self.app, "quadlet", "secrets.env")), 0o600)
        self.assertEqual(mode(os.path.join(self.app, "data", "index.html")), 0o640)
        self.assertEqual(os.readlink(os.path.join(self.app, "data", "link")),
                         "/var/www/html/index.html")
        self.assertEqual(self.lab.system.volumes[VOLUME], "the database")
        self.assertFalse([n for n in os.listdir(self.lab.settings.root) if n.startswith(".redh")])
        self.assertIn(f"rm {path}", out)
        self.assertIn("then: redh check nextcloud && redh install nextcloud", out)
        # It extracts in podman's user namespace, never as root.
        tar = next(c for c in self.lab.system.calls if c[2:3] == ["tar"])
        self.assertEqual(tar[:2], ["podman", "unshare"])
        self.assertIn("--numeric-owner", tar)
        # What came back is an app like any other.
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())

    def test_it_replaces_nothing(self):
        path = self.exported()
        self.lab.write("nextcloud/mine.txt", "keep me\n")
        with self.assertRaises(SystemExit) as cm:
            self.import_(path)
        self.assertIn("already exists", str(cm.exception.code))
        self.assertEqual(os.listdir(self.app), ["mine.txt"])

        shutil.rmtree(self.app)
        self.lab.system.objects.add(("volume", VOLUME))
        with self.assertRaises(SystemExit) as cm:
            self.import_(path)
        self.assertIn(f"{VOLUME} already exists", str(cm.exception.code))
        self.assertFalse(os.path.exists(self.app))

    def test_dry_run_only_checks(self):
        self.exported()
        rc, out = self.import_(dry_run=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("all below nextcloud/", out)
        self.assertIn("dry run: nothing unpacked", out)
        self.assertFalse(os.path.exists(self.app))
        self.assertEqual(self.lab.system.volumes, {})

    def test_which_archive(self):
        path = self.exported()
        newer = path.replace(os.path.basename(path), "nextcloud-20991231-235959.redh.tar.gz")
        self.write_archive(newer, "nextcloud", [("nextcloud", "dir")], created="2099-12-31T23:59:59")
        other = os.path.join(self.downloads, "blog-20260101-000000.redh.tar.gz")
        self.write_archive(other, "blog", [("blog", "dir")])
        with open(os.path.join(self.downloads, "something-else.tar.gz"), "wb") as fh:
            fh.write(b"not ours")

        with self.assertRaises(SystemExit) as cm:
            self.import_()
        self.assertIn("archives of 2 apps", str(cm.exception.code))
        self.assertIn("blog, nextcloud", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            self.import_("wiki")
        self.assertIn("no archive of wiki", str(cm.exception.code))

        rc, out = self.import_("nextcloud", dry_run=True)   # the newest of that app
        self.assertEqual(rc, 0, out)
        self.assertIn(os.path.basename(newer), out)
        rc, out = self.import_(path, dry_run=True)          # exactly this one
        self.assertIn(os.path.basename(path), out)

        with mock.patch.object(transfer.pwd, "getpwuid",
                               lambda uid: mock.Mock(pw_dir=self.home)):
            self.assertEqual(transfer.archive_names("n"), ["nextcloud"])
            self.assertEqual(transfer.archive_names(""), ["blog", "nextcloud"])

    def test_nothing_there(self):
        with self.assertRaises(SystemExit) as cm:
            self.import_()
        self.assertIn(f"no archive in {self.downloads}", str(cm.exception.code))
        junk = os.path.join(self.home, "x.redh.tar.gz")
        with open(junk, "wb") as fh:
            fh.write(b"not an archive")
        with self.assertRaises(SystemExit) as cm:
            self.import_(junk)
        self.assertIn("is not an archive from redh export", str(cm.exception.code))

    def test_a_cut_off_archive_imports_nothing(self):
        path = self.exported()
        self.without_padding(path)
        size = os.path.getsize(path)
        for keep in (size - 4, size - 40, size // 2):
            with open(path, "r+b") as fh:
                fh.truncate(keep)
            rc, out = self.import_(path)
            self.assertEqual(rc, 1, out)
            self.assertIn("it is damaged or incomplete", out)
            self.assertFalse(os.path.exists(self.app))
            self.assertEqual(self.lab.system.volumes, {})

    def test_a_damaged_archive_imports_nothing(self):
        path = self.exported()
        self.without_padding(path)
        with open(path, "r+b") as fh:
            fh.seek(-8, os.SEEK_END)            # gzip's checksum
            crc = fh.read(1)
            fh.seek(-8, os.SEEK_END)
            fh.write(bytes([crc[0] ^ 0xff]))
        rc, out = self.import_(path)
        self.assertEqual(rc, 1, out)
        self.assertIn("it is damaged or incomplete", out)
        self.assertFalse(os.path.exists(self.app))

    # -- archives that want more than they should -------------------------------

    def write_archive(self, path, app, members, volumes=(), created="2026-01-01T00:00:00"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        manifest = json.dumps({"format": transfer.FORMAT, "app": app, "created": created,
                               "host": "elsewhere", "root": self.lab.settings.root,
                               "volumes": list(volumes)}).encode()
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo(transfer.MANIFEST)
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
            for name, kind, *rest in members:
                info = tarfile.TarInfo(name)
                info.type = {"dir": tarfile.DIRTYPE, "file": tarfile.REGTYPE,
                             "sym": tarfile.SYMTYPE, "link": tarfile.LNKTYPE,
                             "chr": tarfile.CHRTYPE}[kind]
                if kind in ("sym", "link"):
                    info.linkname = rest[0]
                if kind == "dir":
                    info.mode = 0o700
                tar.addfile(info, io.BytesIO(b"") if kind == "file" else None)
        return path

    def test_archives_that_reach_too_far(self):
        top = ("evil", "dir")
        cases = {
            "../../etc/cron.d/x": [top, ("../../etc/cron.d/x", "file")],
            "/etc/passwd": [top, ("/etc/passwd", "file")],
            ".config/containers/systemd/x.container":
                [top, (".config/containers/systemd/x.container", "file")],
            "other/x": [top, ("other/x", "file")],
            "behind a symlink": [top, ("evil/out", "sym", "/etc"), ("evil/out/cron.d", "file")],
            "behind a hard link to a symlink":
                [top, ("evil/data", "dir"), ("evil/data/l", "sym", "/etc"),
                 ("evil/data/h", "link", "evil/data/l"), ("evil/data/h/x", "file")],
            "device": [top, ("evil/data", "dir"), ("evil/data/sda", "chr")],
            "hard link out": [top, ("evil/data", "dir"), ("evil/data/h", "link", "etc/shadow")],
            "quadlet symlink": [top, ("evil/quadlet", "dir"),
                                ("evil/quadlet/x.env", "sym", "/etc/shadow")],
            "quadlet symlink up": [top, ("evil/caddy", "dir"),
                                   ("evil/caddy/Caddyfile", "sym", "../../../etc/shadow")],
            "quadlet is a symlink": [top, ("evil/quadlet", "sym", "/etc")],
            "stray at the top": [top, ("evil/x.container", "sym", "/etc/shadow")],
            "app is a symlink": [("evil", "sym", "/etc")],
            ".redh symlink": [top, (".redh/volumes", "sym", "/etc")],
        }
        for what, members in cases.items():
            with self.subTest(what):
                path = self.write_archive(os.path.join(self.home, "evil.redh.tar.gz"), "evil", members)
                self.lab.system.calls.clear()
                rc, out = self.import_(path)
                self.assertEqual(rc, 1, out)
                self.assertIn("nothing imported", out)
                self.assertFalse(os.path.lexists(os.path.join(self.lab.settings.root, "evil")))
                self.assertFalse([c for c in self.lab.system.calls if c[:2] == ["podman", "unshare"]])

    def test_a_link_in_the_data_may_mean_something_inside_the_container(self):
        path = self.write_archive(
            os.path.join(self.home, "fine.redh.tar.gz"), "fine",
            [("fine", "dir"), ("fine/data", "dir"), ("fine/data/current", "sym", "/var/lib/app/v2"),
             ("fine/quadlet", "dir"), ("fine/quadlet/own.env", "file"),
             ("fine/quadlet/same.env", "sym", "own.env")])
        rc, out = self.import_(path)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.islink(os.path.join(self.lab.settings.root, "fine/data/current")))

    def test_a_manifest_that_lies(self):
        for manifest_app in ("../x", ".config", ""):
            path = self.write_archive(os.path.join(self.home, "liar.redh.tar.gz"), manifest_app, [])
            with self.assertRaises(SystemExit) as cm:
                self.import_(path)
            self.assertIn("its manifest makes no sense", str(cm.exception.code))
        path = self.write_archive(os.path.join(self.home, "liar.redh.tar.gz"), "ok",
                                  [("ok", "dir")], volumes=["../../x"])
        with self.assertRaises(SystemExit) as cm:
            self.import_(path)
        self.assertIn("its manifest makes no sense", str(cm.exception.code))
        path = self.write_archive(os.path.join(self.home, "liar.redh.tar.gz"), "ok",
                                  [("ok", "dir")], volumes=["missing"])
        rc, out = self.import_(path)
        self.assertEqual(rc, 1)
        self.assertIn("volume missing is not in it", out)

    def test_names_of_the_old_host_are_pointed_out(self):
        path = self.write_archive(
            os.path.join(self.home, "site.redh.tar.gz"), "site", [("site", "dir")])
        self.assertEqual(self.import_(path)[0], 0)
        self.lab.write("site/caddy/Caddyfile", "site.elsewhere.internal {\n\trespond hi\n}\n")
        notes = transfer.after_import(self.lab.fresh(), "site", {"host": "elsewhere", "root": "/srv/apps"})
        self.assertIn("still answers to site.elsewhere.internal", "\n".join(notes))
        self.assertIn("it came from /srv/apps", "\n".join(notes))

        # A snippet that is a symlink out of the app is not read, as root.
        outside = self.lab.write(os.path.join(self.lab.tmp, "etc", "secret"), "elsewhere.internal {\n}\n")
        snippet = os.path.join(self.lab.settings.root, "site", "caddy", "Caddyfile")
        os.unlink(snippet)
        os.symlink(outside, snippet)
        notes = transfer.after_import(self.lab.fresh(), "site", {"host": "elsewhere", "root": self.lab.settings.root})
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
