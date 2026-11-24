"""Installing an app's units into the Quadlet directory, and taking them out again.

Install mirrors the app's quadlet/ directory into <Quadlet dir>/<app>/:
new and changed files are written atomically and handed to the podman user,
files that disappeared from the source are removed, and old copies of the
app's files elsewhere in the Quadlet directory (from installing by hand) go
too. Then the user manager reloads, and every service the app should have is
checked to actually exist.
"""

from __future__ import annotations

import os
from contextlib import suppress
from typing import List, Optional

from . import confine, lifecycle, sites, userunits
from .app import Context
from .checks import Survey, preflight, show
from .common import join_names, plural
from .plan import Plan, copies_elsewhere, dest_mode, make_plan


def copy_file(ctx: Context, source: str, base: str, rel: str) -> None:
    """source to base/rel, as the podman user's file with the source's mode.

    Read and written through descriptors (see confine): the podman user owns
    both sides, and a symlink planted on either must not send root elsewhere.
    """
    host = ctx.host
    directory = confine.directory(ctx.settings, base, os.path.dirname(rel), host.chown_fd)
    try:
        with confine.open_file(ctx.settings, source) as fh:
            mode = dest_mode(os.fstat(fh.fileno()).st_mode)
            confine.write_file(directory, os.path.basename(rel), fh, mode, host.chown_fd)
    finally:
        os.close(directory)


def remove_file(ctx: Context, path: str) -> None:
    """Delete one file (or a link itself) in the podman user's directories."""
    directory = confine.open_dir(ctx.settings, os.path.dirname(path))
    try:
        os.unlink(os.path.basename(path), dir_fd=directory)
    finally:
        os.close(directory)


def apply_plan(ctx: Context, plan: Plan) -> List[str]:
    """Carry the plan out; returns what went wrong (nothing, ideally)."""
    s = ctx.settings
    # From the Quadlet directory down, every directory has to be a real one: not a
    # link to elsewhere in the home, like the app's own sources.
    qdir, app = os.path.split(plan.inst.dir)
    errors = []
    for rel in plan.add + plan.change:
        try:
            copy_file(ctx, plan.src.files[rel], qdir, os.path.join(app, rel))
        except OSError as exc:
            errors.append(f"{rel}: {exc.strerror or exc}")
    for rel in plan.remove:
        try:
            parent = confine.directory(s, qdir, os.path.join(app, os.path.dirname(rel)),
                                       create=False)
            try:
                os.unlink(os.path.basename(rel), dir_fd=parent)
            finally:
                os.close(parent)
        except OSError as exc:
            errors.append(f"{rel}: {exc.strerror or exc}")
    if plan.remove:
        with suppress(OSError):
            top = confine.directory(s, qdir, app, create=False)
            try:
                confine.remove_empty(top)
            finally:
                os.close(top)
    for path, _, _ in plan.legacy:
        try:
            remove_file(ctx, path)
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror or exc}")
        if os.path.dirname(path).endswith(".d"):
            with suppress(OSError):
                parent = confine.open_dir(s, os.path.dirname(os.path.dirname(path)))
                try:
                    os.rmdir(os.path.basename(os.path.dirname(path)), dir_fd=parent)
                finally:
                    os.close(parent)
    return errors


def reload(ctx: Context) -> bool:
    result = ctx.host.daemon_reload()
    if result.ok:
        ctx.out.status("ok", "daemon-reload")
    else:
        ctx.out.status("FAIL", f"daemon-reload: {result.message()}")
    ctx.forget()
    return result.ok


def install_app(ctx: Context, app: str, survey: Optional[Survey],
                dry_run: bool, force: bool, restart: bool) -> bool:
    out, host, s = ctx.out, ctx.host, ctx.settings
    survey = survey or Survey(ctx)
    plan = make_plan(ctx, app)
    site = sites.plan(ctx, app)
    units = userunits.plan(ctx, app)
    src = plan.src
    out.heading(f"install {app}")
    if not src.exists:
        out.status("FAIL", f"no such app: {src.dir}")
        return False
    if (not src.units and not src.snippets and not src.user_units and site.in_sync
            and units.in_sync and not src.escapes):
        out.status("FAIL", f"nothing to install: no Quadlet unit files, no units in "
                           f"{s.systemd_subdir}/ and no Caddy snippet in {src.dir}")
        return False
    if src.units:
        out.write(out.dim(f"  {src.base}/ → {plan.inst.dir}/"))
        for line in plan.lines():
            out.write(f"  {line}")
        if plan.same:
            out.write(out.dim(f"  = {plural(len(plan.same), 'file')} unchanged"))
    for line in units.lines(s) + site.lines(s):
        out.write(f"  {line}")

    findings = preflight(ctx, plan, survey)
    for name, why in units.conflicts:
        findings.fail("install", f"{s.systemd_subdir}/{name}: {why}",
                      [f"rename {s.systemd_subdir}/{name}" if "Quadlet" in why or "belongs" in why
                       else f"look at {s.user_unit_dir}/{name}, and remove it if it can go"])
    for name, owner in site.conflicts:
        findings.fail("install", f"{s.caddy_dir}/{name} belongs to {owner}, not {app}",
                      ["rename one of the snippets, or remove that file"])
    show(out, [f for f in findings if f.level != "ok" or f.topic == "generator"])
    # Not for --force to overrule: another app's files, or something only root may read.
    conflicts = plan.conflicts or site.conflicts or units.conflicts or src.escapes
    if conflicts or (findings.failed and not force):
        out.status("FAIL", "nothing installed" + ("" if conflicts else
                                                  " — fix the above, or pass --force"))
        return False

    if dry_run:
        out.status("skip", "dry run: nothing written")
        return not findings.failed

    if not src.units and not units.wanted and units.in_sync:
        if site.in_sync:
            out.status("ok", "already up to date")
        return sites.apply(ctx, survey, site) and (not findings.failed or force)

    changed = bool(src.units) and not plan.in_sync
    errors: List[str] = []
    if changed:
        errors += apply_plan(ctx, plan)
    if not units.in_sync:
        userunits.take_out(ctx, units.remove)
        errors += userunits.apply(ctx, units)
    if changed or not units.in_sync:
        if errors:
            out.status("FAIL", "could not write everything", errors)
        else:
            written = len(units.add) + len(units.change) + (len(plan.add) + len(plan.change)
                                                            if changed else 0)
            removed = len(units.remove) + (len(plan.remove) + len(plan.legacy) if changed else 0)
            out.status("ok", ", ".join([f"{plural(written, 'file')} written"]
                                       + ([f"{removed} removed"] if removed else [])))
        if not reload(ctx):
            return False
    elif site.in_sync:
        out.status("ok", "already up to date")

    services = ctx.installed(app).services() if src.units else []
    expected = services + sorted(units.wanted)
    states = host.show(expected) if expected else {}
    missing = [n for n in expected if states[n].get("LoadState") != "loaded"]
    if missing and not (changed or not units.in_sync):
        # Up to date on disk, but systemd never picked it up.
        if not reload(ctx):
            return False
        states = host.show(expected)
        missing = [n for n in expected if states[n].get("LoadState") != "loaded"]
    if missing:
        out.status("FAIL", f"not loaded after daemon-reload: {join_names(missing)}",
                   [f"redh check {app}"])
        return False
    if expected:
        out.status("ok", f"systemd knows all {len(expected)} units" if len(expected) > 1
                   else "systemd knows the unit")
    enabled_ok = True
    for level, text in userunits.enable(ctx, units):
        out.status(level, text)
        enabled_ok = enabled_ok and level != "FAIL"
    caddy_ok = sites.apply(ctx, survey, site)

    if restart:
        out.write()
        return lifecycle.restart_app(ctx, app) and caddy_ok and enabled_ok and not errors
    names, everything = plan.touched()
    active = {n for n in expected if states[n].get("ActiveState") in ("active", "activating", "reloading")}
    stale = [u.startable for u in ctx.installed(app).units
             if u.startable in active and (everything or u.filename in names)] if changed else []
    starting = services + units.starts
    if stale:
        out.status("info", f"running on the old definition: {join_names(stale, 4)} → redh restart {app}")
    elif starting and not active & set(starting):
        out.status("info", f"not running → redh start {app}")
    return caddy_ok and enabled_ok and not errors and (not findings.failed or force)


def install(ctx: Context, apps: List[str], dry_run: bool = False,
            force: bool = False, restart: bool = False) -> int:
    survey = Survey(ctx)
    ok = True
    for index, app in enumerate(apps):
        if index:
            ctx.out.write()
        ok = install_app(ctx, app, survey, dry_run, force, restart) and ok
    return 0 if ok else 1


def remove_installed(ctx: Context, app: str) -> List[str]:
    """Delete the app's install directory, stray copies and plain units (disabled
    first); returns what went wrong."""
    inst = ctx.installed(app)
    legacy, _ = copies_elsewhere(ctx, app, ctx.sources(app).files or inst.files)
    units = userunits.removal(ctx, app)
    if units.remove:
        ctx.host.systemctl("disable", "--", *units.remove, timeout=60)
    errors: List[str] = userunits.apply(ctx, units)
    if inst.exists:
        try:
            parent = confine.open_dir(ctx.settings, os.path.dirname(inst.dir))
            try:
                confine.remove(parent, os.path.basename(inst.dir))
            finally:
                os.close(parent)
        except OSError as exc:
            errors.append(f"{inst.dir}: {exc.strerror or exc}")
    for path, _, _ in legacy:
        try:
            remove_file(ctx, path)
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror or exc}")
    return errors


def uninstall_app(ctx: Context, app: str, dry_run: bool, force: bool,
                  survey: Optional[Survey] = None) -> bool:
    out = ctx.out
    inst = ctx.installed(app)
    legacy, _ = copies_elsewhere(ctx, app, ctx.sources(app).files or inst.files)
    site = sites.removal(ctx, app)
    units = userunits.removal(ctx, app)
    out.heading(f"uninstall {app}")
    if not inst.exists and not legacy and site.in_sync and units.in_sync:
        out.status("skip", "not installed")
        return True
    if inst.exists:
        out.write(f"  - {inst.dir}/  ({plural(len(inst.files), 'file')})")
    for path, _, _ in legacy:
        out.write(f"  x {path}  (old copy)")
    for line in units.lines(ctx.settings) + site.lines(ctx.settings):
        out.write(f"  {line}")
    if dry_run:
        out.status("skip", "dry run: nothing stopped or removed")
        return True

    services = inst.services() + units.remove
    stopped = lifecycle.stop_app(ctx, app, heading=False)
    if not stopped and not force:
        out.status("FAIL", "units still running; nothing removed (--force removes them anyway)")
        return False
    errors = remove_installed(ctx, app)
    removed = inst.exists or legacy or units.remove
    if errors:
        out.status("FAIL", "could not remove everything", errors)
    elif removed:
        out.status("ok", "removed")
    if removed and not reload(ctx):
        return False
    if services:
        ctx.host.systemctl("reset-failed", "--", *services, timeout=60)
    caddy_ok = sites.apply(ctx, survey or Survey(ctx), site)
    return not errors and caddy_ok


def uninstall(ctx: Context, apps: List[str], dry_run: bool = False, force: bool = False) -> int:
    survey = Survey(ctx)
    ok = True
    for index, app in enumerate(apps):
        if index:
            ctx.out.write()
        ok = uninstall_app(ctx, app, dry_run, force, survey) and ok
    return 0 if ok else 1
