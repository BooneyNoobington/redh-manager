"""Plain units of an app: timers, and the services they start, as the podman user's.

    /opt/podman/<app>/systemd/x.timer  ->  /opt/podman/.config/systemd/user/x.timer

Quadlet makes containers, pods and networks; a job that runs now and then
(renewing a certificate, say) is a timer and a service, plain units of the
podman user's systemd. redh installs them like the Quadlet files: copies, the
podman user's, then daemon-reload. That directory has no room for one
subdirectory per app, so each copy begins with a comment naming its source,
like the Caddy snippets: that is how redh tells its files from anyone else's,
and which app each belongs to.

A unit with an [Install] section is enabled, and 'redh start' starts it: the
timer. One without only runs when something starts it, the timer's service:
'redh start' leaves it alone, 'redh stop' stops it too.

A name Quadlet also makes is refused. systemd ranks this directory above the
generator's output, so a plain dms.service here would take the place of the
container dms.container makes.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from . import confine
from .app import Context
from .common import join_names
from .sites import HEADER
from .units import PlainUnit

MODE = 0o644


def content_for(settings, app: str, source: str) -> bytes:
    return HEADER.format(source=source, app=app).encode() + confine.read_bytes(settings, source)


@dataclass
class UnitPlan:
    app: str
    wanted: Dict[str, str] = field(default_factory=dict)       # name -> source
    add: List[str] = field(default_factory=list)
    change: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    same: List[str] = field(default_factory=list)
    conflicts: List[Tuple[str, str]] = field(default_factory=list)   # name, why not
    starts: List[str] = field(default_factory=list)            # wanted, with [Install]

    @property
    def in_sync(self) -> bool:
        return not (self.add or self.change or self.remove)

    def lines(self, settings) -> List[str]:
        where = settings.user_unit_dir
        return ([f"+ {where}/{n}" for n in self.add] + [f"~ {where}/{n}" for n in self.change]
                + [f"- {where}/{n}" for n in self.remove])


def quadlet_names(ctx: Context) -> Dict[str, str]:
    """Every unit name Quadlet makes for any app: name -> 'x.container (app)'."""
    found: Dict[str, str] = {}
    for app in ctx.known_apps():
        for u in ctx.units(app):
            for name in filter(None, (u.service, u.startable)):
                found.setdefault(name, f"{u.filename} ({app})")
    return found


def plan(ctx: Context, app: str) -> UnitPlan:
    s = ctx.settings
    src = ctx.sources(app)
    result = UnitPlan(app, wanted=dict(src.user_units) if src.exists else {})
    present = ctx.user_files()
    made_by_quadlet = quadlet_names(ctx) if result.wanted else {}
    for name, source in sorted(result.wanted.items()):
        if name in made_by_quadlet:
            result.conflicts.append((name, f"Quadlet makes a unit of that name from "
                                           f"{made_by_quadlet[name]}, and this one would replace it"))
            continue
        try:
            if PlainUnit.load(source, confine.reader(s)).starts:
                result.starts.append(name)
        except OSError:
            pass                          # gone, or a link out by now: apply says so
        if name not in present:
            result.add.append(name)
            continue
        path, owner = present[name]
        other = ctx.owner_of(present[name])
        if other != app:
            result.conflicts.append((name, f"it belongs to {other}" if other else
                                           "redh did not install it"))
            continue
        try:
            same = (confine.read_bytes(s, path) == content_for(s, app, source)
                    and stat.S_IMODE(confine.stat_of(s, path).st_mode) == MODE)
        except OSError:
            same = False
        (result.same if same else result.change).append(name)
    result.remove = sorted(name for name, entry in present.items()
                           if ctx.owner_of(entry) == app and name not in result.wanted)
    return result


def removal(ctx: Context, app: str) -> UnitPlan:
    """A plan that takes every plain unit installed for app out again."""
    return UnitPlan(app, remove=sorted(name for name, entry in ctx.user_files().items()
                                       if ctx.owner_of(entry) == app))


def apply(ctx: Context, units: UnitPlan) -> List[str]:
    """Write and delete the copies; returns what went wrong. Stopping and disabling
    what goes, reloading and enabling are the caller's."""
    s, host = ctx.settings, ctx.host
    if units.in_sync:
        return []
    try:
        directory = confine.directory(s, s.user_unit_dir, owner=host.chown_fd)
    except OSError as exc:
        return [f"{s.user_unit_dir}: {exc.strerror or exc}"]
    errors = []
    try:
        for name in units.add + units.change:
            try:
                confine.write_file(directory, name, content_for(s, units.app, units.wanted[name]),
                                   MODE, host.chown_fd)
            except OSError as exc:
                errors.append(f"{name}: {exc.strerror or exc}")
        for name in units.remove:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{name}: {exc.strerror or exc}")
    finally:
        os.close(directory)
    ctx.forget()
    return errors


def take_out(ctx: Context, names: List[str]) -> None:
    """Before their files go: stop them and remove their links in *.wants/."""
    if names:
        ctx.host.systemctl("stop", "--", *names, timeout=300)
        ctx.host.systemctl("disable", "--", *names, timeout=60)


def enable(ctx: Context, units: UnitPlan) -> List[Tuple[str, str]]:
    """After daemon-reload: enable what has an [Install] section (again, if it changed:
    its WantedBy= may be another now), and disable what lost it. Returns (level, text)."""
    host = ctx.host
    report: List[Tuple[str, str]] = []
    if not units.wanted:
        return report
    asked = sorted(set(units.starts) | set(units.change))
    states = host.show(asked, ["UnitFileState"]) if asked else {}
    fresh = [n for n in units.starts
             if n not in units.change and states[n].get("UnitFileState") != "enabled"]
    again = [n for n in units.starts if n in units.change]
    lost = [n for n in units.change
            if n not in units.starts and states[n].get("UnitFileState") == "enabled"]
    for verb, names in (("enable", fresh), ("reenable", again), ("disable", lost)):
        if not names:
            continue
        result = host.systemctl(verb, "--", *names, timeout=60)
        if result.ok:
            report.append(("ok", f"{verb}d {join_names(names, 4)}" if verb != "disable"
                           else f"no [Install] section any more, disabled {join_names(names, 4)}"))
        else:
            report.append(("FAIL", f"systemctl {verb} {' '.join(names)}: {result.message()}"))
    return report
