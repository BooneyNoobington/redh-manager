import json
import os
import stat
import unittest

from redh import userunits
from redh.checks import Survey, check_app, check_host
from redh.install import install, uninstall
from redh.lifecycle import list_apps, start_app, status_app, stop_app
from redh.prune import prune

from fakes import Lab

TIMER = "dms-cert-renew.timer"
SERVICE = "dms-cert-renew.service"


def dms(lab, with_quadlet=True):
    """Like the real one: a mail server, and a timer that renews its certificate."""
    if with_quadlet:
        lab.write("dms/quadlet/dms.container", """
            [Container]
            Image=ghcr.io/docker-mailserver/docker-mailserver:latest
            AutoUpdate=registry
            Network=dms.network

            [Install]
            WantedBy=default.target
            """)
        lab.write("dms/quadlet/dms.network", "[Network]\n")
    lab.write("dms/renew-cert.sh", "#!/bin/sh\nexit 0\n", mode=0o755)
    lab.write(f"dms/systemd/{SERVICE}", """
        [Unit]
        Description=Renew the DMS TLS certificate from step-ca

        [Service]
        Type=oneshot
        ExecStart=%h/dms/renew-cert.sh
        """)
    lab.write(f"dms/systemd/{TIMER}", """
        [Unit]
        Description=Daily DMS cert renewal check

        [Timer]
        OnCalendar=daily
        Persistent=true

        [Install]
        WantedBy=timers.target
        """)


def lines(findings, level=None):
    return [f"{f.level} {f.topic} {f.text}" for f in findings if level in (None, f.level)]


class UserUnitTest(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.cleanup)
        self.s = self.lab.settings
        self.udir = self.s.user_unit_dir
        dms(self.lab)

    def copy(self, name):
        return os.path.join(self.udir, name)

    def install(self, rc=0, **options):
        result = install(self.lab.fresh(), ["dms"], **options)
        out = self.lab.output()
        self.assertEqual(result, rc, out)
        return out

    def calls(self, verb):
        return [c[c.index("--") + 1:] for c in self.lab.system.calls
                if c[:3] == ["systemctl", "--user", verb]]

    def check(self, app="dms"):
        ctx = self.lab.fresh()
        return lines(check_app(ctx, app, Survey(ctx)))

    # -- install ------------------------------------------------------------------

    def test_install_copies_marks_and_enables(self):
        out = self.install()
        self.assertIn(f"+ {self.udir}/{TIMER}", out)
        self.assertIn(f"+ {self.udir}/{SERVICE}", out)
        self.assertIn("systemd knows all 4 units", out)
        self.assertIn(f"enabled {TIMER}", out)
        self.assertIn("not running → redh start dms", out)
        source = os.path.join(self.s.root, "dms", "systemd", TIMER)
        with open(self.copy(TIMER)) as fh:
            self.assertEqual(fh.readline(), f"# Installed by redh from {source} - edit that file, "
                                            f"then: redh install dms\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.copy(TIMER)).st_mode), 0o644)
        # The timer is enabled; the service it starts has no [Install] and is not.
        self.assertEqual(self.lab.system.enabled, {TIMER})
        self.assertEqual(self.calls("enable"), [[TIMER]])

        out = self.install()
        self.assertIn("already up to date", out)
        self.assertEqual(self.calls("enable"), [[TIMER]])          # not again

    def test_changes_reenable_and_removals_stop_and_disable(self):
        self.install()
        self.lab.write(f"dms/systemd/{TIMER}", "[Timer]\nOnCalendar=weekly\n\n"
                                               "[Install]\nWantedBy=default.target\n")
        self.lab.write(f"dms/systemd/{SERVICE}", "[Service]\nType=oneshot\nExecStart=/bin/true\n")
        out = self.install()
        self.assertIn(f"~ {self.udir}/{TIMER}", out)
        self.assertIn(f"reenabled {TIMER}", out)
        self.assertNotIn("disabled", out)                           # the service never was

        start_app(self.lab.fresh(), "dms")
        os.unlink(os.path.join(self.s.root, "dms", "systemd", TIMER))
        out = self.install()
        self.assertIn(f"- {self.udir}/{TIMER}", out)
        self.assertIn([TIMER], self.calls("stop"))
        self.assertIn([TIMER], self.calls("disable"))
        self.assertFalse(os.path.exists(self.copy(TIMER)))
        self.assertNotIn(TIMER, self.lab.system.units)
        self.assertEqual(self.lab.system.enabled, set())

    def test_what_install_refuses(self):
        os.makedirs(self.udir)
        with open(self.copy(TIMER), "w") as fh:
            fh.write("[Timer]\nOnCalendar=hourly\n")                  # someone's, by hand
        out = self.install(rc=1)
        self.assertIn(f"systemd/{TIMER}: redh did not install it", out)
        self.assertIn("nothing installed", out)
        with open(self.copy(TIMER)) as fh:
            self.assertEqual(fh.read(), "[Timer]\nOnCalendar=hourly\n")
        self.assertFalse(os.path.exists(self.copy(SERVICE)))
        os.unlink(self.copy(TIMER))

        # Another app's.
        self.lab.write(f"backup/systemd/{TIMER}", "[Timer]\nOnCalendar=hourly\n")
        self.assertEqual(install(self.lab.fresh(), ["backup"]), 0, self.lab.output())
        out = self.install(rc=1)
        self.assertIn(f"systemd/{TIMER}: it belongs to backup", out)

        # A name Quadlet makes: this one would replace the container.
        self.lab.write("dms/systemd/dms.service", "[Service]\nExecStart=/bin/true\n")
        out = self.install(rc=1, force=True)
        self.assertIn("systemd/dms.service: Quadlet makes a unit of that name from "
                      "dms.container (dms)", out)

    def test_an_app_of_plain_units_only(self):
        self.lab.write("backup/systemd/backup.service", "[Service]\nType=oneshot\nExecStart=/bin/true\n")
        self.lab.write("backup/systemd/backup.timer", "[Timer]\nOnCalendar=daily\n\n"
                                                       "[Install]\nWantedBy=timers.target\n")
        self.assertEqual(install(self.lab.fresh(), ["backup"]), 0, self.lab.output())
        ctx = self.lab.fresh()
        self.assertIn("backup", ctx.installed_apps())
        self.assertEqual([lines for lines in self.check("backup") if lines.startswith("FAIL")], [])
        self.assertTrue(start_app(self.lab.fresh(), "backup"))
        self.assertEqual(self.lab.system.units["backup.timer"]["ActiveState"], "active")

    # -- start, stop, status, list ------------------------------------------------

    def test_start_arms_the_timer_and_stop_stops_both(self):
        self.install()
        self.lab.system.calls.clear()
        self.assertTrue(start_app(self.lab.fresh(), "dms"))
        out = self.lab.output()
        [started] = self.calls("start")
        self.assertIn(TIMER, started)
        self.assertIn("dms.service", started)
        self.assertNotIn(SERVICE, started)                          # that would renew right now
        self.assertIn(f"{TIMER}  active (waiting)", out.replace("   ", " ").replace("  ", "  "))

        status_app(self.lab.fresh(), "dms", [])
        out = self.lab.output()
        self.assertIn("next run 2026-10-06 00:23", out)
        self.assertIn(SERVICE, out)

        self.lab.system.calls.clear()
        self.assertTrue(stop_app(self.lab.fresh(), "dms"))
        [stopped] = self.calls("stop")
        self.assertIn(TIMER, stopped)
        self.assertEqual(self.lab.system.units[TIMER]["ActiveState"], "inactive")

    def test_list_counts_the_timer(self):
        self.install()
        start_app(self.lab.fresh(), "dms")
        self.lab.output()
        list_apps(self.lab.fresh(), as_json=True)
        [entry] = json.loads(self.lab.output())["apps"]
        self.assertEqual((entry["units"], entry["services"], entry["active"], entry["installed"]),
                         (4, 3, 3, "in sync"))

    # -- uninstall, prune -----------------------------------------------------------

    def test_uninstall_and_prune_take_them_out(self):
        self.install()
        start_app(self.lab.fresh(), "dms")
        self.assertEqual(uninstall(self.lab.fresh(), ["dms"]), 0, self.lab.output())
        self.assertFalse(os.path.exists(self.copy(TIMER)))
        self.assertIn(sorted([TIMER, SERVICE]), [sorted(c) for c in self.calls("disable")])
        self.assertEqual(self.lab.system.enabled, set())
        self.assertNotIn("dms", self.lab.fresh().installed_apps())

        self.install()
        self.assertEqual(prune(self.lab.fresh(), "dms", yes=True), 0, self.lab.output())
        self.assertEqual(os.listdir(self.udir), [])

    # -- check ------------------------------------------------------------------------

    def test_check_before_and_after(self):
        found = self.check()
        self.assertIn("warn install not installed → redh install dms", found)
        self.assertFalse([f for f in found if "plain units not installed" in f])   # said once
        self.install()
        start_app(self.lab.fresh(), "dms")
        self.lab.output()
        found = self.check()
        self.assertEqual([f for f in found if f.split()[0] in ("FAIL", "warn")
                          and f.split()[1] != "caddy"], [])         # it has no web UI here
        self.assertIn(f"ok install plain units installed: {SERVICE}, {TIMER}", found)
        self.assertIn(f"ok runtime plain units: {TIMER} active", found)

        self.lab.write(f"dms/systemd/{TIMER}", "[Timer]\nOnCalendar=weekly\n\n"
                                               "[Install]\nWantedBy=timers.target\n")
        self.lab.system.enabled.clear()
        self.lab.system.units[SERVICE].update(ActiveState="failed", Result="exit-code",
                                              ExecMainStatus="1")
        found = "\n".join(self.check())
        self.assertIn(f"plain units in {self.udir} are out of date → redh install dms", found)
        self.assertIn(f"{TIMER} is not enabled, so it does not start at boot", found)
        self.assertIn(f"warn runtime {SERVICE}: failed", found)

    def test_systemds_own_verdict(self):
        self.lab.system.verify_errors = [
            f"{self.s.root}/dms/systemd/{SERVICE}:7: Unknown key name 'ExecStrat' in section "
            f"'Service', ignoring.",
            "other.service: something about a unit that is not ours"]
        found = "\n".join(self.check())
        self.assertIn("FAIL units systemd-analyze finds fault with systemd/", found)
        verify = [c for c in self.lab.system.calls if "systemd-analyze" in c]
        self.assertEqual(verify[0][:3], ["systemd-analyze", "--user", "verify"])
        ctx = self.lab.fresh()
        failing = [f for f in check_app(ctx, "dms", Survey(ctx)) if f.topic == "units"
                   and f.level == "FAIL"]
        self.assertEqual(len(failing[0].details), 1)                 # not the other unit's line

    def test_layout_hints(self):
        self.lab.write("blog/quadlet/blog.container", "[Container]\nImage=docker.io/x/blog:1\n")
        self.lab.write("blog/quadlet/blog-backup.timer", "[Timer]\nOnCalendar=daily\n")
        self.lab.write("blog/SystemD/blog-clean.timer", "[Timer]\nOnCalendar=daily\n")
        found = "\n".join(self.check("blog"))
        self.assertIn("blog-backup.timer: plain systemd unit, which Quadlet ignores", found)
        self.assertIn("SystemD/ looks like systemd/, but redh reads only systemd/", found)
        self.lab.write("dms/systemd/renew.sh", "#!/bin/sh\n")
        found = "\n".join(self.check())
        self.assertIn("renew.sh in systemd/: not unit files", found)

    def test_host_names_copies_whose_app_is_gone(self):
        self.install()
        os.rename(os.path.join(self.s.root, "dms"), os.path.join(self.lab.tmp, "dms-moved"))
        ctx = self.lab.fresh()
        found = "\n".join(lines(check_host(ctx, Survey(ctx))))
        self.assertIn(f"{self.udir}/{SERVICE}", found)
        self.assertIn("is gone", found)

    def test_the_unit_directory_as_a_link_out(self):
        target = os.path.join(self.lab.tmp, "etc", "systemd", "user")
        os.makedirs(target)
        os.makedirs(os.path.dirname(self.udir))
        os.symlink(target, self.udir)
        self.install(rc=1)
        self.assertEqual(os.listdir(target), [])
        ctx = self.lab.fresh()
        found = "\n".join(lines(check_host(ctx, Survey(ctx))))
        self.assertIn(f"FAIL symlink {self.udir} points to {target}", found)

    def test_a_planted_link_in_the_unit_directory_is_replaced(self):
        keep = self.lab.write(os.path.join(self.lab.tmp, "etc", "keep"), "root's own\n")
        os.makedirs(self.udir)
        os.symlink(keep, os.path.join(self.udir, f".{TIMER}.redh-tmp"))
        self.install()
        with open(keep) as fh:
            self.assertEqual(fh.read(), "root's own\n")

    def test_the_plan_alone(self):
        ctx = self.lab.fresh()
        plan = userunits.plan(ctx, "dms")
        self.assertEqual((plan.add, plan.starts), ([SERVICE, TIMER], [TIMER]))
        self.assertEqual(userunits.removal(ctx, "dms").remove, [])


if __name__ == "__main__":
    unittest.main()
