"""redh check: is an app laid out, written and installed the way it should be?

Every check reports findings — ok, info, warn or FAIL, each under a topic.
Only FAIL makes 'redh check' exit non-zero. A warn is something worth fixing
that does not stop the app from working today.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import caddy, confine, envfiles, lifecycle, perms, sites, userunits
from .app import Context, Sources
from .common import HostError, join_names, mode_of, plural
from .confine import Escape
from .host import GENERATOR_CANDIDATES, STATE_PROPS, Result, is_task
from .plan import Plan, make_plan
from .report import WIDTH
from .running import mount_sources
from .update import last_automatic_run
from .units import (SYSTEM_ONLY_TARGETS, PlainUnit, QuadletUnit, is_global_dropin_dir,
                    quadlet_ext)

MIN_PODMAN = (4, 7)          # Quadlet searches subdirectories from 4.7 on
TOPIC_WIDTH = 9
# What 'redh init' leaves for a person to fill in; install refuses while any is left.
PLACEHOLDER = "TODO(redh init)"


@dataclass
class Finding:
    level: str
    topic: str
    text: str
    details: List[str] = field(default_factory=list)
    verbose: bool = False        # only shown with -v


class Findings(List[Finding]):
    def add(self, level: str, topic: str, text: str,
            details: Iterable[str] = (), verbose: bool = False) -> None:
        self.append(Finding(level, topic, text, list(details), verbose))

    def ok(self, topic: str, text: str, details: Iterable[str] = (), verbose: bool = False) -> None:
        self.add("ok", topic, text, details, verbose)

    def info(self, topic: str, text: str, details: Iterable[str] = (), verbose: bool = False) -> None:
        self.add("info", topic, text, details, verbose)

    def warn(self, topic: str, text: str, details: Iterable[str] = ()) -> None:
        self.add("warn", topic, text, details)

    def fail(self, topic: str, text: str, details: Iterable[str] = ()) -> None:
        self.add("FAIL", topic, text, details)

    def count(self, level: str, topic: Optional[str] = None) -> int:
        return sum(1 for f in self if f.level == level and topic in (None, f.topic))

    @property
    def failed(self) -> bool:
        return self.count("FAIL") > 0


def show(out, findings: Iterable[Finding]) -> None:
    for f in findings:
        if f.verbose and not out.verbose:
            continue
        out.status(f.level, f"{f.topic:<{TOPIC_WIDTH}} {f.text}", f.details,
                   detail_indent=2 + WIDTH + 1 + TOPIC_WIDTH + 1)


@dataclass
class Publish:
    app: str
    file: str
    spec: str
    address: str
    low: int
    high: int
    rest: str

    def covers(self, port: int) -> bool:
        return self.low <= port <= self.high

    @property
    def proto(self) -> str:
        return self.spec.partition("/")[2] or "tcp"

    def local_spec(self) -> str:
        ports = str(self.low) if self.low == self.high else f"{self.low}-{self.high}"
        return f"127.0.0.1:{ports}:{self.rest}"


class Survey:
    """Facts about every app and the host that the per-app checks compare against."""

    def __init__(self, ctx: Context) -> None:
        s = ctx.settings
        self.apps = ctx.known_apps()
        self.providers: Dict[str, List[str]] = {}           # unit file name -> apps
        self.services: Dict[str, Set[Tuple[str, str]]] = {}  # service -> {(app, file)}
        self.publishers: List[Publish] = []
        self.snippets: Set[str] = set()                      # real paths, every app
        self.caddy_dir = s.caddy_dir
        self.snippet_owner: Dict[str, str] = {}
        self.autoupdate = 0
        self.builders: Dict[str, Tuple[str, QuadletUnit]] = {}   # image -> (app, its .build)
        for app in self.apps:
            for unit in ctx.units(app):
                self.providers.setdefault(unit.filename, []).append(app)
                if unit.ext == ".build" and unit.get("ImageTag"):
                    self.builders.setdefault(full_ref(unit.get("ImageTag")), (app, unit))
                self.services.setdefault(unit.service, set()).add((app, unit.filename))
                if unit.get_all("AutoUpdate"):
                    self.autoupdate += 1
                for spec in unit.publish_ports():
                    parsed = caddy.parse_publish(spec)
                    if parsed:
                        self.publishers.append(Publish(app, unit.filename, spec, *parsed))
            for path in ctx.sources(app).snippets:
                real = os.path.realpath(path)
                self.snippets.add(real)
                self.snippet_owner[real] = app
        self.caddy_bin = shutil.which(s.caddy)
        self.caddyfile_exists = os.path.isfile(s.caddyfile)
        self.import_lines = caddy.scan_imports(s.caddyfile, s) if self.caddyfile_exists else []
        self.imports = caddy.imported_files(s.caddyfile, s) if self.caddyfile_exists else {}
        # Caddy runs as its own user; what root can read, it may not reach.
        identity = ctx.host.service_identity(s.caddy_service)
        self.caddy_user: Optional[str] = identity[0] if identity else None
        self.caddy_groups: List[str] = identity[1] if identity else []
        self.subuids = ctx.host.subordinate_uids()
        self.mounts = mount_sources(ctx)             # never take bits away from these
        # Files Caddy opens below the apps root besides the snippets: certificates, keys,
        # CA roots, static roots, sockets — read from its adapted configuration.
        self.caddy_paths: Dict[str, str] = (
            caddy.referenced_paths(ctx.host, self.caddy_bin, s.caddyfile, s.root)
            if self.caddy_bin and self.caddyfile_exists else {})
        self.service_imports: Optional[Set[str]] = None     # None: could not look
        self.blocked: Dict[str, Tuple[str, str]] = {}       # real path -> (where, enter/list/read)
        if self.caddyfile_exists and identity and identity[0] != "root":
            with ctx.host.impersonate(*identity) as switched:
                if switched:
                    self.service_imports = set(caddy.imported_files(s.caddyfile))
                    for real, entry in self.imports.items():
                        if real not in self.service_imports:
                            stop = caddy.blocker(real, entry.pattern)
                            if stop:
                                self.blocked[real] = stop
        self.generator = ctx.host.generator()
        self.manager_ok = True
        self.timer_on: Optional[bool] = None


# -- the host ---------------------------------------------------------------

def check_host(ctx: Context, survey: Survey) -> Findings:
    f = Findings()
    s, host = ctx.settings, ctx.host

    lingers = host.linger()
    if lingers:
        f.ok("user", f"{s.user} (uid {s.uid}) lingers, so its units start at boot")
    else:
        f.fail("user", f"linger is off for {s.user}: its units stop at logout "
                       f"and never start at boot", [f"loginctl enable-linger {s.user}"])

    state = host.systemctl("is-system-running", timeout=30)
    word = state.out.strip()
    if word == "running":
        f.ok("user", f"{s.user}'s user manager is running", verbose=True)
    elif word == "degraded":
        listing = host.systemctl("list-units", "--state=failed", "--plain",
                                 "--no-legend", timeout=30)
        failed = [line.split()[0] for line in listing.out.splitlines() if line.strip()]
        f.warn("user", f"user manager is degraded: {plural(len(failed), 'failed unit')}",
               [join_names(failed, 10), f"{host.manager_hint} reset-failed  clears them"])
    elif word:
        f.info("user", f"user manager is {word}")
    else:
        survey.manager_ok = False
        f.fail("user", f"cannot reach {s.user}'s user manager: {state.message()}",
               [f"{host.manager_hint} status  shows what is wrong"] if lingers else [])

    version = host.podman_version()
    if version is None:
        f.fail("podman", f"podman does not run as {s.user}")
    else:
        shown = ".".join(map(str, version))
        if version[:2] < MIN_PODMAN:
            f.fail("podman", f"podman {shown}: Quadlet searches subdirectories only from "
                             f"4.7 on, so installed apps stay invisible to it")
        else:
            f.ok("podman", f"podman {shown}")
    if survey.generator:
        f.ok("podman", f"Quadlet generator {survey.generator}", verbose=True)
    else:
        f.warn("podman", "Quadlet generator not found; the dry runs are skipped",
               ["looked for " + ", ".join(GENERATOR_CANDIDATES)])

    if survey.manager_ok:
        timer = "podman-auto-update.timer"
        enabled = host.systemctl("is-enabled", timer, timeout=30).out.strip() or "missing"
        active = host.systemctl("is-active", timer, timeout=30).out.strip() or "unknown"
        survey.timer_on = enabled == "enabled" and active == "active"
        if survey.timer_on:
            upcoming = host.show([timer], ["NextElapseUSecRealtime"])[timer]
            when = upcoming.get("NextElapseUSecRealtime", "").strip()
            f.ok("update", f"{timer} is on" + (f", next run {when}" if when else "")
                 + f"; {plural(survey.autoupdate, 'unit')} opt in")
        elif survey.autoupdate:
            f.fail("update", f"{plural(survey.autoupdate, 'unit')} set AutoUpdate=, but "
                             f"{timer} is {enabled}/{active}: nothing gets updated",
                   [f"{host.manager_hint} enable --now {timer}"])
        else:
            f.info("update", f"{timer} is {enabled}/{active}, and no unit sets AutoUpdate=")
        if survey.timer_on or survey.autoupdate:
            level, text, lines = last_automatic_run(ctx)
            f.add(level, "update", text, [*lines, "redh update APP  does the same by hand"]
                  if level == "FAIL" else lines)

    if not survey.caddy_bin:
        f.warn("caddy", f"{s.caddy} is not in PATH; the Caddy checks are skipped")
    elif not survey.caddyfile_exists:
        f.warn("caddy", f"{s.caddyfile} not found; cannot tell which snippets Caddy loads")
    else:
        user = survey.caddy_user
        if user and user != "root" and not host.is_root:
            f.info("caddy", f"Caddy runs as {user}; run redh as root to see what {user} can read")
        result, errors, records = caddy.validate(host, survey.caddy_bin, s.caddyfile, user)
        whom = f" as {user}" if user and host.is_root else ""
        if result.ok:
            f.ok("caddy", f"{s.caddyfile} validates{whom}")
        else:
            culprits = sorted({app for app in survey.apps
                               if any(os.path.join(s.root, app) + os.sep in e for e in errors)})
            blame = f" — look at {join_names(culprits)}" if culprits else ""
            f.fail("caddy", f"{s.caddyfile} does not validate{whom}{blame}", errors[-3:])
        me = "root" if host.is_root else s.user
        for record in records:
            pattern = record.get("pattern", "?")
            # An empty snippet directory just means nothing is installed yet: the apps say so.
            if record.get("msg") == caddy.NO_MATCH and \
                    os.path.realpath(os.path.dirname(pattern)) != os.path.realpath(s.caddy_dir):
                _no_match(f, survey, pattern, whom, me)
        if any(ctx.sources(app).snippets for app in survey.apps) and not sites.imported(ctx, survey):
            f.warn("caddy", f"{s.caddyfile} does not import {s.caddy_dir}, where redh installs "
                            f"the snippets", [f"add: {sites.import_line(s)}"])
    for name, (path, source) in sites.installed(s).items():
        if source and not os.path.exists(source):
            f.warn("caddy", f"{path} was installed from {source}, which is gone",
                   [f"rm {path} && systemctl reload {s.caddy_service}"])

    _links(f, ctx.tree_escapes())
    _cross_app(f, ctx, survey)
    perms.check_host(f, ctx, survey)
    envfiles.check_host(f, ctx, survey)
    return f


def blocked_details(survey: Survey, real: str) -> List[str]:
    """Where Caddy's user is stopped. Not opened up: whatever it is moves to /etc/caddy."""
    stop = survey.blocked.get(real)
    if not stop:
        return []
    where, kind = stop
    return [f"{survey.caddy_user} cannot {kind} {where} ({mode_of(where)})"]


def _no_match(f: Findings, survey: Survey, pattern: str, whom: str, me: str) -> None:
    """Caddy said an import glob matched nothing."""
    entry = next((e for e in survey.import_lines
                  if e.pattern == pattern or e.pattern.endswith(os.sep + pattern.lstrip("./"))), None)
    where = f" ({entry.file}:{entry.line})" if entry else ""
    if entry and entry.matches:
        f.fail("caddy", f"import {pattern}{where} matches nothing{whom}, but "
                        f"{plural(len(entry.matches), 'file')} as {me} — their sites are not served",
               (blocked_details(survey, entry.matches[0]) or [join_names(entry.matches, 4)])
               + [f"redh install the apps, and import {survey.caddy_dir}/*.caddy instead"])
    else:
        f.warn("caddy", f"import {pattern}{where} matches no file at all")


def _cross_app(f: Findings, ctx: Context, survey: Survey) -> None:
    s = ctx.settings
    for name, apps in sorted(survey.providers.items()):
        owners = sorted(set(apps))
        if len(owners) > 1:
            f.fail("apps", f"{name} exists in {join_names(owners)}; Quadlet loads only one of them")
    for service, owners in sorted(survey.services.items()):
        if len({fn for _, fn in owners}) > 1:
            makers = ", ".join(f"{fn} ({app})" for app, fn in sorted(owners))
            f.fail("apps", f"{service} is generated twice: {makers}")

    pubs = survey.publishers
    for i, a in enumerate(pubs):
        for b in pubs[i + 1:]:
            if (a.app, a.file) == (b.app, b.file) or a.proto != b.proto:
                continue
            overlap = a.low <= b.high and b.low <= a.high
            shared = (caddy.all_interfaces(a.address) or caddy.all_interfaces(b.address)
                      or a.address == b.address)
            if overlap and shared:
                f.fail("apps", f"port {max(a.low, b.low)} is published twice: "
                               f"{a.file} ({a.app}) and {b.file} ({b.app})")

    for app in ctx.installed_apps():
        if not ctx.sources(app).exists:
            what = [f"{s.install_dir(app)}/"] if ctx.installed(app).exists else []
            what += [f"{s.user_unit_dir}/{u.name}" for u in ctx.user_units(app)]
            f.warn("apps", f"{join_names(what, 3)} {'is' if len(what) == 1 else 'are'} "
                           f"installed, but {s.app_dir(app)} is gone",
                   [f"redh prune {app}  stops and removes what is left"])
    try:
        os.close(confine.open_dir(s, s.user_unit_dir))
    except Escape as escape:
        f.fail("symlink", str(escape), [f"redh installs no plain unit there until "
                                        f"{s.user_unit_dir} is a directory of {s.user}'s own"])
    except OSError:
        pass                                  # not there yet: install makes it

    loose = [rel for rel in ctx.tree() if os.sep not in rel and quadlet_ext(rel)]
    unowned = [rel for rel in loose if rel not in survey.providers]
    if unowned:
        f.info("apps", f"{plural(len(unowned), 'unit file')} in {s.quadlet_dir} belong to no app",
               [join_names(unowned, 10)])
    global_dirs = sorted({rel.split(os.sep)[0] for rel in ctx.tree()
                          if os.sep in rel and is_global_dropin_dir(rel.split(os.sep)[0])})
    for name in global_dirs:
        f.info("apps", f"{s.quadlet_dir}/{name}/ applies to every {name[:-2]} of every app")

    if survey.apps:
        f.ok("apps", f"{plural(len(survey.apps), 'app')} under {s.root}", verbose=True)
    else:
        f.info("apps", f"no apps under {s.root}")


# -- one app ----------------------------------------------------------------

def check_app(ctx: Context, app: str, survey: Survey) -> Findings:
    f = Findings()
    src, inst = ctx.sources(app), ctx.installed(app)
    if not src.exists:
        if inst.exists:
            f.fail("layout", f"{src.dir} is gone, but {inst.dir}/ is still installed",
                   [f"redh prune {app}  removes the rest"])
        else:
            f.fail("layout", f"no such app: {src.dir}")
        return f

    _links(f, src.escapes)
    if src.linked_out:
        return f
    if (src.layout == "none" and src.snippet_layout == "none" and src.user_layout == "none"
            and not src.user_misnamed and not inst.exists and not ctx.user_units(app)):
        f.info("layout", f"no {ctx.settings.quadlet_subdir}/, unit files or Caddy snippet here — "
                         f"not an app? (ignore= in the settings hides it)")
        perms.check_app(f, ctx, survey, app)
        envfiles.check_app(f, ctx, survey, app)     # secrets lie around in those too
        return f

    plan = make_plan(ctx, app)
    _layout(f, ctx, src)
    _placeholders(f, ctx, src)
    if src.units:
        _units(f, ctx, app, src, survey)
        _generator(f, ctx, app, plan, survey)
        _update(f, src, survey)
        _local_images(f, ctx, app, src, survey)
        _boot(f, src)
    _caddy(f, ctx, app, src, survey)
    perms.check_app(f, ctx, survey, app)
    envfiles.check_app(f, ctx, survey, app)
    if src.units:
        _installed(f, plan)
    _site(f, ctx, app)
    _plain(f, ctx, app, src)
    if src.units:
        _runtime(f, ctx, app, survey)
    return f


def preflight(ctx: Context, plan: Plan, survey: Optional[Survey] = None) -> Findings:
    """The checks an install depends on: symlinks, placeholders, unit files, the generator,
    what it would replace."""
    survey = survey or Survey(ctx)
    f = Findings()
    _links(f, plan.src.escapes)
    _placeholders(f, ctx, plan.src)
    if plan.src.units:
        _units(f, ctx, plan.app, plan.src, survey)
        _generator(f, ctx, plan.app, plan, survey)
    for path, owner in plan.conflicts:
        f.fail("install", f"{path} has the name of one of {plan.app}'s files but is "
                          f"installed for {owner}", [_conflict_hint(owner)])
    for path, source, same in plan.legacy:
        if not same:
            f.fail("install", f"old copy {path} differs from the source",
                   [f"diff {source} {path}", "--force replaces it anyway"])
    return f


def _links(f: Findings, escapes: Iterable[Escape]) -> None:
    """Symlinks that would make root read something the podman user has no business with."""
    for escape in escapes:
        f.fail("symlink", str(escape),
               ["root reads what is there, so redh does not follow it: remove the link, or keep "
                f"the file below {escape.top}"])


def _conflict_hint(owner: str) -> str:
    return f"rename one of them, or remove the stale install: redh uninstall {owner}"


def _mv_hint(paths: List[str], target: str) -> List[str]:
    return [f"mkdir -p {target} && mv -n {' '.join(paths)} {target}/"]


def _layout(f: Findings, ctx: Context, src: Sources) -> None:
    s = ctx.settings
    qdir = os.path.join(src.dir, s.quadlet_subdir)
    units = len(src.units) + len(src.unreadable)
    sdir = os.path.join(src.dir, s.systemd_subdir)
    if src.layout == "none" and (src.user_units or src.user_misnamed):
        f.info("layout", "no Quadlet unit files, only plain units", verbose=True)
    elif src.layout == "none":
        f.fail("layout", f"no Quadlet unit files; they belong in {qdir}/")
    elif src.layout == "top level":
        f.warn("layout", f"{plural(units, 'unit file')} at the top level of {src.dir}; "
                         f"the convention is {s.quadlet_subdir}/",
               _mv_hint(sorted({os.path.join(src.dir, rel.split(os.sep)[0]) for rel in src.files}), qdir))
    elif not units:
        f.fail("layout", f"{qdir}/ holds no unit files")
    else:
        others = len(src.files) - units
        f.ok("layout", f"{s.quadlet_subdir}/ holds {plural(units, 'unit file')}"
                       + (f" and {plural(others, 'other file')}" if others else ""))
    if src.stray:
        f.warn("layout", f"{join_names(os.path.basename(p) for p in src.stray)} outside "
                         f"{s.quadlet_subdir}/ {'is' if len(src.stray) == 1 else 'are'} ignored",
               _mv_hint(src.stray, qdir))
    if src.plain_units:
        f.warn("layout", f"{join_names(src.plain_units, 4)}: plain systemd "
                         f"{'units' if len(src.plain_units) > 1 else 'unit'}, which Quadlet ignores",
               [f"they belong in {s.systemd_subdir}/, which redh installs into "
                f"{s.user_unit_dir}:"]
               + _mv_hint([os.path.join(src.base or src.dir, rel) for rel in src.plain_units], sdir))
    if src.user_misnamed:
        f.warn("layout", f"{src.user_misnamed}/ looks like {s.systemd_subdir}/, but redh reads "
                         f"only {s.systemd_subdir}/: its units are not installed",
               [f"mv {os.path.join(src.dir, src.user_misnamed)} {sdir}"])
    if src.user_stray:
        f.warn("layout", f"{join_names(src.user_stray, 4)} in {s.systemd_subdir}/: not unit files, "
                         f"so not installed", ["only *.service, *.timer and the like belong there"])
    for rel, error in src.unreadable:
        f.fail("units", f"cannot read {rel}: {error}")


def marked(name: str, lines: Iterable[str]) -> List[str]:
    """'quadlet/x.container:9  the image' for each line marked as a placeholder."""
    left: List[str] = []
    for number, line in enumerate(lines, 1):
        if PLACEHOLDER in line:
            what = line.split(PLACEHOLDER, 1)[1].lstrip(" :").strip()
            left.append(f"{name}:{number}  {what}".rstrip())
    return left


def placeholders(ctx: Context, src: Sources) -> List[str]:
    """What redh init left to fill in, in the app's Quadlet files and snippets."""
    left: List[str] = []
    for path in sorted(set(src.files.values()) | set(src.snippets)):
        try:
            with confine.open_file(ctx.settings, path) as fh:
                if os.fstat(fh.fileno()).st_size > 1 << 20:
                    continue
                text = fh.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        left += marked(os.path.relpath(path, src.dir), text.splitlines())
    return left


def _placeholders(f: Findings, ctx: Context, src: Sources) -> None:
    left = placeholders(ctx, src)
    if left:
        f.fail("init", f"{plural(len(left), 'line')} marked {PLACEHOLDER} still to fill in",
               left + ["fill them in, then delete the TODO comments"])


def _units(f: Findings, ctx: Context, app: str, src: Sources, survey: Survey) -> None:
    s = ctx.settings
    before = len(f)
    mine = {u.filename for u in src.units}
    install_dir = s.install_dir(app)
    installed_with_it = set(src.files.values())

    for u in src.units:
        if not u.ini.has(u.section):
            f.fail("units", f"{u.filename} has no [{u.section}] section")
        if u.ext == ".container" and not (u.get("Image") or u.get("Rootfs")):
            f.fail("units", f"{u.filename} sets no Image=")

        for written, source in u.bind_sources():
            if source.startswith("."):
                here = os.path.dirname(u.path)
                target = os.path.normpath(os.path.join(
                    install_dir, os.path.relpath(here, src.base), source))
                meant = os.path.normpath(os.path.join(here, source))
                f.fail("units", f"{u.filename}: {written} is relative",
                       [f"Quadlet resolves it against the installed copy: {target}",
                        f"write it absolute: {meant}"])
            elif "%" not in source and not os.path.exists(source):
                f.fail("units", f"{u.filename}: {written} — {source} does not exist",
                       ["podman refuses to start a container whose bind mount source is missing"])

        for env in u.env_files():
            if "%" in env:
                continue
            path = env if os.path.isabs(env) else os.path.normpath(
                os.path.join(os.path.dirname(u.path), env))
            if not os.path.isfile(path):
                f.fail("units", f"{u.filename}: EnvironmentFile={env} not found")
                continue
            if not os.path.isabs(env) and path not in installed_with_it:
                f.fail("units", f"{u.filename}: EnvironmentFile={env} is relative, but "
                                f"{path} is not installed with the unit",
                       ["Quadlet resolves it against the installed copy; write it absolute, "
                        f"or keep it in {s.quadlet_subdir}/"])

        for key, target in u.references():
            if target in mine:
                continue
            providers = sorted(set(survey.providers.get(target, [])) - {app})
            if providers:
                f.info("units", f"{u.filename}: {key}={target} comes from {join_names(providers)}")
            else:
                f.fail("units", f"{u.filename}: {key}={target}, but no app has {target}")

    for rel in src.files:
        top = rel.split(os.sep)[0]
        if os.sep in rel and is_global_dropin_dir(top):
            f.warn("units", f"{top}/ applies to every {top[:-2]} of every app, not just {app}",
                   [f"use {app}-specific names like <unit>.{top[:-2]}.d/"])
            break

    if len(f) == before or all(x.level in ("ok", "info") for x in f[before:]):
        kinds: Dict[str, int] = {}
        for u in src.units:
            kinds[u.kind] = kinds.get(u.kind, 0) + 1
        f.ok("units", ", ".join(plural(n, k) for k, n in kinds.items()), verbose=True)


GEN_PREFIX = re.compile(r"^quadlet-generator\[\d+\]:\s*")
GEN_PROBLEM = re.compile(r"error|warning|unsupported|not found|invalid|failed|"
                         r"converting|cannot|can't|missing|requires|not allowed", re.I)


def mentions(message: str, filename: str) -> bool:
    return re.search(r"(^|[\s\"'/])" + re.escape(filename) + r"($|[\s\"':,])", message) is not None


def generator_input(rel: str) -> bool:
    """Unit files and drop-ins: all the generator reads. Env files stay out of /tmp."""
    parts = rel.split(os.sep)
    if len(parts) > 1 and parts[-2].endswith(".d"):
        return parts[-1].endswith(".conf")
    return quadlet_ext(parts[-1]) is not None


def stage_for_generator(ctx: Context, plan: Plan) -> Tuple[str, str, str]:
    """Lay out the Quadlet directory as it will be after installing plan.

    Returns (stage, the app's files, everything else); the caller removes stage.
    """
    host = ctx.host
    stage = tempfile.mkdtemp(prefix="redh-quadlet-")
    new, rest = os.path.join(stage, "new"), os.path.join(stage, "rest")
    leaving = {path for path, _, _ in plan.legacy}
    copies = [(source, os.path.join(new, rel)) for rel, source in plan.src.files.items()
              if generator_input(rel)]
    copies += [(path, os.path.join(rest, rel)) for rel, path in ctx.tree().items()
               if rel.split(os.sep)[0] != plan.app and path not in leaving
               and generator_input(rel)]
    os.makedirs(new)
    os.makedirs(rest)
    for source, dest in copies:
        try:
            content = confine.read_bytes(ctx.settings, source)
        except OSError:
            continue                  # gone, or a symlink by now: install refuses that itself
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(content)
        os.chmod(dest, 0o600)
    # Bottom up, the stage itself last: until then the podman user cannot get in to swap a
    # directory for a symlink, and send a chown somewhere else.
    for dirpath, dirnames, filenames in os.walk(stage, topdown=False):
        for name in filenames:
            host.chown(os.path.join(dirpath, name))
        host.chown(dirpath)
    return stage, new, rest


def _generator(f: Findings, ctx: Context, app: str, plan: Plan, survey: Survey) -> None:
    src = plan.src
    if not survey.generator:
        return
    stage, new, rest = stage_for_generator(ctx, plan)
    try:
        result: Result = ctx.host.generator_dryrun([new, rest])
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    if result.rc in (124, 127) or (not result.out and not result.err):
        f.warn("generator", f"could not run the Quadlet generator: {result.message()}")
        return

    produced = {os.path.basename(m) for m in re.findall(r"^---(.+?)---$", result.out, re.M)}
    messages = []
    for line in result.err.splitlines():
        # Show the paths people know, not the temporary ones.
        line = GEN_PREFIX.sub("", line.strip())
        line = line.replace(new, src.base or new).replace(rest, ctx.settings.quadlet_dir)
        if line and GEN_PROBLEM.search(line) and any(mentions(line, u.filename) for u in src.units):
            messages.append(line)

    used: Set[str] = set()
    missing = [u for u in src.units if u.service not in produced]
    for u in missing:
        about = [m for m in messages if mentions(m, u.filename)]
        used.update(about)
        f.fail("generator", f"{u.filename} does not generate {u.service}",
               about or ["the generator gave no reason"])
    for message in messages:
        if message not in used:
            f.warn("generator", message)
    if not missing:
        services = [u.service for u in src.units]
        which = f"all {len(services)}" if len(services) > 1 else "it"
        f.ok("generator", f"Quadlet accepts {which}: {join_names(services, 5)}")


def fully_qualified(image: str) -> bool:
    """registry.example/x, localhost/x — what 'podman auto-update' insists on."""
    if "/" not in image:
        return False
    first = image.split("/", 1)[0]
    return "." in first or ":" in first or first == "localhost"


def _update(f: Findings, src: Sources, survey: Survey) -> None:
    by_name = {u.filename: u for u in src.units}
    on: List[str] = []
    for u in src.units:
        if u.ext not in (".container", ".kube"):
            continue
        policies = u.get_all("AutoUpdate")
        if u.ext == ".kube":
            if policies:
                on.append(u.filename)
            else:
                f.warn("update", f"{u.filename}: no AutoUpdate=, so its images are never updated")
            continue
        image = u.get("Image") or ""
        policy = policies[-1] if policies else None
        if policy is None:
            f.warn("update", f"{u.filename}: no AutoUpdate=, so {image or 'its image'} is never updated")
            continue
        if policy not in ("registry", "local"):
            f.fail("update", f"{u.filename}: AutoUpdate={policy} — only registry and local exist")
            continue
        if image.endswith(".image"):
            if image not in by_name:          # another app's .image; checked there
                on.append(u.filename)
                continue
            image = by_name[image].get("Image") or image
        if image.endswith(".build"):
            if policy == "registry":
                f.fail("update", f"{u.filename}: AutoUpdate=registry on an image built by {image}",
                       ["there is no registry to check; use AutoUpdate=local"])
                continue
        elif policy == "registry" and image and not fully_qualified(image):
            f.fail("update", f"{u.filename}: AutoUpdate=registry needs a fully-qualified image, not {image}",
                   [f"e.g. docker.io/library/{image}" if "/" not in image else
                    f"e.g. docker.io/{image}"])
            continue
        elif policy == "registry" and "@sha256:" in image:
            f.warn("update", f"{u.filename}: {image} is pinned by digest, so AutoUpdate=registry never changes it")
            continue
        on.append(u.filename)
        f.ok("update", f"{u.filename}: {policy}, {image}", verbose=True)
    if on:
        if survey.timer_on is False:
            f.fail("update", "AutoUpdate= is set, but podman-auto-update.timer is off (see host)")
        else:
            f.ok("update", f"{plural(len(on), 'unit')} auto-update: {join_names(on, 4)}")


def full_ref(image: str) -> str:
    """localhost/x -> localhost/x:latest, which is what podman makes of a name without a tag."""
    last = image.rsplit("/", 1)[-1]
    return image if ":" in last or "@" in last else image + ":latest"


def _local_images(f: Findings, ctx: Context, app: str, src: Sources, survey: Survey) -> None:
    """Containers that run an image only this host has: what builds it, and is it there."""
    user, hint = ctx.settings.user, ctx.host.manager_hint
    for u in src.units:
        image = (u.get("Image") or "") if u.ext == ".container" else ""
        if not image.startswith("localhost/"):
            continue
        builder = survey.builders.get(full_ref(image))
        if builder:
            owner, build = builder
            where = build.filename if owner == app else f"{build.filename} ({owner})"
            f.warn("image", f"{u.filename}: Image={image} is what {where} builds, but Quadlet "
                            f"only builds it before the container starts with Image={build.filename}",
                   [f"Image={build.filename}"])
            again = [f"{hint} start {build.service}  builds it again"]
        else:
            f.warn("image", f"{u.filename}: {image} was built by hand; nothing builds it again "
                            f"once it is gone (podman image prune -a, a storage reset)",
                   [f"a .build unit builds it whenever it is missing — "
                    f"quadlet/{u.stem}.build:",
                    "  [Build]",
                    f"  ImageTag={image}",
                    f"  File={src.dir}/<dir>/Containerfile",
                    "  SetWorkingDirectory=file",
                    f"then in {u.filename}: Image={u.stem}.build"])
            again = [f"build it again as {user}: cd <dir with the Containerfile> && "
                     f"runuser -u {user} -- podman build -t {image} ."]
        exists = ctx.host.podman("image", "exists", image, timeout=60)
        if exists.rc == 1:
            f.fail("image", f"{image} is not in {user}'s image store, so {u.service} "
                            f"cannot start",
                   ["podman then tries to pull it from a registry at https://localhost "
                    "and fails with a TLS or connection error"] + again)
        elif not exists.ok:
            f.warn("image", f"cannot tell whether {image} exists: {exists.message()}")


def _boot(f: Findings, src: Sources) -> None:
    started: List[str] = []
    for u in src.units:
        targets = u.install_targets()
        wrong = [t for t in targets if t in SYSTEM_ONLY_TARGETS]
        if wrong:
            f.warn("boot", f"{u.filename}: WantedBy={wrong[0]} does not exist for a user manager",
                   ["use WantedBy=default.target"])
        if u.is_template and targets and not u.get("DefaultInstance", "Install"):
            f.info("boot", f"{u.filename} is a template without DefaultInstance=; "
                           f"only its instances start")
        if u.ext not in (".container", ".kube", ".pod") or u.is_template:
            continue
        if u.ext == ".container" and u.get("Pod"):
            continue                          # its pod starts it
        if any(t not in SYSTEM_ONLY_TARGETS for t in targets):
            started.append(u.filename)
        elif not wrong:
            f.warn("boot", f"{u.filename} has no [Install] section, so it does not start at boot",
                   ["[Install]", "WantedBy=default.target"])
    if started:
        f.ok("boot", f"starts at boot: {join_names(started, 4)}", verbose=True)


def _caddy(f: Findings, ctx: Context, app: str, src: Sources, survey: Survey) -> None:
    s = ctx.settings
    cdir = os.path.join(src.dir, s.caddy_subdir)
    if src.snippet_layout == "none":
        f.warn("caddy", f"no snippet in {s.caddy_subdir}/ — fine if {app} has no web UI")
        return
    if src.snippet_layout == "top level":
        f.warn("caddy", f"snippet at the top level of {src.dir}; the convention is {s.caddy_subdir}/",
               _mv_hint(src.snippets, cdir) + [f"then: redh install {app}"])
    if src.stray_snippets:
        f.warn("caddy", f"{join_names(os.path.basename(p) for p in src.stray_snippets)} "
                        f"outside {s.caddy_subdir}/ is ignored", _mv_hint(src.stray_snippets, cdir))

    publishers = [p for p in survey.publishers if p.app == app]
    host_network = any(u.uses_host_network() for u in ctx.units(app))
    warned: Set[str] = set()
    site = sites.plan(ctx, app)
    installed_as = {source: name for name, source in site.wanted.items()}
    for path in src.snippets:
        real = os.path.realpath(path)
        name = os.path.relpath(path, src.dir)
        if survey.caddyfile_exists:
            _snippet_import(f, ctx, app, src, survey, path, installed_as.get(path))
        if survey.caddy_bin:
            result = caddy.adapt_snippet(ctx.host, survey.caddy_bin, s.caddyfile, real,
                                         survey.snippets, s.caddy_dir, s)
            if result.ok:
                f.ok("caddy", f"{name} adapts cleanly {result.mode}")
            elif result.mode == "on its own":
                f.warn("caddy", f"{name} does not adapt on its own (it may need the main Caddyfile)",
                       result.errors)
            else:
                f.fail("caddy", f"{name} does not adapt {result.mode}", result.errors)
            for warning in result.warnings:
                f.warn("caddy", warning)

        try:
            text = confine.read_text(s, path)
        except OSError as exc:
            f.fail("caddy", f"cannot read {path}: {exc.strerror}")
            continue
        for line, host, port in caddy.proxy_targets(text):
            if not caddy.is_loopback(host):
                continue
            where = f"{name}:{line}"
            mine = [p for p in publishers if p.covers(port)]
            for p in mine:
                if caddy.all_interfaces(p.address) and p.spec not in warned:
                    warned.add(p.spec)
                    f.warn("caddy", f"{p.file}: PublishPort={p.spec} listens on every interface, "
                                    f"so port {port} is reachable around Caddy",
                           [f"PublishPort={p.local_spec()} keeps it local"])
            if mine:
                f.ok("caddy", f"{where} → port {port}, published by {mine[0].file}", verbose=True)
            elif host_network:
                f.info("caddy", f"{where} → port {port}: a host-network container may serve it",
                       verbose=True)
            else:
                others = [p for p in survey.publishers if p.app != app and p.covers(port)]
                if others:
                    f.info("caddy", f"{where} → port {port}, published by {others[0].app}")
                else:
                    f.warn("caddy", f"{where} proxies to {host or 'localhost'}:{port}, "
                                    f"but no container publishes port {port}")


def _import_hint(survey: Survey, caddyfile: str, real: str, caddy_dir: str) -> List[str]:
    """What the Caddyfile does import, so a pattern that misses is easy to spot."""
    lines = [f"{e.file}:{e.line}: import {e.pattern}  ({plural(len(e.matches), 'match', 'es')})"
             for e in survey.import_lines[:8]]
    details = (["the imports there:"] + ["  " + line for line in lines]) if lines else \
        [f"{caddyfile} has no file imports"]
    return details + [f"add: import {caddy_dir}/*.caddy"]


def _snippet_import(f: Findings, ctx: Context, app: str, src: Sources, survey: Survey,
                    path: str, installed_name: Optional[str]) -> None:
    """Where Caddy reads the snippet from: its installed copy (the convention), or in place."""
    s = ctx.settings
    real = os.path.realpath(path)
    name = os.path.relpath(path, src.dir)
    copy = os.path.join(s.caddy_dir, installed_name) if installed_name else None
    entry = survey.imports.get(real)
    if entry is None:
        if copy and os.path.realpath(copy) in survey.imports:
            f.ok("caddy", f"{name} is installed as {copy}, which Caddy imports", verbose=True)
        elif not sites.imported(ctx, survey):
            f.warn("caddy", f"{name} is not imported by {s.caddyfile}, so Caddy never loads it",
                   _import_hint(survey, s.caddyfile, real, s.caddy_dir)
                   + [f"then: redh install {app}"])
        # else: Caddy imports the directory, only the copy is missing — the install topic says so.
        return
    user = survey.caddy_user
    f.fail("caddy", f"Caddy reads {name} straight from {src.dir}, but nothing below "
                    f"{s.root} is Caddy's",
           [f"redh install {app}  puts it into {s.caddy_dir}; then import that in "
            f"{s.caddyfile} instead of {entry.pattern}: {sites.import_line(s)}"])
    if survey.service_imports is not None and real not in survey.service_imports:
        f.fail("caddy", f"{name}: Caddy runs as {user} and cannot reach it, "
                        f"so its sites are not served",
               blocked_details(survey, real)
               + [f"imported by {entry.file}:{entry.line}: import {entry.pattern}"])


def _site(f: Findings, ctx: Context, app: str) -> None:
    """The app's snippets in the Caddy snippet directory."""
    s = ctx.settings
    site = sites.plan(ctx, app)
    src, inst = ctx.sources(app), ctx.installed(app)
    for name, owner in site.conflicts:
        f.fail("install", f"{s.caddy_dir}/{name} belongs to {owner}, not {app}",
               ["rename one of the snippets, or remove that file"])
    if not site.wanted:
        return
    if site.add and not site.same and not site.change:
        if src.units and not inst.exists:
            return                    # 'not installed' says it already, for the snippet too
        f.warn("install", f"Caddy snippet not installed → redh install {app}", site.lines(s))
    elif not site.in_sync:
        f.warn("install", f"Caddy snippet in {s.caddy_dir} is out of date → redh install {app}",
               site.lines(s))
    else:
        f.ok("install", f"Caddy snippet installed: {join_names(site.same, 3)}", verbose=True)


def _plain(f: Findings, ctx: Context, app: str, src: Sources) -> None:
    """The app's plain units: systemd's own verdict, the installed copies, how they run."""
    s, host = ctx.settings, ctx.host
    installed = ctx.user_units(app)
    if not src.user_units and not installed:
        return
    if src.user_units:
        names, paths = list(src.user_units), sorted(src.user_units.values())
        # As the podman user: %h is its home, and ExecStart= has to be its to run.
        result = host.run(["systemd-analyze", "--user", "verify", *paths], as_user=True, timeout=120)
        said = [line.strip() for line in (result.err + result.out).splitlines()
                if line.strip() and any(n in line for n in names)]
        if result.rc == 127:
            f.info("units", "systemd-analyze not found: the plain units are not verified")
        elif said:
            f.fail("units", f"systemd-analyze finds fault with {s.systemd_subdir}/", said[:8])
        elif not result.ok:                   # about something else on the host, it seems
            f.warn("units", f"systemd-analyze verify failed: {result.message()}")
        else:
            f.ok("units", f"systemd accepts {join_names(names, 4)}", verbose=True)
        for name, path in sorted(src.user_units.items()):
            try:
                unit = PlainUnit.load(path, confine.reader(s))
            except OSError:
                continue
            wrong = [t for t in unit.install_targets() if t in SYSTEM_ONLY_TARGETS]
            if wrong:
                f.warn("boot", f"{s.systemd_subdir}/{name}: WantedBy={wrong[0]} does not exist for "
                               f"a user manager", ["timers.target for a timer, else default.target"])

    units = userunits.plan(ctx, app)
    for name, why in units.conflicts:
        f.fail("install", f"{s.systemd_subdir}/{name}: {why}")
    if units.wanted and units.add and not units.same and not units.change:
        if not (src.units and not ctx.installed(app).exists):   # 'not installed' says it
            f.warn("install", f"plain units not installed → redh install {app}", units.lines(s))
    elif not units.in_sync:
        f.warn("install", f"plain units in {s.user_unit_dir} are out of date → redh install {app}",
               units.lines(s))
    elif units.same:
        f.ok("install", f"plain units installed: {join_names(units.same, 4)}", verbose=True)

    if not installed:
        return
    try:
        states = host.show([u.name for u in installed], STATE_PROPS + ("UnitFileState",))
    except HostError as exc:
        f.warn("runtime", str(exc))
        return
    quiet = True
    for u in installed:
        state = states[u.name]
        if u.starts and state.get("UnitFileState") not in ("enabled", "enabled-runtime"):
            quiet = False
            f.warn("runtime", f"{u.name} is not enabled, so it does not start at boot",
                   [f"redh install {app}  enables it"])
        if state.get("ActiveState") == "failed":
            quiet = False
            f.warn("runtime", f"{u.name}: {lifecycle.describe(state)}",
                   host.journal(u.name, since=0, lines=6))
    idle = [u.name for u in installed if u.starts and states[u.name].get("ActiveState") != "active"]
    if idle:
        f.info("runtime", f"not running: {join_names(idle, 4)} → redh start {app}")
    elif quiet:
        f.ok("runtime", f"plain units: {join_names([u.name for u in installed if u.starts], 4)} "
                        f"active", verbose=True)


def _installed(f: Findings, plan: Plan) -> None:
    inst = plan.inst
    for path, owner in plan.conflicts:
        f.fail("install", f"{path} is installed for {owner} under the name of one of "
                          f"{plan.app}'s files", [_conflict_hint(owner)])
    for path, _, same in plan.legacy:
        what = "shadows the installed one" if inst.exists else "is what systemd runs now"
        f.warn("install", f"old copy {path} {what}"
                          + ("" if same else " — and differs from the source"),
               [f"redh install {plan.app}  replaces it"])
    if not inst.exists:
        f.warn("install", f"not installed → redh install {plan.app}")
    elif plan.add or plan.change or plan.remove:
        changes = [line for line in plan.lines() if not line.startswith("x ")]
        f.warn("install", f"{inst.dir}/ is out of date → redh install {plan.app}", changes)
    else:
        f.ok("install", f"{inst.dir}/ matches {plan.src.layout}")


def _runtime(f: Findings, ctx: Context, app: str, survey: Survey) -> None:
    inst = ctx.installed(app)
    names = inst.services()
    if not survey.manager_ok or not names:
        return
    try:
        states = ctx.host.show(names)
    except HostError as exc:
        f.warn("runtime", str(exc))
        return
    not_loaded = [n for n in names if states[n].get("LoadState") != "loaded"]
    failed = [n for n in names if states[n].get("ActiveState") == "failed"]
    # Tasks (builds, volumes) are inactive once they ran; only their failure counts.
    tasks = [n for n in names if is_task(states[n]) and n not in failed]
    names = [n for n in names if n not in tasks]
    active = [n for n in names if states[n].get("ActiveState") == "active"]
    done = f", {plural(len(tasks), 'task')} ({join_names(tasks, 3)})" if tasks else ""
    if not_loaded:
        f.fail("runtime", f"not loaded: {join_names(not_loaded)}",
               ["the generator rejected them, or daemon-reload is pending: "
                f"redh install {app}"])
    if failed:
        f.warn("runtime", f"failed: {join_names(failed)}", [f"redh status {app}"])
    if len(active) == len(names):
        f.ok("runtime", f"all {plural(len(names), 'unit')} active{done}")
    elif not failed and not not_loaded:
        stopped = [n for n in names if n not in active]
        f.info("runtime", f"{len(active)}/{len(names)} units active; "
                          f"not running: {join_names(stopped, 4)}")


# -- the command ------------------------------------------------------------

def run_check(ctx: Context, apps: List[str]) -> int:
    out = ctx.out
    survey = Survey(ctx)
    everything = Findings()

    host_findings = check_host(ctx, survey)
    out.heading("host")
    show(out, host_findings)
    everything.extend(host_findings)

    for app in apps:
        out.write()
        out.heading(app)
        findings = check_app(ctx, app, survey)
        show(out, findings)
        everything.extend(findings)

    fails, warns = everything.count("FAIL"), everything.count("warn")
    out.write()
    verdict = "FAIL" if fails else ("warn" if warns else "ok")
    out.status(verdict, f"{plural(len(apps), 'app')} checked: "
                        f"{plural(fails, 'problem')}, {plural(warns, 'warning')}", indent=0)
    return 1 if fails else 0
