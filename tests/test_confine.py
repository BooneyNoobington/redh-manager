import builtins
import errno
import os
import shutil
import stat
import unittest
from contextlib import contextmanager
from unittest import mock

from redh import caddy, confine, envfiles, perms, sites
from redh.app import load_units, walk_files
from redh.checks import (PLACEHOLDER, Findings, Survey, _caddy, check_app, check_host, placeholders,
                         preflight, run_check, stage_for_generator)
from redh.confine import Escape
from redh.init import ports_taken, site_domain
from redh.install import copy_file, install, uninstall
from redh.plan import make_plan, same_content
from redh.running import mount_sources

from fakes import Lab, nextcloud

MARK = "$6$only-root-may-read-this"
SECRET = f"root:{MARK}:19000:0:99999:7:::\n"


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def failed(findings, topic=None):
    return [f.text for f in findings if f.level == "FAIL" and topic in (None, f.topic)]


class Planted(unittest.TestCase):
    """A lab with a file only root may read, outside everything the podman user owns."""

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.s = self.lab.settings
        self.home = self.s.home
        self.secret = self.lab.write(os.path.join(self.lab.tmp, "etc", "shadow"), SECRET)
        self.outside = os.path.dirname(self.secret)

    def path(self, rel):
        return os.path.join(self.home, rel)

    def link(self, rel, target):
        path = self.path(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return path

    def swap_for_link(self, path, target=None):
        os.unlink(path)
        os.symlink(target or self.secret, path)

    @contextmanager
    def never_opened(self, *paths):
        """Fail if anything in this process opens one of these files, by whatever path."""
        files = {(st.st_dev, st.st_ino) for st in map(os.stat, paths or [self.secret])}
        real_os_open, real_open = os.open, builtins.open
        opened = []

        def note(name, fd):
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) in files:
                opened.append(name)

        def spy_os_open(name, *args, **kwargs):
            fd = real_os_open(name, *args, **kwargs)
            note(name, fd)
            return fd

        def spy_open(name, *args, **kwargs):
            fh = real_open(name, *args, **kwargs)
            note(name, fh.fileno())
            return fh

        with mock.patch("os.open", spy_os_open), mock.patch("builtins.open", spy_open):
            yield
        self.assertEqual(opened, [])

    def leaks(self):
        """Files in the podman user's home or Caddy's directory that hold the secret."""
        found = []
        for top in (self.home, self.s.caddy_dir):
            for dirpath, _, names in os.walk(top):
                for name in names:
                    path = os.path.join(dirpath, name)
                    if not os.path.islink(path):
                        with open(path, errors="replace") as fh:
                            if MARK in fh.read():
                                found.append(path)
        return found


class ReadingTest(Planted):
    def setUp(self):
        super().setUp()
        self.app = self.path("web")
        self.lab.write("web/quadlet/real.env", "X=1\n")

    def read(self, rel):
        return confine.read_text(self.s, os.path.join(self.app, rel))

    def assertEscapes(self, rel, link, target, top=None):
        with self.assertRaises(Escape) as caught:
            self.read(rel)
        self.assertEqual(caught.exception.link, link)
        self.assertEqual(caught.exception.target, target)
        self.assertEqual(caught.exception.top, top or self.app)

    def test_plain_files_read_as_ever(self):
        self.assertEqual(self.read("quadlet/real.env"), "X=1\n")
        self.assertEqual(confine.stat_of(self.s, self.path("web/quadlet/real.env")).st_size, 4)

    def test_links_that_stay_in_the_app_are_followed(self):
        real = os.path.join(self.app, "quadlet", "real.env")
        self.link("web/quadlet/sibling.env", "real.env")
        self.link("web/caddy/up.env", "../quadlet/real.env")
        self.link("web/quadlet/absolute.env", real)
        self.link("web/quadlet/by-real-path.env", os.path.realpath(real))
        self.link("web/quadlet/chain.env", "sibling.env")
        self.link("web/quadlet/out-and-back.env", "../../web/quadlet/real.env")
        self.link("web/config", "quadlet")
        for rel in ("quadlet/sibling.env", "caddy/up.env", "quadlet/absolute.env",
                    "quadlet/by-real-path.env", "quadlet/chain.env",
                    "quadlet/out-and-back.env", "config/real.env"):
            self.assertEqual(self.read(rel), "X=1\n", rel)

    def test_a_link_out_names_itself_and_where_it_points(self):
        absolute = self.link("web/quadlet/x.env", self.secret)
        self.assertEscapes("quadlet/x.env", absolute, self.secret)
        relative = os.path.relpath(self.secret, os.path.join(self.app, "quadlet"))
        climbing = self.link("web/quadlet/y.env", relative)
        self.assertEscapes("quadlet/y.env", climbing, relative)

    def test_the_message_reads_well_where_oserror_is_handled(self):
        link = self.link("web/quadlet/x.env", self.secret)
        with self.assertRaises(OSError) as caught:
            self.read("quadlet/x.env")
        self.assertEqual(caught.exception.strerror, f"symlink to {self.secret}, outside {self.app}")
        self.assertEqual(str(caught.exception), f"{link} points to {self.secret}, outside {self.app}")

    def test_a_directory_on_the_way_counts_too(self):
        link = self.link("web/quadlet/sub", self.outside)
        self.assertEscapes("quadlet/sub/shadow", link, self.outside)

    def test_the_last_link_of_a_chain_is_the_one_named(self):
        self.link("web/quadlet/a.env", "b.env")
        last = self.link("web/quadlet/b.env", self.secret)
        self.assertEscapes("quadlet/a.env", last, self.secret)

    def test_another_app_is_outside_too(self):
        other = self.lab.write("other/secret.env", "X=1\n")
        link = self.link("web/quadlet/x.env", other)
        self.assertEscapes("quadlet/x.env", link, other)
        climbing = self.link("web/quadlet/y.env", "../../other/secret.env")
        self.assertEscapes("quadlet/y.env", climbing, "../../other/secret.env")

    def test_a_link_to_the_directory_above_the_app(self):
        link = self.link("web/quadlet/up", "../..")
        self.assertEscapes("quadlet/up/other", link, "../..")

    def test_a_loop_is_an_error_and_no_escape(self):
        self.link("web/quadlet/a", "b")
        self.link("web/quadlet/b", "a")
        with self.assertRaises(OSError) as caught:
            self.read("quadlet/a")
        self.assertEqual(caught.exception.errno, errno.ELOOP)
        self.assertNotIsInstance(caught.exception, Escape)

    def test_only_regular_files_are_read(self):
        os.mkfifo(self.path("web/quadlet/pipe"))        # would hang a plain open()
        self.link("web/quadlet/to-pipe", "pipe")
        for rel in ("quadlet/pipe", "quadlet/to-pipe", "quadlet"):
            with self.assertRaises(OSError) as caught:
                self.read(rel)
            self.assertEqual(caught.exception.strerror, "not a regular file", rel)

    def test_a_file_swapped_right_before_the_open(self):
        path = self.path("web/quadlet/real.env")
        real_open = os.open

        def swap_then_open(name, *args, **kwargs):
            if name == "real.env":
                self.swap_for_link(path)
            return real_open(name, *args, **kwargs)

        with mock.patch("os.open", swap_then_open):
            self.assertEscapes("quadlet/real.env", path, self.secret)

    def test_a_directory_swapped_right_before_it_is_entered(self):
        self.lab.write("web/quadlet/sub/shadow", "harmless\n")
        sub = self.path("web/quadlet/sub")
        moved = self.path("web/moved")
        real_open = os.open

        def swap_then_open(name, *args, **kwargs):
            if name == "sub":
                os.rename(sub, moved)
                os.symlink(self.outside, sub)
            return real_open(name, *args, **kwargs)

        with mock.patch("os.open", swap_then_open):
            self.assertEscapes("quadlet/sub/shadow", sub, self.outside)

    def test_a_directory_swapped_once_it_is_passed_changes_nothing(self):
        # What O_NOFOLLOW on a whole path cannot do: the swap is above the last part.
        self.lab.write("web/quadlet/sub/shadow", "harmless\n")
        sub = self.path("web/quadlet/sub")
        moved = self.path("web/moved")
        real_open = os.open

        def swap_then_open(name, *args, **kwargs):
            if name == "shadow":
                os.rename(sub, moved)
                os.symlink(self.outside, sub)
            return real_open(name, *args, **kwargs)

        with mock.patch("os.open", swap_then_open):
            self.assertEqual(self.read("quadlet/sub/shadow"), "harmless\n")

    def test_how_linux_reports_a_symlink_opened_as_a_directory(self):
        # O_DIRECTORY | O_NOFOLLOW on a symlink: ENOTDIR on Linux, ELOOP elsewhere.
        link = self.link("web/quadlet/sub", self.outside)
        real_open = os.open

        def like_linux(name, flags, *args, **kwargs):
            if name == "sub" and flags & os.O_DIRECTORY:
                raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), name)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch("os.open", like_linux):
            self.assertEscapes("quadlet/sub/shadow", link, self.outside)

    def test_a_file_on_the_way_is_no_link(self):
        with self.assertRaises(NotADirectoryError):
            self.read("quadlet/real.env/x")

    def test_the_app_directory_as_a_link_out_of_the_apps_root(self):
        link = self.link("evil", self.outside)
        with self.assertRaises(Escape) as caught:
            confine.read_text(self.s, os.path.join(link, "shadow"))
        self.assertEqual((caught.exception.link, caught.exception.top), (link, self.home))
        with self.assertRaises(Escape):
            confine.listdir(self.s, link)

    def test_the_app_directory_as_a_link_within_the_apps_root(self):
        self.link("web2", "web")
        self.assertEqual(confine.read_text(self.s, self.path("web2/quadlet/real.env")), "X=1\n")

    def test_a_link_above_the_quadlet_directory(self):
        config = self.path(".config")
        os.symlink(self.outside, config)
        with self.assertRaises(Escape) as caught:
            confine.read_text(self.s, os.path.join(self.s.quadlet_dir, "web", "x.env"))
        self.assertEqual(caught.exception.link, config)

    def test_the_quadlet_directory_may_lean_on_the_home(self):
        self.lab.write(os.path.join(self.s.quadlet_dir, "keep"), "")
        by_hand = os.path.join(self.s.quadlet_dir, "web.env")
        os.symlink(self.path("web/quadlet/real.env"), by_hand)
        self.assertEqual(confine.read_text(self.s, by_hand), "X=1\n")
        other = os.path.join(self.s.quadlet_dir, "x.env")
        os.symlink(self.secret, other)
        with self.assertRaises(Escape) as caught:
            confine.read_text(self.s, other)
        self.assertEqual(caught.exception.top, self.home)

    def test_what_the_podman_user_has_no_say_over_opens_as_usual(self):
        beside = os.path.join(self.outside, "beside")
        os.symlink(self.secret, beside)
        self.assertEqual(confine.read_text(self.s, beside), SECRET)
        self.assertIsNone(confine.top_of(self.s, beside))

    def test_tops(self):
        self.assertEqual(confine.top_of(self.s, self.path("web/quadlet/x")), self.path("web"))
        self.assertEqual(confine.top_of(self.s, self.path("web")), self.home)
        self.assertEqual(confine.top_of(self.s, os.path.join(self.s.quadlet_dir, "web/x")), self.home)
        self.s.root = self.path("apps")
        self.assertEqual(confine.top_of(self.s, self.path("apps/web/x")), self.path("apps/web"))
        self.assertIsNone(confine.top_of(self.s, self.path("elsewhere/x")))

    def test_an_apps_root_below_the_home_is_walked_to_from_there(self):
        self.s.root = self.path("apps")
        os.symlink(os.path.dirname(self.outside), self.s.root)   # the podman user owns the home
        with self.assertRaises(Escape) as caught:
            confine.read_text(self.s, os.path.join(self.s.root, "etc", "shadow"))
        self.assertEqual(caught.exception.link, self.s.root)

    def test_a_nul_in_what_a_unit_file_names_is_just_not_there(self):
        for path in (self.path("web/a\0b"), "/etc/a\0b"):
            with self.assertRaises(FileNotFoundError):
                confine.stat_of(self.s, path)

    def test_walking_finds_files_and_notes_links_out(self):
        self.link("web/quadlet/out.env", self.secret)
        out_dir = self.link("web/quadlet/sub", self.outside)
        self.link("web/quadlet/inside.env", "real.env")
        self.link("web/quadlet/dangling.env", "gone")
        self.link("web/quadlet/.hidden", self.secret)
        os.mkfifo(self.path("web/quadlet/pipe"))
        escapes = []
        files = walk_files(self.path("web/quadlet"), self.s, escapes)
        self.assertEqual(sorted(files), ["inside.env", "real.env"])
        self.assertEqual(sorted(e.link for e in escapes),
                         [self.path("web/quadlet/out.env"), out_dir])

    def test_walking_a_link_out_notes_the_link_and_finds_nothing(self):
        link = self.link("web/caddy", self.outside)
        escapes = []
        self.assertEqual(walk_files(link, self.s, escapes), {})
        self.assertEqual([e.link for e in escapes], [link])


class InstallTest(Planted):
    def setUp(self):
        super().setUp()
        nextcloud(self.lab)
        self.app = self.path("nextcloud")
        self.qdir = self.s.quadlet_dir

    def assertRefused(self, link, target=None, **options):
        with self.never_opened():
            rc = install(self.lab.fresh(), ["nextcloud"], **options)
        out = self.lab.output()
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL  symlink", out)
        self.assertIn(f"{link} points to {target or self.secret}, outside {self.app}", out)
        self.assertIn("nothing installed", out)
        self.assertNotIn("--force", out.split("nothing installed")[1])
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "nextcloud")))
        self.assertFalse(os.path.exists(self.s.caddy_dir))
        self.assertEqual(self.leaks(), [])

    def test_an_env_file(self):
        self.assertRefused(self.link("nextcloud/quadlet/x.env", self.secret))

    def test_force_does_not_overrule_it(self):
        self.assertRefused(self.link("nextcloud/quadlet/x.env", self.secret), force=True)

    def test_a_dry_run_says_so_too(self):
        self.assertRefused(self.link("nextcloud/quadlet/x.env", self.secret), dry_run=True)

    def test_a_relative_link(self):
        target = os.path.relpath(self.secret, os.path.join(self.app, "quadlet"))
        self.assertRefused(self.link("nextcloud/quadlet/x.env", target), target)

    def test_a_unit_file(self):
        self.assertRefused(self.link("nextcloud/quadlet/x.container", self.secret))

    def test_a_drop_in(self):
        self.assertRefused(self.link("nextcloud/quadlet/nextcloud.container.d/x.conf", self.secret))

    def test_a_drop_in_directory(self):
        self.lab.write(os.path.join(self.outside, "x.conf"), SECRET)
        self.assertRefused(self.link("nextcloud/quadlet/nextcloud.container.d", self.outside),
                           self.outside)

    def test_a_subdirectory(self):
        self.assertRefused(self.link("nextcloud/quadlet/sub", self.outside), self.outside)

    def test_a_caddy_snippet(self):
        self.assertRefused(self.link("nextcloud/caddy/x.caddy", self.secret))

    def test_the_quadlet_directory_itself(self):
        os.rename(self.path("nextcloud/quadlet"), self.path("nextcloud/was-quadlet"))
        self.assertRefused(self.link("nextcloud/quadlet", self.outside), self.outside)

    def test_the_caddy_directory_itself(self):
        os.rename(self.path("nextcloud/caddy"), self.path("nextcloud/was-caddy"))
        self.assertRefused(self.link("nextcloud/caddy", self.outside), self.outside)

    def test_another_apps_file(self):
        other = self.lab.write("other/secret.env", "X=1\n")
        self.assertRefused(self.link("nextcloud/quadlet/x.env", other), other)

    def test_the_top_level_layout(self):
        self.lab.write("legacy/legacy.container", "[Container]\nImage=docker.io/library/redis:7\n")
        for name in ("evil.container", "evil.caddy", "legacy.container.d"):
            self.link(f"legacy/{name}", self.outside if name.endswith(".d") else self.secret)
        with self.never_opened():
            rc = install(self.lab.fresh(), ["legacy"], force=True)
        out = self.lab.output()
        self.assertEqual(rc, 1, out)
        for name in ("evil.container", "evil.caddy", "legacy.container.d"):
            self.assertIn(f"{self.path('legacy/' + name)} points to ", out)
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "legacy")))
        self.assertEqual(self.leaks(), [])

    def test_an_app_directory_that_is_a_link(self):
        self.lab.write(os.path.join(self.outside, "quadlet", "x.container"), f"[Container]\n# {MARK}\n")
        link = self.link("evil", self.outside)
        with self.never_opened(self.secret, os.path.join(self.outside, "quadlet", "x.container")):
            rc = install(self.lab.fresh(), ["evil"], force=True)
        out = self.lab.output()
        self.assertEqual(rc, 1, out)
        self.assertIn(f"{link} points to {self.outside}, outside {self.home}", out)
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "evil")))
        self.assertEqual(self.leaks(), [])

    def test_links_inside_the_app_are_installed_as_plain_files(self):
        self.lab.write("nextcloud/config/shared.env", "X=1\n", mode=0o640)
        self.link("nextcloud/quadlet/relative.env", "../config/shared.env")
        self.link("nextcloud/quadlet/absolute.env", self.path("nextcloud/config/shared.env"))
        self.lab.write("nextcloud/shared/site.caddy", "site.example.org {\n}\n")
        self.link("nextcloud/caddy/site.caddy", "../shared/site.caddy")
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        for name in ("relative.env", "absolute.env"):
            installed = os.path.join(self.qdir, "nextcloud", name)
            self.assertFalse(os.path.islink(installed))
            self.assertEqual(mode(installed), 0o640)
            with open(installed) as fh:
                self.assertEqual(fh.read(), "X=1\n")
        with open(os.path.join(self.s.caddy_dir, "nextcloud.site.caddy")) as fh:
            self.assertIn("site.example.org", fh.read())
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0)
        self.assertIn("already up to date", self.lab.output())

    def test_a_link_elsewhere_in_the_app_does_not_matter(self):
        self.link("nextcloud/storage", self.outside)        # a data directory on a bigger disk
        self.link("nextcloud/data/current", self.secret)    # means something in the container
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        self.assertEqual(self.leaks(), [])

    def test_a_link_swapped_in_after_the_checks(self):
        path = self.lab.write("nextcloud/quadlet/x.env", "X=1\n", mode=0o600)

        def preflight_then_swap(*args, **kwargs):
            found = preflight(*args, **kwargs)
            self.swap_for_link(path)
            return found

        with mock.patch("redh.install.preflight", preflight_then_swap), self.never_opened():
            rc = install(self.lab.fresh(), ["nextcloud"])
        out = self.lab.output()
        self.assertEqual(rc, 1, out)
        self.assertIn("could not write everything", out)
        self.assertIn(f"x.env: symlink to {self.secret}, outside {self.app}", out)
        self.assertFalse(os.path.exists(os.path.join(self.qdir, "nextcloud", "x.env")))
        self.assertEqual(self.leaks(), [])

    def test_a_snippet_swapped_in_after_the_plan(self):
        source = self.path("nextcloud/caddy/nextcloud.caddy")
        ctx = self.lab.fresh()
        site = sites.plan(ctx, "nextcloud")
        survey = Survey(ctx)
        self.swap_for_link(source)
        with self.never_opened():
            self.assertFalse(sites.apply(ctx, survey, site))
        self.assertIn(f"{source} points to {self.secret}", self.lab.output())
        self.assertFalse(os.path.exists(self.s.caddy_dir))
        self.assertEqual(self.lab.system.reloads, [])

    def test_a_snippet_swapped_in_before_the_plan_counts_as_changed(self):
        source = self.path("nextcloud/caddy/nextcloud.caddy")
        self.assertEqual(install(self.lab.ctx, ["nextcloud"]), 0, self.lab.output())
        ctx = self.lab.fresh()
        ctx.sources("nextcloud")
        self.swap_for_link(source)
        with self.never_opened():
            site = sites.plan(ctx, "nextcloud")
        self.assertEqual((site.same, site.change), ([], ["nextcloud.caddy"]))

    def test_copying_does_not_read_through_a_link(self):
        link = self.link("nextcloud/quadlet/x.env", self.secret)
        dest = self.path("copy.env")
        with self.assertRaises(Escape), self.never_opened():
            copy_file(self.lab.ctx, link, self.s.home, "copy.env")
        self.assertFalse(os.path.lexists(dest))
        self.assertEqual(self.leaks(), [])

    def test_comparing_does_not_read_through_a_link(self):
        link = self.link("nextcloud/quadlet/x.env", self.secret)
        same = self.lab.write("same", SECRET)
        with self.never_opened():
            self.assertFalse(same_content(self.s, link, same))
            self.assertFalse(same_content(self.s, same, link))

    def test_copying_does_not_write_through_a_planted_temporary_file(self):
        victim = self.lab.write(os.path.join(self.outside, "victim"), "untouched\n", mode=0o644)
        temp = os.path.join(self.qdir, "nextcloud", ".nextcloud.network.redh-tmp")
        os.makedirs(os.path.dirname(temp))
        os.symlink(victim, temp)
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        with open(victim) as fh:
            self.assertEqual(fh.read(), "untouched\n")
        self.assertEqual(mode(victim), 0o644)
        self.assertFalse(os.path.lexists(temp))
        self.assertTrue(os.path.isfile(os.path.join(self.qdir, "nextcloud", "nextcloud.network")))

    def test_a_hand_made_link_in_the_quadlet_directory_is_an_old_copy_still(self):
        source = self.path("nextcloud/quadlet/nextcloud.network")
        by_hand = os.path.join(self.qdir, "nextcloud.network")
        os.makedirs(self.qdir)
        os.symlink(source, by_hand)
        plan = make_plan(self.lab.fresh(), "nextcloud")
        self.assertEqual([(p, same) for p, _, same in plan.legacy], [(by_hand, True)])
        self.assertEqual(install(self.lab.fresh(), ["nextcloud"]), 0, self.lab.output())
        self.assertFalse(os.path.lexists(by_hand))
        self.assertTrue(os.path.isfile(source))


class CheckTest(Planted):
    def setUp(self):
        super().setUp()
        nextcloud(self.lab)
        self.app = self.path("nextcloud")
        self.qdir = self.s.quadlet_dir

    def check(self, app="nextcloud"):
        ctx = self.lab.fresh()
        return check_app(ctx, app, Survey(ctx))

    def test_a_link_out_of_quadlet_or_caddy_fails(self):
        env = self.link("nextcloud/quadlet/x.env", self.secret)
        snippet = self.link("nextcloud/caddy/x.caddy", self.secret)
        findings = self.check()
        self.assertEqual(failed(findings, "symlink"),
                         [f"{env} points to {self.secret}, outside {self.app}",
                          f"{snippet} points to {self.secret}, outside {self.app}"])
        [detail] = [f.details for f in findings if f.topic == "symlink"][0]
        self.assertIn("redh does not follow it", detail)

    def test_the_exit_status_says_so(self):
        self.link("nextcloud/quadlet/x.env", self.secret)
        self.assertEqual(run_check(self.lab.fresh(), ["nextcloud"]), 1)
        self.assertIn("FAIL  symlink", self.lab.output())

    def test_links_inside_the_app_are_fine(self):
        self.lab.write("nextcloud/config/shared.env", "X=1\n", mode=0o600)
        self.link("nextcloud/quadlet/shared.env", "../config/shared.env")
        self.assertEqual(failed(self.check(), "symlink"), [])

    def test_a_link_elsewhere_in_the_app_does_not_fail(self):
        self.link("nextcloud/storage", self.outside)
        self.link("nextcloud/data/current", self.secret)
        self.assertEqual(failed(self.check()), [])

    def test_an_app_directory_that_is_a_link_gets_that_and_nothing_else(self):
        link = self.link("evil", self.outside)
        findings = self.check("evil")
        self.assertEqual([(f.level, f.topic, f.text) for f in findings],
                         [("FAIL", "symlink", f"{link} points to {self.outside}, outside {self.home}")])

    def test_a_link_out_of_the_quadlet_directory_fails_with_the_host(self):
        link = os.path.join(self.qdir, "zz.container")
        os.makedirs(self.qdir)
        os.symlink(self.secret, link)
        ctx = self.lab.fresh()
        self.assertEqual(failed(check_host(ctx, Survey(ctx)), "symlink"),
                         [f"{link} points to {self.secret}, outside {self.home}"])

    def test_nothing_reads_what_a_link_points_to(self):
        marked = self.lab.write(os.path.join(self.outside, "todo"), f"# {PLACEHOLDER}: {MARK}\n")
        unit = self.lab.write(os.path.join(self.outside, "u"), f"[Container]\nImage={MARK}\n")
        self.link("nextcloud/quadlet/x.env", marked)
        self.link("nextcloud/quadlet/x.container", unit)
        self.link("nextcloud/caddy/x.caddy", marked)
        with self.never_opened(marked, unit):
            ctx = self.lab.fresh()
            src = ctx.sources("nextcloud")
            self.assertEqual(placeholders(ctx, src), [])
            self.assertNotIn("x.container", [u.filename for u in src.units])
            self.assertEqual(src.unreadable, [])
            run_check(ctx, ["nextcloud"])
        self.assertNotIn(MARK, self.lab.output())

    def test_a_drop_in_that_turned_into_a_link_makes_the_unit_unreadable(self):
        self.lab.write("nextcloud/quadlet/nextcloud.container.d/x.conf", "[Container]\n")
        conf = self.path("nextcloud/quadlet/nextcloud.container.d/x.conf")
        files = {"nextcloud.container": self.path("nextcloud/quadlet/nextcloud.container")}
        self.swap_for_link(conf)
        units, unreadable = load_units(self.path("nextcloud/quadlet"), files, confine.reader(self.s))
        self.assertEqual(units, [])
        self.assertEqual(unreadable, [("nextcloud.container",
                                       f"symlink to {self.secret}, outside {self.app}")])

    def stage_contents(self, ctx, plan):
        """What the generator's stage holds, which goes to the podman user."""
        with self.never_opened():
            stage, new, _ = stage_for_generator(ctx, plan)
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        files = {}
        for dirpath, _, names in os.walk(stage):
            for name in names:
                with open(os.path.join(dirpath, name)) as fh:
                    files[os.path.relpath(os.path.join(dirpath, name), new)] = fh.read()
        return files

    def test_the_generators_stage_stays_clean_of_links(self):
        self.link("nextcloud/quadlet/x.container", self.secret)
        self.link("nextcloud/quadlet/x.container.d/y.conf", self.secret)
        os.makedirs(self.qdir)
        os.symlink(self.secret, os.path.join(self.qdir, "zz.container"))
        ctx = self.lab.fresh()
        files = self.stage_contents(ctx, make_plan(ctx, "nextcloud"))
        self.assertIn("nextcloud.network", files)
        self.assertNotIn(MARK, "".join(files.values()))

    def test_the_generators_stage_stays_clean_of_files_swapped_since(self):
        unit = self.lab.write("nextcloud/quadlet/x.container", "[Container]\nImage=docker.io/x/y:1\n")
        loose = self.lab.write(os.path.join(self.qdir, "zz.container"), "[Container]\n")
        ctx = self.lab.fresh()
        plan = make_plan(ctx, "nextcloud")
        ctx.tree()
        self.swap_for_link(unit)
        self.swap_for_link(loose)
        files = self.stage_contents(ctx, plan)
        self.assertIn("nextcloud.network", files)
        self.assertNotIn("x.container", files)
        self.assertNotIn(MARK, "".join(files.values()))

    def test_the_stage_goes_to_the_podman_user_last(self):
        chowned = []
        self.lab.host.chown = chowned.append
        ctx = self.lab.fresh()
        stage, _, _ = stage_for_generator(ctx, make_plan(ctx, "nextcloud"))
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        self.assertEqual(chowned[-1], stage)
        for index, path in enumerate(chowned):      # nothing below a directory it already has
            self.assertFalse([p for p in chowned[index + 1:] if p.startswith(path + os.sep)], path)

    def test_unit_files_loose_in_the_quadlet_directory(self):
        unit = self.lab.write(os.path.join(self.outside, "u"),
                              f"[Container]\nVolume={self.path(MARK)}:/x\n")
        os.makedirs(self.qdir)
        os.symlink(unit, os.path.join(self.qdir, "loose.container"))
        with self.never_opened(unit):
            ctx = self.lab.fresh()
            self.assertNotIn(self.path(MARK), mount_sources(ctx))
            self.assertEqual(ctx.tree(), {})

    def test_unit_files_loose_in_the_quadlet_directory_swapped_since(self):
        loose = self.lab.write(os.path.join(self.qdir, "loose.container"), "[Container]\n")
        ctx = self.lab.fresh()
        ctx.tree()
        self.swap_for_link(loose)
        with self.never_opened():
            mount_sources(ctx)
            ports_taken(ctx)
            envfiles.host_issues(ctx, Survey(ctx))

    def test_units_swapped_between_the_scan_and_the_read(self):
        source = self.lab.write("nextcloud/quadlet/x.container", "[Container]\n")
        copy = self.lab.write(os.path.join(self.s.install_dir("nextcloud"), "y.container"), "[Container]\n")

        def walk_then_swap(*args, **kwargs):
            files = walk_files(*args, **kwargs)
            for path in (source, copy):
                if path in files.values():
                    self.swap_for_link(path)
            return files

        problem = f"symlink to {self.secret}, outside "
        with mock.patch("redh.app.walk_files", walk_then_swap), self.never_opened():
            ctx = self.lab.fresh()
            src, inst = ctx.sources("nextcloud"), ctx.installed("nextcloud")
        self.assertEqual(src.unreadable, [("x.container", problem + self.app)])
        self.assertEqual(inst.unreadable, [("y.container", problem + self.home)])

    def test_placeholders_swapped_since_the_scan(self):
        unit = self.lab.write("nextcloud/quadlet/x.env", "X=1\n")
        marked = self.lab.write(os.path.join(self.outside, "todo"), f"# {PLACEHOLDER}: {MARK}\n")
        ctx = self.lab.fresh()
        src = ctx.sources("nextcloud")
        self.swap_for_link(unit, marked)
        with self.never_opened(marked):
            self.assertEqual(placeholders(ctx, src), [])

    def test_a_snippet_swapped_since_the_scan_is_not_read_for_its_upstreams(self):
        source = self.path("nextcloud/caddy/nextcloud.caddy")
        ctx = self.lab.fresh()
        src, survey = ctx.sources("nextcloud"), Survey(ctx)
        survey.caddy_bin = None                         # Caddy itself is not what this is about
        self.swap_for_link(source)
        found = Findings()
        with self.never_opened():
            _caddy(found, ctx, "nextcloud", src, survey)
        self.assertEqual(failed(found, "caddy"), [f"cannot read {source}: symlink to {self.secret}, "
                                                  f"outside {self.app}"])

    def test_a_snippet_swapped_while_following_the_imports(self):
        source = self.path("nextcloud/caddy/nextcloud.caddy")
        other = self.lab.write("nextcloud/caddy/other.caddy", "other.example.org {\n}\n")
        main = self.lab.write(self.s.caddyfile, f"import {self.home}/*/caddy/*.caddy\n")
        real_expand = caddy.expand

        def expand_then_swap(*args):
            found = real_expand(*args)
            if os.path.exists(source) and not os.path.islink(source):
                self.swap_for_link(source)
            return found

        with mock.patch("redh.caddy.expand", expand_then_swap), self.never_opened():
            files = caddy.imported_files(main, self.s)
        self.assertIn(os.path.realpath(other), files)
        os.unlink(source)
        self.lab.write(source, "cloud.example.org {\n}\n")
        with mock.patch("redh.caddy.expand", expand_then_swap), self.never_opened():
            config = caddy.Isolator(main, set(), self.lab.tmp, None, self.s).build(os.path.realpath(other))
        self.assertIsNone(config)

    def test_an_env_file_swapped_since_it_was_found(self):
        path = self.lab.write("nextcloud/.env", "X=1\n", mode=0o644)
        ctx = self.lab.fresh()
        survey = Survey(ctx)
        self.swap_for_link(path)
        self.assertEqual(envfiles.assess(ctx, survey, [path], show=lambda p: p), ([], []))

    def test_finding_the_domain_for_a_new_app_does_not_follow_links(self):
        marked = self.lab.write(os.path.join(self.outside, "s"), "secret.example.org {\n}\n")
        source = self.path("nextcloud/caddy/nextcloud.caddy")
        ctx = self.lab.fresh()
        ctx.sources("nextcloud")
        self.swap_for_link(source, marked)
        with self.never_opened(marked):
            self.assertEqual(site_domain(ctx), (None, 0))

    def test_a_snippet_imported_in_place_is_not_followed(self):
        nextcloud(self.lab, in_place=True)
        self.lab.write(self.s.caddyfile, f"import {self.home}/*/caddy/*.caddy\n")
        self.link("nextcloud/caddy/x.caddy", self.secret)
        real = os.path.realpath(self.secret)
        self.assertNotIn(real, caddy.imported_files(self.s.caddyfile, self.s))
        kept = os.path.realpath(self.path("nextcloud/caddy/nextcloud.caddy"))
        self.assertIn(kept, caddy.imported_files(self.s.caddyfile, self.s))
        # Caddy's own user, which is what that scan is about, sees what Caddy would.
        self.assertIn(real, caddy.imported_files(self.s.caddyfile))
        ctx = self.lab.fresh()
        self.assertNotIn(real, Survey(ctx).imports)


class WritingTest(Planted):
    """Root writes and deletes in the podman user's directories, never through a link."""

    def setUp(self):
        super().setUp()
        nextcloud(self.lab)
        self.qdir = self.s.quadlet_dir
        self.target = os.path.join(self.outside, "systemd", "system")     # root's own
        self.keep = self.lab.write(os.path.join(self.target, "keep.service"), "root's own\n")

    def untouched(self):
        self.assertEqual(sorted(os.listdir(self.target)), ["keep.service"])
        with open(self.keep) as fh:
            self.assertEqual(fh.read(), "root's own\n")

    def install(self, rc=0):
        result = install(self.lab.fresh(), ["nextcloud"])
        out = self.lab.output()
        self.assertEqual(result, rc, out)
        return out

    def test_the_install_directory_is_a_link_out(self):
        os.makedirs(self.qdir)
        os.symlink(self.target, os.path.join(self.qdir, "nextcloud"))
        self.install(rc=1)
        self.untouched()

    def test_the_install_directory_is_a_link_elsewhere_in_the_home(self):
        elsewhere = self.path("elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(self.qdir)
        os.symlink(elsewhere, os.path.join(self.qdir, "nextcloud"))
        out = self.install(rc=1)
        self.assertIn("a symlink or not a directory", out)
        self.assertEqual(os.listdir(elsewhere), [])

    def test_the_quadlet_directory_is_a_link_out(self):
        os.makedirs(os.path.dirname(self.qdir))
        os.symlink(self.target, self.qdir)
        self.install(rc=1)
        self.untouched()

    def test_a_drop_in_directory_swapped_for_a_link_out(self):
        self.lab.write("nextcloud/quadlet/nextcloud.container.d/override.conf", "[Container]\n")
        self.install()
        dropins = os.path.join(self.qdir, "nextcloud", "nextcloud.container.d")
        shutil.rmtree(dropins)
        os.symlink(self.target, dropins)
        self.lab.write("nextcloud/quadlet/nextcloud.container.d/override.conf", "[Container]\n# new\n")
        self.install(rc=1)
        self.untouched()

    def test_an_installed_file_swapped_for_a_link_is_replaced_not_written_through(self):
        self.install()
        installed = os.path.join(self.qdir, "nextcloud", "nextcloud.container")
        os.unlink(installed)
        os.symlink(self.keep, installed)
        with open(self.path("nextcloud/quadlet/nextcloud.container"), "a") as fh:
            fh.write("# changed\n")
        self.install()
        self.assertFalse(os.path.islink(installed))
        self.untouched()

    def test_a_link_left_where_the_temporary_file_goes(self):
        os.makedirs(os.path.join(self.qdir, "nextcloud"))
        os.symlink(self.keep, os.path.join(self.qdir, "nextcloud", ".nextcloud.container.redh-tmp"))
        self.install()
        self.untouched()

    def test_uninstall_removes_a_link_not_what_it_points_to(self):
        self.install()
        real = os.path.join(self.qdir, "nextcloud")
        shutil.rmtree(real)
        os.symlink(self.target, real)
        self.assertEqual(uninstall(self.lab.fresh(), ["nextcloud"], force=True), 0, self.lab.output())
        self.assertFalse(os.path.lexists(real))
        self.untouched()


class FixTest(Planted):
    """'check --fix' changes things as root: what it does must not be the podman user's to steer."""

    def setUp(self):
        super().setUp()
        self.app = self.path("blog")
        self.lab.write("blog/quadlet/blog.container", """
            [Container]
            Image=docker.io/library/ghost:5
            """)
        os.chmod(self.home, 0o755)                     # others may enter, so there are things to fix
        self.addCleanup(os.chmod, self.home, 0o700)

    def secrets(self, app="blog"):
        ctx = self.lab.fresh()
        findings = Findings()
        envfiles.check_app(findings, ctx, Survey(ctx), app)
        return [(f.level, f.text, f.details) for f in findings if f.topic == "secrets"]

    def fix(self, *apps):
        ctx = self.lab.fresh()
        return perms.fix(ctx, Survey(ctx), list(apps or ["blog"])), self.lab.output()

    def test_an_env_link_out_fails_on_its_own_and_is_left_alone(self):
        self.link("blog/.env", self.secret)
        self.assertEqual(self.secrets(), [
            ("FAIL", f".env points to {self.secret}, outside {self.app}: not looked at",
             [f"remove the link, or keep the file below {self.app}"])])
        _, out = self.fix()
        self.assertEqual(mode(self.secret), 0o644)
        self.assertNotIn(self.secret + "\n", out)
        self.assertNotIn(f"chmod go-rwx {self.path('blog/.env')}", out)
        self.assertNotIn(["chmod", "go-rwx"], [c[:2] for c in self.lab.system.calls
                                               if c[-1].endswith(".env")])

    def test_an_env_link_inside_the_app_is_looked_at_as_usual(self):
        self.lab.write("blog/config/real.env", "X=1\n", mode=0o600)
        self.link("blog/.env", "config/real.env")
        self.assertEqual([level for level, _, _ in self.secrets()], ["ok"])

    def test_a_link_a_container_would_follow_is_its_own_business(self):
        data = self.path("blog/data")
        os.makedirs(data)
        self.lab.write("blog/quadlet/blog.container", f"""
            [Container]
            Image=docker.io/library/ghost:5
            Volume={data}:/data
            """)
        self.link("blog/data/.env", "/run/secrets/app")
        self.assertEqual(self.secrets(), [])

    def test_a_reported_link_is_not_reported_twice(self):
        self.link("blog/quadlet/x.env", self.secret)
        self.assertEqual(self.secrets(), [])
        ctx = self.lab.fresh()
        self.assertEqual(len(failed(check_app(ctx, "blog", Survey(ctx)), "symlink")), 1)

    def test_a_file_a_unit_names_elsewhere_is_reported_and_not_changed(self):
        self.lab.write("blog/quadlet/blog.container", f"""
            [Container]
            Image=docker.io/library/ghost:5
            EnvironmentFile={self.secret}
            """)
        [(level, text, details)] = self.secrets()
        self.assertEqual(level, "FAIL")
        self.assertTrue(text.startswith(f"{self.secret} ("), text)
        self.assertEqual(details, [f"a unit names it, but it lies outside {self.home}: "
                                   f"not redh's to change"])
        _, out = self.fix()
        self.assertEqual(mode(self.secret), 0o644)
        self.assertIn(f"skip  {self.secret}: a unit names it", out)

    def test_a_unit_naming_an_env_link_out(self):
        link = self.link("blog/config/token", self.secret)
        self.lab.write("blog/quadlet/blog.container", f"""
            [Container]
            Image=docker.io/library/ghost:5
            EnvironmentFile={link}
            """)
        [(level, text, _)] = self.secrets()
        self.assertEqual(level, "FAIL")
        self.assertIn(f"points to {self.secret}, outside {self.app}: not looked at", text)
        self.fix()
        self.assertEqual(mode(self.secret), 0o644)

    def test_an_app_directory_that_is_a_link_is_left_alone(self):
        os.chmod(self.outside, 0o755)
        self.lab.write(os.path.join(self.outside, ".env"), "X=1\n", mode=0o644)
        self.link("evil", self.outside)
        ctx = self.lab.fresh()
        found = perms.evaluate(ctx, Survey(ctx), [self.path("evil")])[0]
        self.assertEqual(found, [])
        ok, out = self.fix("evil")
        self.assertTrue(ok, out)
        self.assertEqual(mode(self.outside), 0o755)
        self.assertEqual(mode(os.path.join(self.outside, ".env")), 0o644)
        self.assertNotIn("chmod 700 " + self.path("evil"), out)

    def test_an_env_link_at_the_top_of_the_apps_root(self):
        link = self.link(".env", self.secret)
        ctx = self.lab.fresh()
        texts = [f.text for f in check_host(ctx, Survey(ctx)) if f.topic == "secrets"]
        self.assertIn(f"{link} points to {self.secret}, outside {self.home}: not looked at", texts)


if __name__ == "__main__":
    unittest.main()
