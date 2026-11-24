"""What is actually running: the podman user's containers, and their apps.

systemd only knows the units redh installed; podman knows every container,
however it was started — by a unit redh installed, by a Quadlet file someone
copied by hand, or by a plain 'podman run'. Before a backup that is the
question that matters, so this asks podman and works out each container's
app from, in order:

    unit    its systemd unit is generated from one of the app's Quadlet files
    file    the Quadlet file behind its unit lives in the app's directory, or
            has the name of one of the app's files
    mount   it bind-mounts the app's directory under /opt/podman
    name    its name is the app's name (systemd-<app>, <app>-db, <app>_web)
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import confine
from .common import HostError
from .units import QuadletUnit, quadlet_ext


@dataclass
class Container:
    id: str
    name: str
    image: str
    image_id: str = ""                # the image it actually runs
    unit: str = ""                    # the systemd unit running it, if any
    sources: List[str] = field(default_factory=list)   # bind-mounted host paths
    source_path: str = ""             # the Quadlet file behind its unit
    app: Optional[str] = None
    why: str = ""                     # unit, file, mount or name


def running(ctx) -> List[Container]:
    """Every running container of the podman user, each with its app if it has one.

    Raises HostError when podman cannot be asked.
    """
    host = ctx.host
    listing = host.podman("ps", "--quiet", "--no-trunc", timeout=60)
    if not listing.ok:
        raise HostError(f"podman ps failed: {listing.message()}")
    ids = listing.out.split()
    if not ids:
        return []
    # A container can stop between the two calls; inspect then reports the
    # others and fails, so the output counts, not the exit status.
    inspected = host.podman("container", "inspect", *ids, timeout=120)
    try:
        records = json.loads(inspected.out or "[]")
    except ValueError:
        raise HostError(f"podman inspect: {inspected.message()}") from None
    containers = []
    for record in records:
        state = record.get("State") or {}
        if record.get("IsInfra") or state.get("Status") != "running":
            continue
        config = record.get("Config") or {}
        labels = config.get("Labels") or {}
        containers.append(Container(
            id=(record.get("Id") or "")[:12],
            name=record.get("Name") or "",
            image=record.get("ImageName") or config.get("Image") or "",
            image_id=record.get("Image") or "",
            unit=labels.get("PODMAN_SYSTEMD_UNIT", ""),
            sources=[m.get("Source", "") for m in record.get("Mounts") or []
                     if m.get("Type") == "bind" and m.get("Source")]))
    assign(ctx, containers)
    return sorted(containers, key=lambda c: (c.app or "~", c.name))


def app_of_path(ctx, apps: Set[str], path: str) -> Optional[str]:
    """The app whose directory (or install directory) holds path."""
    s = ctx.settings
    for base in (s.quadlet_dir, s.root):
        prefix = base.rstrip("/") + "/"
        if path.startswith(prefix):
            first = path[len(prefix):].split("/", 1)[0]
            if first in apps:
                return first
    return None


def assign(ctx, containers: List[Container]) -> None:
    apps = set(ctx.known_apps())
    by_service: Dict[str, str] = {}
    by_file: Dict[str, str] = {}
    for app in sorted(apps):
        for unit in ctx.installed(app).units + ctx.sources(app).units:
            for name in (unit.service, unit.startable):
                if name:
                    by_service.setdefault(name, app)
            by_file.setdefault(unit.filename, app)

    units = sorted({c.unit for c in containers if c.unit})
    try:
        paths = ctx.host.show(units, ["SourcePath"]) if units else {}
    except HostError:
        paths = {}

    for c in containers:
        c.source_path = paths.get(c.unit, {}).get("SourcePath", "") if c.unit else ""
        app, why = by_service.get(c.unit), "unit"
        if not app and c.source_path:
            app = (app_of_path(ctx, apps, os.path.realpath(c.source_path))
                   or app_of_path(ctx, apps, c.source_path)
                   or by_file.get(os.path.basename(c.source_path)))
            why = "file"
        if not app:
            hits = Counter(a for a in (app_of_path(ctx, apps, s) for s in c.sources) if a)
            if hits:
                app, why = hits.most_common(1)[0][0], "mount"
        if not app:
            bare = c.name[len("systemd-"):] if c.name.startswith("systemd-") else c.name
            for candidate in sorted(apps, key=len, reverse=True):
                if bare == candidate or bare.startswith((candidate + "-", candidate + "_")):
                    app, why = candidate, "name"
                    break
        c.app, c.why = app, why if app else ""


def mount_sources(ctx) -> Set[str]:
    """Every host path a container mounts, or will: from every app's units, from the
    loose unit files in the Quadlet directory, and from what actually runs."""
    units = []
    for app in ctx.known_apps():
        units += ctx.sources(app).units + ctx.installed(app).units
    for rel, path in ctx.tree().items():
        if os.sep not in rel and quadlet_ext(rel):
            try:
                units.append(QuadletUnit.load(path, read=confine.reader(ctx.settings)))
            except OSError:
                pass
    paths = {os.path.realpath(source) for u in units for _, source in u.bind_sources()
             if source.startswith("/") and "%" not in source}
    try:
        paths |= {os.path.realpath(source) for c in running(ctx) for source in c.sources}
    except HostError:
        pass
    return paths


def stop_hint(ctx, container: Container) -> str:
    """The command that stops this one container, as root."""
    s = ctx.settings
    if container.unit:
        return f"{ctx.host.manager_hint} stop {container.unit}"
    return f"runuser -u {s.user} -- podman stop {container.name}"


def describe(container: Container) -> str:
    """'vaultwarden (docker.io/vaultwarden/server:1.32), unit vaultwarden.service'."""
    text = f"{container.name} ({container.image})"
    if container.unit:
        text += f", unit {container.unit}"
    else:
        text += ", no systemd unit"
    return text
