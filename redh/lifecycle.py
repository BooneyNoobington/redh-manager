"""Starting, stopping and looking at an app's units.

An app's units are the services its *installed* Quadlet files generate —
that is what systemd knows about — and its installed plain units (see
userunits.py): 'start' starts those with an [Install] section (the timer, not
the service it triggers), 'stop' stops them all. One systemctl call starts or stops them
all, so systemd orders the jobs by their dependencies; afterwards every unit
is looked at on its own, and for each one that did not end up where it should
the journal says why.

systemd is not the last word on what runs, though: after stopping, podman is
asked whether any container of the app is still up (see running.py), however
it was started.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

from .app import Context
from .common import HostError, join_names, plural
from .host import finished, is_task
from . import sites, userunits
from .plan import make_plan
from .running import Container, running, stop_hint
from .running import describe as running_describe
from .units import QuadletUnit

HEALTH_FORMAT = "{{.State.Health.Status}}"


class Tally:
    def __init__(self) -> None:
        self.counts = {"ok": 0, "info": 0, "warn": 0, "FAIL": 0, "skip": 0}

    def add(self, level: str) -> None:
        self.counts[level] = self.counts.get(level, 0) + 1

    @property
    def failed(self) -> int:
        return self.counts["FAIL"]

    def summary(self) -> str:
        parts = [f"{self.counts['ok']} ok"]
        if self.counts["warn"]:
            parts.append(plural(self.counts["warn"], "warning"))
        parts.append(f"{self.counts['FAIL']} failed")
        return ", ".join(parts)


def services_of(ctx: Context, app: str) -> List[Tuple[str, QuadletUnit]]:
    """(service, unit) for every unit of the installed copy that can be started."""
    return [(u.startable, u) for u in ctx.installed(app).units if u.startable]


def plain_of(ctx: Context, app: str, every: bool = False) -> List[Tuple[str, None]]:
    """(name, None) for the app's installed plain units: those that start with the app
    (an [Install] section: a timer), or every one (its service too, to stop it)."""
    return [(u.name, None) for u in ctx.user_units(app) if every or u.starts]


def shown(ctx: Context, app: str) -> List[str]:
    """The units whose state 'list' counts: the services, and the plain units that start."""
    return ctx.installed(app).services() + [name for name, _ in plain_of(ctx, app)]


def dependents(ctx: Context, app: str) -> List[Tuple[str, str, str]]:
    """Units of other installed apps that systemd stops along with this app.

    Quadlet turns Network=x.network and friends into Requires=, and a unit
    whose requirement is stopped is stopped too. Returns (other app, its
    service, the service of this app it requires).
    """
    mine = {u.filename: u for u in ctx.installed(app).units}
    my_services = {u.service for u in mine.values()} | {u.startable for u in mine.values()}
    found = []
    for other in ctx.installed_apps():
        if other == app:
            continue
        for u in ctx.installed(other).units:
            if not u.startable:
                continue
            needs = [mine[target].service for _, target in u.references() if target in mine]
            needs += [name for name in u.requires() if name in my_services]
            for need in dict.fromkeys(needs):
                found.append((other, u.startable, need))
    return found


def describe(state: Dict[str, str]) -> str:
    """'active (running)', 'failed (exit-code), exit status 125', 'finished', ..."""
    if finished(state):
        return "finished"
    active = state.get("ActiveState") or state.get("LoadState") or "?"
    sub = state.get("SubState", "")
    text = f"{active} ({sub})" if sub and sub != active else active
    result = state.get("Result", "")
    if result and result != "success":
        text += f", result {result}"
    status = state.get("ExecMainStatus", "")
    if status not in ("", "0") and active != "active":
        text += f", exit status {status}"
    return text


def since(state: Dict[str, str]) -> str:
    key = "ActiveEnterTimestamp" if state.get("ActiveState") == "active" else "InactiveEnterTimestamp"
    parts = (state.get(key) or "").split()
    return f"since {parts[1]} {parts[2][:5]}" if len(parts) >= 3 else ""


def health(ctx: Context, pairs: List[Tuple[str, QuadletUnit]],
           states: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """healthy/unhealthy/starting for active containers that define a HealthCmd=."""
    found = {}
    for name, unit in pairs:
        if (unit is None or unit.ext != ".container" or not unit.get("HealthCmd")
                or states.get(name, {}).get("ActiveState") != "active" or not unit.resource_name):
            continue
        result = ctx.host.podman("inspect", "--format", HEALTH_FORMAT, unit.resource_name, timeout=30)
        if result.ok and result.out.strip():
            found[name] = result.out.strip()
    return found


def _restarts(after: Dict[str, str], before: Dict[str, str]) -> int:
    try:
        return int(after.get("NRestarts") or 0) - int(before.get("NRestarts") or 0)
    except ValueError:
        return 0


def not_installed(ctx: Context, app: str) -> bool:
    if ctx.installed(app).exists or ctx.user_units(app):
        return False
    hint = f" — redh install {app}" if ctx.sources(app).exists else ""
    ctx.out.status("FAIL", f"not installed{hint}")
    return True


def counted(states: Dict[str, Dict[str, str]], names: Sequence[str]) -> List[str]:
    """The units whose being active matters: tasks only count when they failed."""
    return [n for n in names
            if not (is_task(states.get(n, {})) and states[n].get("ActiveState") != "failed")]


def start_app(ctx: Context, app: str, restart: Sequence[str] = (), heading: bool = True) -> bool:
    """Start the app's units (and restart the ones named), then report on each."""
    host, out, settle = ctx.host, ctx.out, ctx.settings.settle
    if heading:
        out.heading(f"start {app}")
    if not_installed(ctx, app):
        return False
    pairs: List[Tuple[str, Optional[QuadletUnit]]] = [*services_of(ctx, app), *plain_of(ctx, app)]
    if not pairs:
        out.status("warn", "no unit to start (only templates without DefaultInstance=?)")
        return True
    names = [name for name, _ in pairs]
    before = host.show(names)
    missing = [n for n in names if before[n].get("LoadState") != "loaded"]
    todo = [n for n in names if n not in missing]
    # A finished task (a build, say) is not 'stopped': it only runs again when
    # something that needs it starts, or when it failed last time.
    fresh = [n for n in todo if before[n].get("ActiveState") != "active"
             and (not is_task(before[n]) or before[n].get("ActiveState") == "failed")]
    restart = [n for n in restart if n in todo]

    started_at = time.time()
    result = None
    failed = [n for n in todo if before[n].get("ActiveState") == "failed"]
    if failed and (fresh or restart):
        host.systemctl("reset-failed", "--", *failed, timeout=60)
    if restart:
        out.write(out.dim(f"  restarting {plural(len(restart), 'unit')} …"))
        result = host.systemctl("restart", "--", *restart)
    if fresh:
        out.write(out.dim(f"  starting {plural(len(fresh), 'unit')} …"))
        started = host.systemctl("start", "--", *todo)
        result = started if result is None or result.ok else result
    if (fresh or restart) and settle:
        time.sleep(settle)
    after = host.show(names)
    healthy = health(ctx, pairs, after)

    tally = Tally()
    width = max(len(n) for n in names)
    for name, unit in pairs:
        if name in missing:
            tally.add("FAIL")
            why = "the generator did not produce it" if unit is not None else "its file is gone"
            out.status("FAIL", f"{name:<{width}}  not loaded ({before[name].get('LoadState')}): "
                               f"{why}", [f"redh check {app}"])
            continue
        state, was = after[name], before[name]
        active = state.get("ActiveState")
        notes = []
        if name in restart:
            notes.append("restarted")
        elif was.get("ActiveState") == "active":
            notes.append("already running")
        level = "ok"
        if active == "active":
            restarts = _restarts(state, was)
            if restarts > 0:
                level = "warn"
                notes.append(f"restarted {plural(restarts, 'time')} on the way up")
            status = healthy.get(name)
            if status == "unhealthy":
                level = "warn"
            if status:
                notes.append(status)
        elif is_task(state) and active == "inactive" and state.get("Result") in ("success", None):
            level = "ok"                      # ran and finished, or had no reason to run
        elif active == "activating" and state.get("SubState") == "auto-restart":
            level = "FAIL"
            notes.append("crash loop: systemd keeps restarting it")
        elif active == "activating":
            level = "warn"
            notes.append(f"still starting after {settle:g}s")
        else:
            level = "FAIL"
        tally.add(level)
        text = f"{name:<{width}}  {describe(state)}" + (f" — {', '.join(notes)}" if notes else "")
        out.status(level, text, host.journal(name, started_at - 1) if level != "ok" else ())

    if result is not None and not result.ok and not tally.failed:
        out.status("warn", f"systemctl: {result.message()}")
    out.write(f"  → {tally.summary()}")
    return tally.failed == 0


def stop_app(ctx: Context, app: str, heading: bool = True) -> bool:
    """Stop the app's units, then make sure podman runs nothing of the app any more."""
    host, out = ctx.host, ctx.out
    if heading:
        out.heading(f"stop {app}")
    tally = Tally()
    pairs: List[Tuple[str, Optional[QuadletUnit]]] = [
        *(services_of(ctx, app) if ctx.installed(app).exists else []), *plain_of(ctx, app, every=True)]
    names = [name for name, _ in pairs]
    width = max((len(n) for n in names), default=0)
    if not pairs:
        out.status("skip", "no installed units to stop")
    else:
        width = _stop_units(ctx, app, names, tally)
    _leftovers(ctx, app, names, width, tally)
    out.write(f"  → {tally.summary()}")
    return tally.failed == 0


def _stop_units(ctx: Context, app: str, names: List[str], tally: Tally) -> int:
    host, out = ctx.host, ctx.out
    before = host.show(names)
    # A unit whose file is gone still shows as not-found while it runs; a
    # finished task has nothing to stop.
    todo = [n for n in names if before[n].get("ActiveState") not in ("inactive", None)
            or (before[n].get("LoadState") == "loaded" and not is_task(before[n]))]

    deps = dependents(ctx, app)
    dep_names = list(dict.fromkeys(service for _, service, _ in deps))
    dep_before = host.show(dep_names) if dep_names else {}
    running_deps = [d for d in deps if dep_before[d[1]].get("ActiveState") == "active"]
    if running_deps:
        users = sorted({d[1] for d in running_deps})
        needs = sorted({d[2] for d in running_deps})
        out.status("info", f"systemd stops {join_names(users)} as well: "
                           f"{'it requires' if len(users) == 1 else 'they require'} {join_names(needs)}")

    stopped_at = time.time()
    result = None
    if todo:
        out.write(out.dim(f"  stopping {plural(len(todo), 'unit')} …"))
        result = host.systemctl("stop", "--", *todo)
    after = host.show(names + dep_names)

    width = max(len(n) for n in names + dep_names)
    for name in names:
        state, was = after[name], before[name]
        active = state.get("ActiveState")
        notes = ["was not running"] if (was.get("ActiveState") in ("inactive", None)
                                        and not is_task(was)) else []
        if active == "inactive":
            level = "ok"
        elif active == "failed" and was.get("ActiveState") == "failed":
            level = "ok"
            notes.append("had already failed before")
        elif active == "failed":
            level = "warn"
            notes.append("stopped, but did not exit cleanly")
        else:
            level = "FAIL"
            notes.append("still running")
        tally.add(level)
        text = f"{name:<{width}}  {describe(state)}" + (f" — {', '.join(notes)}" if notes else "")
        out.status(level, text, host.journal(name, stopped_at - 1) if level != "ok" else ())

    for other in sorted({d[0] for d in running_deps}):
        for name in sorted({d[1] for d in running_deps if d[0] == other}):
            state = after[name]
            if state.get("ActiveState") in ("inactive", "failed"):
                out.status("info", f"{name:<{width}}  {describe(state)} — part of {other}; "
                                   f"bring it back with: redh start {other}")

    if result is not None and not result.ok and not tally.failed:
        out.status("warn", f"systemctl: {result.message()}")
    return width


def _leftovers(ctx: Context, app: str, names: List[str], width: int, tally: Tally) -> None:
    """Containers of the app that podman still runs — started some other way, or stuck."""
    out = ctx.out
    try:
        leftovers = [c for c in running(ctx) if c.app == app]
    except HostError as exc:
        tally.add("warn")
        out.status("warn", f"could not ask podman what still runs: {exc}")
        return
    for c in leftovers:
        if c.unit in names:
            why = "its unit is stopped, but the container is not"
        elif c.unit:
            why = f"started by {c.unit}, which redh did not install"
        else:
            why = "started without a systemd unit"
        tally.add("FAIL")
        out.status("FAIL", f"{c.name:<{width}}  still running — {why}",
                   ([f"from {c.source_path}"] if c.source_path else []) + [stop_hint(ctx, c)])


def nothing_running(ctx: Context) -> bool:
    """After 'stop --all': is any container of the podman user still up?"""
    out = ctx.out
    out.heading("still running?")
    try:
        containers = running(ctx)
    except HostError as exc:
        out.status("FAIL", f"cannot tell: {exc}")
        return False
    if not containers:
        out.status("ok", f"no container of {ctx.settings.user} is running")
        return True
    for c in containers:
        out.status("FAIL", f"{running_describe(c)} [{c.app or 'no app'}]", [stop_hint(ctx, c)])
    out.write(f"  → {plural(len(containers), 'container')} still running")
    return False


def restart_app(ctx: Context, app: str) -> bool:
    stopped = stop_app(ctx, app)
    ctx.out.write()
    return start_app(ctx, app) and stopped


def status_app(ctx: Context, app: str, containers: Optional[List[Container]] = None) -> bool:
    """The app's units as systemd sees them, plus whatever else of it podman runs."""
    host, out = ctx.host, ctx.out
    out.heading(app)
    src, inst = ctx.sources(app), ctx.installed(app)
    mine = [c for c in containers or [] if c.app == app]
    plain = ctx.user_units(app)
    if not inst.exists and not plain:
        out.status("info", "not installed by redh" + (f" — redh install {app}"
                                                      if src.units or src.user_units else ""))
        for c in mine:
            out.status("ok", f"running: {running_describe(c)}",
                       [f"from {c.source_path}"] if c.source_path else [])
        return True
    pairs: List[Tuple[str, Optional[QuadletUnit]]] = [*services_of(ctx, app),
                                                      *plain_of(ctx, app, every=True)]
    templates = [u for u in inst.units if not u.startable]
    names = [name for name, _ in pairs]
    states = host.show(names)
    healthy = health(ctx, pairs, states)
    timers = [u.name for u in plain if u.ext == ".timer"]
    next_runs = host.show(timers, ["NextElapseUSecRealtime"]) if timers else {}
    width = max((len(n) for n in names), default=0)
    ok = True
    for name, unit in pairs:
        state = states[name]
        active = state.get("ActiveState")
        if state.get("LoadState") != "loaded" and active in ("inactive", None):
            level, text = "FAIL", f"not loaded ({state.get('LoadState')})"
        else:
            level = {"active": "ok", "inactive": "info", "failed": "FAIL"}.get(active, "warn")
            if finished(state):
                level = "ok"
            text = describe(state)
        extras = [x for x in (since(state), healthy.get(name)) if x]
        trigger = f"{name[:-len('.service')]}.timer" if name.endswith(".service") else ""
        if trigger in timers and active == "inactive" and state.get("Result") in ("success", None):
            level = "ok"                      # it runs when its timer says so
            extras.append(f"started by {trigger}")
        upcoming = (next_runs.get(name, {}).get("NextElapseUSecRealtime") or "").split()
        if len(upcoming) >= 3 and active == "active":
            extras.append(f"next run {upcoming[1]} {upcoming[2][:5]}")
        if healthy.get(name) == "unhealthy":
            level = "warn"
        try:
            restarts = int(state.get("NRestarts") or 0)
        except ValueError:
            restarts = 0
        if restarts:
            extras.append(f"{plural(restarts, 'restart')}")
        ok = ok and level != "FAIL"
        out.status(level, f"{name:<{width}}  {text}" + (f", {', '.join(extras)}" if extras else ""))
    for unit in templates:
        out.status("skip", f"{unit.service} is a template; instances are started by name")
    for c in mine:
        if c.unit not in names:
            out.status("warn", f"also running, outside redh: {running_describe(c)}",
                       [f"from {c.source_path}"] if c.source_path else [])
    if src.exists and ((src.units and not make_plan(ctx, app).in_sync)
                       or not sites.plan(ctx, app).in_sync
                       or not userunits.plan(ctx, app).in_sync):
        out.status("info", f"the installed copy is out of date → redh install {app}")
    return ok


def list_apps(ctx: Context, as_json: bool = False) -> int:
    """One line per app; with as_json, the same as one JSON document, for programs."""
    out = ctx.out
    warnings: List[str] = []
    apps = ctx.known_apps()
    names = [name for app in apps for name in shown(ctx, app)]
    try:
        states = ctx.host.show(names) if names else {}
    except HostError as exc:
        warnings.append(str(exc))
        states = {}
    try:
        containers: Optional[List[Container]] = running(ctx)
    except HostError as exc:
        warnings.append(f"cannot tell which containers run: {exc}")
        containers = None
    per_app = Counter(c.app for c in containers or [])
    listed = [app_listing(ctx, app, states, None if containers is None else per_app.get(app, 0))
              for app in apps]
    stray = [c for c in containers or [] if not c.app]

    if as_json:
        out.write(json.dumps({
            "root": str(ctx.settings.root),
            "apps": listed,
            "containers": None if containers is None else {
                "running": len(containers),
                "in_no_app": [{"name": c.name, "image": c.image, "unit": c.unit or None} for c in stray],
            },
            "warnings": warnings,
        }, indent=2))
        return 0

    for warning in warnings:
        out.status("warn", warning, indent=0)
    rows = [("APP", "LAYOUT", "UNITS", "ACTIVE", "RUNNING", "INSTALLED", "CADDY")]
    for entry in listed:
        services, active = entry["services"], entry["active"]
        if services and active is not None:
            unit_state = f"{active}/{services}"
        else:
            unit_state = "-" if not services else "?"
        up = "?" if entry["running"] is None else str(entry["running"] or "-")
        rows.append((entry["name"], entry["layout"] or "-", str(entry["units"] or "-"), unit_state, up,
                     entry["installed"] or "-", str(entry["caddy"] or "-")))
    if apps:
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for index, row in enumerate(rows):
            line = "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
            out.write(out.paint("1", line) if index == 0 else line)
    else:
        out.write(f"no apps under {ctx.settings.root}")

    if containers is not None:
        out.write()
        out.write(f"{plural(len(containers), 'container')} running"
                  + (f", {len(stray)} of them in no app:" if stray else "."))
        for c in stray:
            out.write(f"  {running_describe(c)}")
    return 0


def app_listing(ctx: Context, app: str, states: Dict[str, Dict[str, str]],
                up: Optional[int]) -> Dict[str, object]:
    """What list says about one app, None where it cannot tell: the table and --json alike."""
    src, inst = ctx.sources(app), ctx.installed(app)
    services = shown(ctx, app)
    site = sites.plan(ctx, app)
    units = userunits.plan(ctx, app)
    fresh_site = bool(site.add) and not site.same and not site.change
    fresh_units = bool(units.add) and not units.same and not units.change
    installed: Optional[str]
    if not src.exists:
        installed = "orphaned"
    elif not src.units and not site.wanted and not units.wanted:
        installed = None                       # nothing to install
    elif (src.units and not inst.exists) or (not src.units and (fresh_site or fresh_units)):
        installed = "no"
    else:
        in_sync = (site.in_sync and units.in_sync
                   and (not src.units or make_plan(ctx, app).in_sync))
        installed = "in sync" if in_sync else "outdated"
    relevant = counted(states, services) if states else services
    if relevant and states:
        active: Optional[int] = sum(1 for n in relevant if states[n].get("ActiveState") == "active")
    else:
        active = None if relevant else 0       # systemd could not be asked, or nothing to ask
    return {
        "name": app,
        "layout": src.layout if src.exists else None,
        "units": ((len(src.units) if src.exists and src.units else len(inst.units))
                  + (len(src.user_units) if src.exists else len(ctx.user_units(app)))),
        "services": len(relevant),
        "active": active,
        "running": up,
        "installed": installed,
        "caddy": len(src.snippets),
    }
