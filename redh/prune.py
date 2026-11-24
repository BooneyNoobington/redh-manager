"""redh prune: remove an app completely — units, podman objects, data.

In order: stop the units, remove the installed Quadlet files, reload, remove
what the units left in podman (containers and pods normally go with --rm;
networks and named volumes stay behind), then delete /opt/podman/<app>.
Nothing is deleted while a unit is still running, and the whole thing has to
be confirmed by typing the app's name (or --yes).
"""

from __future__ import annotations

import os
import sys
from typing import List, Tuple

from . import lifecycle, sites, userunits
from .checks import Survey
from .app import Context
from .common import die, join_names, plural, rmtree
from .install import reload, remove_installed
from .plan import copies_elsewhere

# kind -> (podman subcommand to remove it, the unit extension that defines it)
OBJECTS = (
    ("container", ["rm", "--force"], ".container"),
    ("pod", ["pod", "rm", "--force"], ".pod"),
    ("volume", ["volume", "rm"], ".volume"),
    ("network", ["network", "rm"], ".network"),
)


def podman_objects(ctx: Context, app: str, keep_volumes: bool) -> List[Tuple[str, str]]:
    """(kind, name) of every podman object the app's units create."""
    units = {u.filename: u for u in ctx.installed(app).units}
    for u in ctx.sources(app).units:
        units.setdefault(u.filename, u)
    found = []
    for kind, _, ext in OBJECTS:
        if kind == "volume" and keep_volumes:
            continue
        for u in sorted(units.values(), key=lambda unit: unit.filename):
            if u.ext == ext and u.resource_name:
                found.append((kind, u.resource_name))
    return found


def check_target(ctx: Context, app: str) -> None:
    """Refuse anything but a plain directory directly below the apps root."""
    s = ctx.settings
    path = s.app_dir(app)
    if not os.path.lexists(path):
        return
    if os.path.islink(path):
        die(f"{path} is a symlink; remove it by hand")
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(s.root):
        die(f"{path} resolves to {real}, outside {s.root}")
    for protected in (s.home, s.quadlet_dir):
        guarded = os.path.realpath(protected)
        if guarded == real or guarded.startswith(real + os.sep):
            die(f"refusing to delete {path}: it contains {protected}")


def delete_app_dir(ctx: Context, path: str) -> List[str]:
    """rm -rf as root; as the podman user via 'podman unshare', which can
    delete files owned by the container's subordinate UIDs."""
    if ctx.host.is_root:
        if os.path.ismount(path):
            errors = []
            for name in os.listdir(path):
                child = os.path.join(path, name)
                if os.path.isdir(child) and not os.path.islink(child):
                    errors += rmtree(child)
                else:
                    try:
                        os.unlink(child)
                    except OSError as exc:
                        errors.append(f"{child}: {exc.strerror}")
            return errors + [f"{path} is a mount point: emptied it, the directory stays"]
        return rmtree(path)
    result = ctx.host.podman("unshare", "rm", "-rf", "--one-file-system", "--", path, timeout=None)
    return [] if result.ok else [result.message()]


def prune(ctx: Context, app: str, dry_run: bool = False, yes: bool = False,
          keep_volumes: bool = False, force: bool = False) -> int:
    s, host, out = ctx.settings, ctx.host, ctx.out
    src, inst = ctx.sources(app), ctx.installed(app)
    legacy, _ = copies_elsewhere(ctx, app, src.files or inst.files)
    if (not src.exists and not inst.exists and not legacy and sites.removal(ctx, app).in_sync
            and userunits.removal(ctx, app).in_sync):
        die(f"no app {app}: neither {src.dir} nor {inst.dir}/ exists")
    check_target(ctx, app)

    units = userunits.removal(ctx, app)
    services = inst.services() + units.remove
    objects = podman_objects(ctx, app, keep_volumes)
    deps = lifecycle.dependents(ctx, app)

    out.heading(f"prune {app}")
    if services:
        out.write(f"  stop      {plural(len(services), 'unit')}: {join_names(services, 4)}")
    if inst.exists:
        out.write(f"  remove    {inst.dir}/  ({plural(len(inst.files), 'file')})")
    for path, _, _ in legacy:
        out.write(f"  remove    {path}  (old copy)")
    for name in units.remove:
        out.write(f"  remove    {s.user_unit_dir}/{name}  (plain unit)")
    site = sites.removal(ctx, app)
    for name in site.remove:
        out.write(f"  remove    {s.caddy_dir}/{name}  (Caddy snippet)")
    for kind in ("container", "pod", "volume", "network"):
        names = [name for k, name in objects if k == kind]
        if names:
            out.write(f"  podman    {kind}s: {join_names(names, 4)}")
    if keep_volumes:
        kept = [u.resource_name for u in ctx.units(app) if u.ext == ".volume" and u.resource_name]
        if kept:
            out.write(f"  keep      volumes: {join_names(kept, 4)}")
    if src.exists:
        out.write(f"  delete    {src.dir}/")

    if deps:
        users = sorted({f"{service} ({other})" for other, service, _ in deps})
        level = "warn" if force else "FAIL"
        out.status(level, f"other apps depend on {app}: {join_names(users, 4)}",
                   [] if force else ["remove or change those first, or pass --force"])
        if not force:
            return 1
    if dry_run:
        out.status("skip", "dry run: nothing changed")
        return 0

    if not yes:
        if not sys.stdin.isatty():
            die("prune needs confirmation: run it in a terminal, or pass --yes")
        if src.exists:
            out.write(f"\nThis deletes {src.dir} for good. Make sure your last borg archive has it.")
        try:
            answer = input(f"Type '{app}' to go ahead: ")
        except EOFError:
            answer = ""
        if answer.strip() != app:
            out.write("aborted, nothing changed.")
            return 1
    out.write()

    # Also for an app redh never installed: nothing is deleted under a running container.
    if not lifecycle.stop_app(ctx, app, heading=False):
        if not force:
            out.status("FAIL", "units still running; stopped here, nothing deleted "
                               "(--force carries on anyway)")
            return 1
        out.status("warn", "units still running; carrying on because of --force")

    ok = True
    errors = remove_installed(ctx, app)
    if errors:
        ok = False
        out.status("FAIL", "could not remove all Quadlet files", errors)
    elif inst.exists or legacy or units.remove:
        out.status("ok", "Quadlet files and plain units removed" if units.remove
                   else "Quadlet files removed")
    if inst.exists or legacy or units.remove:
        ok = reload(ctx) and ok
        if services:
            host.systemctl("reset-failed", "--", *services, timeout=60)
    ok = sites.apply(ctx, Survey(ctx), site) and ok

    for kind, name in objects:
        command = next(cmd for k, cmd, _ in OBJECTS if k == kind)
        if host.podman(kind, "exists", name, timeout=30).rc == 1:
            if out.verbose:
                out.status("ok", f"{kind} {name} is gone already")
            continue
        result = host.podman(*command, name, timeout=300)
        if result.ok:
            out.status("ok", f"removed {kind} {name}")
        else:
            ok = False
            out.status("FAIL", f"could not remove {kind} {name}: {result.message()}")

    if src.exists:
        errors = delete_app_dir(ctx, src.dir)
        if errors:
            ok = False
            out.status("FAIL", f"could not delete all of {src.dir}", errors[:10]
                       + ([f"... and {len(errors) - 10} more"] if len(errors) > 10 else []))
        else:
            out.status("ok", f"deleted {src.dir}")

    out.status("info", f"images stay; 'podman image prune' as {s.user} removes unused ones")
    if ok:
        out.status("ok", f"{app} is gone")
    return 0 if ok else 1
