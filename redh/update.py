"""redh update: by hand, for one app, what podman auto-update does on its timer.

1. run the app's .build units again and pull its images (.image units too);
2. restart exactly the units whose running container is now behind the image
   its name points at;
3. report on those the way 'start' does, and if one fails, say how to go back
   to the image it ran before.

Unlike podman auto-update this also covers containers without AutoUpdate=
and images built locally. 'update --dry-run' goes the other way and asks
podman auto-update itself what it would do — which is what the timer sees.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import lifecycle
from .app import Context
from .common import HostError, plural
from .running import running, stop_hint
from .units import QuadletUnit

AUTO_UPDATE = "podman-auto-update.service"
DRY_RUN_FORMAT = "{{.Unit}}\t{{.Image}}\t{{.Policy}}\t{{.Updated}}"

# unit -> [(image, policy, updated)], and what podman auto-update complained about
Report = Tuple[Dict[str, List[Tuple[str, str, str]]], List[str]]


def short(image_id: Optional[str]) -> str:
    return (image_id or "").split(":")[-1][:12] or "none"


def image_id(ctx: Context, ref: str) -> Optional[str]:
    result = ctx.host.podman("image", "inspect", "--format", "{{.Id}}", ref, timeout=60)
    value = result.out.strip()
    return value if result.ok and value else None


def change(before: Optional[str], after: Optional[str]) -> str:
    if before is None:
        return f"pulled {short(after)}"
    if before == after:
        return f"up to date ({short(after)})"
    return f"new image {short(before)} → {short(after)}"


def what_to_update(ctx: Context, app: str) -> Tuple[List[str], List[QuadletUnit], List[str]]:
    """(images to pull, build units to run again, what is left out and why)."""
    units = ctx.installed(app).units
    builds = [u for u in units if u.ext == ".build" and u.startable]
    refs: List[str] = []
    skipped: List[str] = []
    for u in units:
        if u.ext == ".kube":
            skipped.append(f"{u.filename}: the images in its YAML are left to podman auto-update")
            continue
        if u.ext not in (".container", ".image"):
            continue
        image = u.get("Image") or ""
        # .image: pulled through that unit (here, or in the app that has it); .build: rebuilt.
        if not image or image.endswith((".image", ".build")):
            continue
        if image.startswith("localhost/"):
            skipped.append(f"{u.filename}: {image} only exists here, there is nothing to pull")
        elif image not in refs:
            refs.append(image)
    return refs, builds, skipped


def last_automatic_run(ctx: Context) -> Tuple[str, str, List[str]]:
    """(level, text, journal lines) about the last run of podman-auto-update.service."""
    state = ctx.host.show([AUTO_UPDATE], ["ActiveState", "Result", "ExecMainStatus",
                                          "ExecMainStartTimestamp"])[AUTO_UPDATE]
    stamp = (state.get("ExecMainStartTimestamp") or "").split()
    when = f"{stamp[1]} {stamp[2][:5]}" if len(stamp) >= 3 else ""
    if state.get("ActiveState") == "activating":
        return "info", "podman auto-update is running right now", []
    if not when:
        return "info", "podman auto-update has not run since the user manager started", []
    if state.get("Result") == "success":
        return "ok", f"last automatic update {when}: success", []
    return ("FAIL", f"last automatic update {when} failed ({state.get('Result')}, "
                    f"exit status {state.get('ExecMainStatus')})",
            ctx.host.journal(AUTO_UPDATE, since=0, lines=10))


def auto_update_report(ctx: Context) -> Report:
    """What podman auto-update would do right now, without doing it."""
    result = ctx.host.podman("auto-update", "--dry-run", f"--format={DRY_RUN_FORMAT}", timeout=900)
    entries: Dict[str, List[Tuple[str, str, str]]] = {}
    for line in result.out.splitlines():
        parts = line.split("\t")
        if len(parts) == 4:
            entries.setdefault(parts[0], []).append((parts[1], parts[2], parts[3]))
    problems = [line.strip() for line in result.err.splitlines() if line.strip()]
    if not result.ok and not entries and not problems:
        problems = [result.message()]
    return entries, problems


def preview(ctx: Context, app: str, refs: Sequence[str], builds: Sequence[QuadletUnit],
            report: Report) -> bool:
    out = ctx.out
    entries, problems = report
    names = [u.startable for u in ctx.installed(app).units
             if u.ext in (".container", ".kube") and u.startable]
    width = max((len(n) for n in names), default=0)
    ok = True
    for b in builds:
        out.status("info", f"{b.service} would build {b.get('ImageTag') or 'its image'} again")
    for name in names:
        rows = entries.get(name)
        if not rows:
            out.status("skip", f"{name:<{width}}  podman auto-update leaves it alone "
                               f"(no AutoUpdate=, or not running)")
        for image, policy, updated in rows or ():
            if updated == "pending":
                out.status("info", f"{name:<{width}}  update available: {image} ({policy})")
            elif updated == "false":
                out.status("ok", f"{name:<{width}}  up to date: {image} ({policy})")
            else:
                ok = False
                out.status("warn", f"{name:<{width}}  {updated}: {image} ({policy})")
    images = list(refs) + [image for rows in entries.values() for image, _, _ in rows]
    for problem in problems:
        if any(name in problem for name in list(names) + images):
            ok = False
            out.status("FAIL", problem)
    out.status("skip", "dry run: nothing pulled, built or restarted")
    return ok


def update_app(ctx: Context, app: str, dry_run: bool = False,
               report: Optional[Report] = None) -> bool:
    host, out = ctx.host, ctx.out
    out.heading(f"update {app}")
    if lifecycle.not_installed(ctx, app):
        return False
    refs, builds, skipped = what_to_update(ctx, app)
    for line in skipped:
        out.status("skip", line)
    if dry_run:
        return preview(ctx, app, refs, builds, report or auto_update_report(ctx))

    ok = True
    width = max([len(r) for r in refs] + [len(b.service) for b in builds], default=0)
    previous: Dict[str, str] = {}          # image -> the ID it had, to go back to
    for b in builds:
        tag = b.get("ImageTag") or ""
        before = image_id(ctx, tag) if tag else None
        out.write(out.dim(f"  building {tag or b.filename} …"))
        began = time.time()
        result = host.systemctl("restart", "--", b.service)
        if result.ok and host.show([b.service])[b.service].get("ActiveState") != "failed":
            after = image_id(ctx, tag) if tag else None
            out.status("ok", f"{b.service:<{width}}  built: {change(before, after)}")
            if before and after and before != after:
                previous[tag] = before
        else:
            ok = False
            out.status("FAIL", f"{b.service:<{width}}  the build failed",
                       host.journal(b.service, began - 1))

    if refs:
        out.write(out.dim(f"  pulling {plural(len(refs), 'image')} …"))
    for ref in refs:
        before = image_id(ctx, ref)
        result = host.podman("pull", "--quiet", ref, timeout=None)
        if not result.ok:
            ok = False
            out.status("FAIL", f"{ref:<{width}}  pull failed: {result.message()}")
            continue
        after = image_id(ctx, ref) or (result.out.split() or [""])[-1]
        out.status("ok", f"{ref:<{width}}  {change(before, after)}")
        if before and after and before != after:
            previous[ref] = before

    # A container is behind when the name it was started from now means another image.
    try:
        containers = [c for c in running(ctx) if c.app == app]
    except HostError as exc:
        out.status("FAIL", f"cannot tell what runs: {exc}")
        return False
    services = set(ctx.installed(app).services())
    behind = []
    for c in containers:
        current = image_id(ctx, c.image) if c.image else None
        if not current or not c.image_id or current == c.image_id:
            continue
        if c.unit in services:
            behind.append(c)
        else:
            out.status("warn", f"{c.name} runs an older image, but redh did not start it",
                       [stop_hint(ctx, c) + ", then start it again"])
    if not behind:
        out.status("ok", "every container already runs its newest image" if containers
                   else "nothing runs; the next start uses the new images")
        return ok

    started = lifecycle.start_app(ctx, app, restart=sorted({c.unit for c in behind}),
                                  heading=False)
    if not started and previous:
        user = ctx.settings.user
        out.status("info", "to go back to the images it ran before:",
                   [f"runuser -u {user} -- podman tag {short(old)} {ref}"
                    for ref, old in previous.items()] + [f"redh restart {app}"])
    return ok and started


def update(ctx: Context, apps: Sequence[str], dry_run: bool = False) -> int:
    out = ctx.out
    level, text, lines = last_automatic_run(ctx)
    out.status(level, text, lines, indent=0)
    report = auto_update_report(ctx) if dry_run else None
    ok = True
    for app in apps:
        out.write()
        ok = update_app(ctx, app, dry_run, report) and ok
    return 0 if ok else 1
