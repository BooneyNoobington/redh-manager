"""Caddy snippets, installed into one directory below /etc/caddy like the units.

    /opt/podman/<app>/caddy/Caddyfile   ->  /etc/caddy/Caddyfile.d/<app>.caddy
    /opt/podman/<app>/caddy/<name>      ->  /etc/caddy/Caddyfile.d/<app>.<name>.caddy

The main Caddyfile imports them all with one line:

    import /etc/caddy/Caddyfile.d/*.caddy

Caddy then only reads /etc/caddy, and /opt/podman stays the podman user's
alone. Every installed file begins with a comment naming its source: that is
how redh tells its files from anyone else's in that directory, and to which
app each belongs. Installed files are root's, readable by Caddy's group only.

A change is checked before it counts: Caddy validates the whole configuration
as its own user, the old file goes back if it fails, and Caddy reloads if not.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import caddy, confine

HEADER = "# Installed by redh from {source} - edit that file, then: redh install {app}\n"
HEADER_RE = re.compile(r"^# Installed by redh from (\S+) ")


def app_of(settings, source: str) -> Optional[str]:
    rel = os.path.relpath(source, settings.root)
    return None if rel.startswith("..") else rel.split(os.sep)[0]


def dest_name(app: str, base: str, snippet: str) -> str:
    """<app>.caddy for a Caddyfile (or <app>.caddy), <app>.<name>.caddy for anything else."""
    stem = os.path.relpath(snippet, base).replace(os.sep, ".")
    for suffix in (".caddyfile", ".caddy"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    return f"{app}.caddy" if stem.lower() in ("caddyfile", "", app.lower()) else f"{app}.{stem}.caddy"


def content_for(settings, app: str, source: str) -> bytes:
    return HEADER.format(source=source, app=app).encode() + confine.read_bytes(settings, source)


def installed(settings) -> Dict[str, Tuple[str, Optional[str]]]:
    """Every *.caddy file in the directory: name -> (path, source; None if not redh's)."""
    files: Dict[str, Tuple[str, Optional[str]]] = {}
    try:
        names = sorted(os.listdir(settings.caddy_dir))
    except OSError:
        return files
    for name in names:
        path = os.path.join(settings.caddy_dir, name)
        if name.startswith(".") or not name.endswith(".caddy") or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                match = HEADER_RE.match(fh.readline())
        except OSError:
            match = None
        files[name] = (path, match.group(1) if match else None)
    return files


@dataclass
class SitePlan:
    app: str
    wanted: Dict[str, str] = field(default_factory=dict)       # name -> source
    add: List[str] = field(default_factory=list)
    change: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    same: List[str] = field(default_factory=list)
    conflicts: List[Tuple[str, str]] = field(default_factory=list)   # name, who has it

    @property
    def in_sync(self) -> bool:
        return not (self.add or self.change or self.remove)

    def lines(self, settings) -> List[str]:
        where = settings.caddy_dir
        return ([f"+ {where}/{n}" for n in self.add] + [f"~ {where}/{n}" for n in self.change]
                + [f"- {where}/{n}" for n in self.remove])


def plan(ctx, app: str) -> SitePlan:
    s = ctx.settings
    src = ctx.sources(app)
    result = SitePlan(app)
    if src.exists and src.snippets:
        base = (os.path.join(src.dir, s.caddy_subdir) if src.snippet_layout != "top level"
                else src.dir)
        result.wanted = {dest_name(app, base, p): p for p in src.snippets}
    present = installed(s)
    for name, source in sorted(result.wanted.items()):
        if name in present:
            path, owner = present[name]
            if owner is None or app_of(s, owner) != app:
                result.conflicts.append((name, app_of(s, owner) if owner else "no redh app"))
                continue
            with open(path, "rb") as fh:
                current = fh.read()
            try:
                same = (current == content_for(s, app, source)
                        and stat.S_IMODE(os.stat(path).st_mode) == 0o640)
            except OSError:
                same = False                  # gone, or a symlink by now: apply says so
            (result.same if same else result.change).append(name)
        else:
            result.add.append(name)
    result.remove = sorted(name for name, (_, owner) in present.items()
                           if owner and app_of(s, owner) == app and name not in result.wanted)
    return result


def removal(ctx, app: str) -> SitePlan:
    """A plan that takes every snippet installed for app out again."""
    s = ctx.settings
    return SitePlan(app, remove=sorted(name for name, (_, owner) in installed(s).items()
                                       if owner and app_of(s, owner) == app))


def caddy_gid(survey) -> int:
    """The group Caddy runs in, which may read the snippets; root's if unknown."""
    try:
        return pwd.getpwnam(survey.caddy_user).pw_gid if survey.caddy_user else 0
    except KeyError:
        return 0


def imported(ctx, survey) -> bool:
    """Does the main Caddyfile import the snippet directory?"""
    target = os.path.realpath(ctx.settings.caddy_dir)
    return any(os.path.realpath(os.path.dirname(e.pattern)) == target for e in survey.import_lines)


def import_line(settings) -> str:
    return f"import {settings.caddy_dir}/*.caddy"


def write(ctx, survey, site: SitePlan, contents: Dict[str, bytes]) -> Dict[str, Optional[bytes]]:
    """Carry the plan out; returns what each touched file held before (None: new)."""
    s, host = ctx.settings, ctx.host
    gid = caddy_gid(survey)
    if not os.path.isdir(s.caddy_dir):
        os.makedirs(s.caddy_dir, 0o750)
        host.chown_to(s.caddy_dir, 0, gid)
    before: Dict[str, Optional[bytes]] = {}
    for name in site.add + site.change:
        dest = os.path.join(s.caddy_dir, name)
        with suppress(OSError), open(dest, "rb") as fh:
            before[name] = fh.read()
        before.setdefault(name, None)
        temp = os.path.join(s.caddy_dir, f".{name}.redh-tmp")
        with open(temp, "wb") as fh:
            fh.write(contents[name])
        os.chmod(temp, 0o640)
        host.chown_to(temp, 0, gid)
        os.replace(temp, dest)
    for name in site.remove:
        dest = os.path.join(s.caddy_dir, name)
        with open(dest, "rb") as fh:
            before[name] = fh.read()
        os.unlink(dest)
    return before


def restore(ctx, survey, before: Dict[str, Optional[bytes]]) -> None:
    s, host = ctx.settings, ctx.host
    for name, content in before.items():
        dest = os.path.join(s.caddy_dir, name)
        if content is None:
            with suppress(OSError):
                os.unlink(dest)
        else:
            with open(dest, "wb") as fh:
                fh.write(content)
            os.chmod(dest, 0o640)
            host.chown_to(dest, 0, caddy_gid(survey))


def apply(ctx, survey, site: SitePlan) -> bool:
    """Write, let Caddy validate as its own user, put things back on failure, reload."""
    s, host, out = ctx.settings, ctx.host, ctx.out
    if site.in_sync:
        return True
    existing = s.caddy_dir
    while not os.path.exists(existing):
        existing = os.path.dirname(existing)
    if not os.access(existing, os.W_OK):
        out.status("FAIL", f"cannot write {s.caddy_dir} (run as root); the Caddy snippet is "
                           f"not installed")
        return False
    try:                                  # before anything changes: the source is not ours
        contents = {name: content_for(s, site.app, site.wanted[name])
                    for name in site.add + site.change}
    except OSError as exc:
        out.status("FAIL", f"{exc}; the Caddy snippet is not installed")
        return False
    checkable = bool(survey.caddy_bin and survey.caddyfile_exists)
    valid_before = checkable and caddy.validate(host, survey.caddy_bin, s.caddyfile,
                                                survey.caddy_user)[0].ok
    before = write(ctx, survey, site, contents)
    if valid_before:
        result, errors, _ = caddy.validate(host, survey.caddy_bin, s.caddyfile, survey.caddy_user)
        if not result.ok:
            restore(ctx, survey, before)
            out.status("FAIL", "Caddy rejects the new snippet; the old one is back in place",
                       errors[-3:])
            return False
    done = [f"{len(site.add) + len(site.change)} written"] if site.add or site.change else []
    done += [f"{len(site.remove)} removed"] if site.remove else []
    out.status("ok", f"Caddy snippets in {s.caddy_dir}: {', '.join(done)}")
    if not imported(ctx, survey):
        out.status("warn", f"{s.caddyfile} does not import {s.caddy_dir}, so Caddy does not "
                           f"load it", [f"add to {s.caddyfile}: {import_line(s)}"])
        return True
    if checkable and not valid_before:
        out.status("warn", f"{s.caddyfile} did not validate before either; Caddy not reloaded",
                   [f"caddy validate --config {s.caddyfile}"])
        return True
    return reload(ctx)


def reload(ctx) -> bool:
    s, host, out = ctx.settings, ctx.host, ctx.out
    began = time.time()
    result = host.run(["systemctl", "reload", s.caddy_service], timeout=120)
    if result.ok:
        out.status("ok", f"reloaded {s.caddy_service}")
        return True
    log = host.run(["journalctl", "--no-pager", "--quiet", "--output=short", "--lines=8",
                    f"--since=@{int(began) - 1}", "-u", s.caddy_service], timeout=30)
    out.status("FAIL", f"reloading {s.caddy_service}: {result.message()}",
               [line for line in log.out.splitlines() if line.strip()])
    return False
