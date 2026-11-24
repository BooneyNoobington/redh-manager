"""Argument parsing and dispatch."""

from __future__ import annotations

import argparse
import difflib
import fcntl
import os
import sys
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence

from . import __version__, completion, lifecycle, perms
from .app import Context
from .checks import Survey, run_check
from .common import HostError, die, join_names
from .config import DEFAULT_CONFIG, load_settings
from .host import Host
from .init import init
from .install import install, uninstall
from .prune import prune
from .report import Printer
from .running import running
from .transfer import archive_names, export_app, import_app
from .update import update

# Commands that change something take a lock, so two runs cannot interleave.
MUTATING = {"init", "install", "uninstall", "start", "stop", "restart", "update", "prune",
            "export", "import"}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    # The global options work before and after the command; the copies on
    # the subcommands default to SUPPRESS so they do not overwrite each other.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="PATH", default=argparse.SUPPRESS,
                        help=f"settings file (default: {DEFAULT_CONFIG} if it exists).")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="also show what is fine, not only what is not.")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                        help="plain output, even on a terminal.")

    parser = argparse.ArgumentParser(
        prog="redh",
        description="Manage rootless Podman Quadlet apps that live under /opt/podman/<app>.",
        epilog="Run as root (or as the podman user). 'redh COMMAND -h' explains a command.",
    )
    parser.add_argument("-c", "--config", metavar="PATH", default=None,
                        help=f"settings file (default: {DEFAULT_CONFIG} if it exists).")
    parser.add_argument("-v", "--verbose", action="store_true", default=False,
                        help="also show what is fine, not only what is not.")
    parser.add_argument("--no-color", action="store_true", default=False,
                        help="plain output, even on a terminal.")
    parser.add_argument("--version", action="version", version=f"redh {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.required = True

    def command(name: str, text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[common], help=text, description=text)

    p = command("list", "one line per app: layout, units, how many run, install state.")
    p.add_argument("--json", action="store_true",
                   help="the same as one JSON document, for programs.")

    p = command("init", "create /opt/podman/APP with a skeleton to fill in: a Quadlet unit "
                        "and a Caddy site, the podman user's alone. What only you know is "
                        "marked TODO(redh init).")
    p.add_argument("app", metavar="APP")
    p.add_argument("--image", metavar="IMAGE",
                   help="the image, fully qualified: docker.io/library/nginx:1")
    p.add_argument("--port", metavar="[HOST:]PORT",
                   help="the port the app listens on in the container. It is published on "
                        "127.0.0.1, at HOST or else the first free port from 8000 up.")
    p.add_argument("--domain", metavar="NAME",
                   help="the site's address (default: APP under the domain your other sites use).")
    p.add_argument("--data", metavar="PATH",
                   help="create data/ and mount it at PATH in the container.")
    p.add_argument("--env", action="store_true",
                   help="create quadlet/APP.env for secrets and use it.")
    p.add_argument("--no-caddy", dest="site", action="store_false",
                   help="no caddy/: for an app without a web interface.")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would be created.")

    p = command("check", "check apps: layout, unit files, generator dry run, auto-update, "
                         "boot, Caddy snippet, permissions, env files, install state. "
                         "Without APP: every app.")
    p.add_argument("apps", nargs="*", metavar="APP").completer = completion.completer("known")
    p.add_argument("--fix", action="store_true",
                   help="first apply the permission policy and make env files private "
                        "(as root), then check.")

    p = command("status", "what systemd says about each unit. Without APP: every app.")
    p.add_argument("apps", nargs="*", metavar="APP").completer = completion.completer("known")

    p = command("install", "(re)install the apps' Quadlet files and reload systemd.")
    p.add_argument("apps", nargs="*", metavar="APP").completer = completion.completer("apps")
    p.add_argument("--all", action="store_true", help="every app under the apps root.")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would change, change nothing.")
    p.add_argument("--force", action="store_true",
                   help="install even though the checks found problems.")
    p.add_argument("--restart", action="store_true", help="restart the app afterwards.")

    p = command("uninstall", "stop the apps and remove their installed Quadlet files; "
                             "/opt/podman/<app> stays.")
    p.add_argument("apps", nargs="+", metavar="APP").completer = completion.completer("known")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would go, change nothing.")
    p.add_argument("--force", action="store_true", help="remove the files even if units keep running.")

    for name, text in (("start", "start every unit of the apps."),
                       ("stop", "stop every unit of the apps."),
                       ("restart", "stop, then start every unit of the apps.")):
        p = command(name, text)
        p.add_argument("apps", nargs="*", metavar="APP").completer = completion.completer("installed")
        p.add_argument("--all", action="store_true", help="every installed app.")

    p = command("update", "by hand, what podman auto-update does: build and pull the apps' "
                          "images, restart what runs an older one.")
    p.add_argument("apps", nargs="*", metavar="APP").completer = completion.completer("installed")
    p.add_argument("--all", action="store_true", help="every installed app.")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="ask podman auto-update what it would do; change nothing.")

    p = command("prune", "stop the app, remove its units, networks and volumes, "
                         "and delete /opt/podman/<app>.")
    p.add_argument("app", metavar="APP").completer = completion.completer("known")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would go, change nothing.")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation.")
    p.add_argument("--keep-volumes", action="store_true", help="leave the named volumes alone.")
    p.add_argument("--force", action="store_true",
                   help="carry on when other apps depend on it or units do not stop.")

    p = command("export", "pack the app's directory and named volumes into one archive and "
                          "send it: to your Downloads here, or with rsync to another host. "
                          "A running app is stopped for that, and started again.")
    p.add_argument("app", metavar="APP").completer = completion.completer("apps")
    p.add_argument("target", nargs="?", metavar="DESTINATION",
                   help="host, user@host or user@host:path (default path: Downloads, in the "
                        "home directory there), or a directory here. Default: your Downloads.")
    p.add_argument("--live", action="store_true",
                   help="do not stop the app; what a database is writing may not survive that.")
    p.add_argument("-n", "--dry-run", action="store_true", help="show what would happen.")

    p = command("import", "unpack an archive from 'redh export' to /opt/podman/APP. It replaces "
                          "nothing, and installs nothing: 'redh install APP' does that.")
    p.add_argument("what", nargs="?", metavar="APP|FILE",
                   help="an app's newest archive in your Downloads, or a file. "
                        "Default: the app with an archive there.").completer = archive_names
    p.add_argument("-n", "--dry-run", action="store_true", help="only check the archive.")

    p = command("completion", "print the shell code that makes TAB complete redh's commands, "
                              "options and apps, for ~/.zshrc or ~/.bashrc: "
                              "eval \"$(redh completion zsh)\"")
    p.add_argument("shell", choices=completion.SHELLS)

    completion.activate(parser)
    return parser.parse_args(argv)


def resolve(ctx: Context, names: List[str], pool: List[str], what: str) -> List[str]:
    """App names as given — also as paths, like 'nextcloud/' from tab completion."""
    apps = []
    for name in names:
        app = os.path.basename(os.path.normpath(name))
        if app not in pool and app in ctx.apps():
            die(f"{app} is not installed → redh install {app}")
        if app not in pool:
            close = difflib.get_close_matches(app, pool, n=1)
            hint = f" — did you mean {close[0]}?" if close else (
                f" ({what}: {join_names(pool, 8)})" if pool else f" (there are no {what})")
            die(f"unknown app {app!r}{hint}")
        apps.append(app)
    return list(dict.fromkeys(apps))


@contextmanager
def exclusive(path: str) -> Iterator[None]:
    """Hold a lock on the podman user's home while changing things."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            die("another redh run is busy; try again when it is done")
        yield
    finally:
        os.close(fd)


def dispatch(ctx: Context, args: argparse.Namespace) -> int:
    cmd = args.command
    if cmd == "list":
        return lifecycle.list_apps(ctx, as_json=args.json)

    if cmd == "check":
        known = ctx.known_apps()
        apps = resolve(ctx, args.apps, known, "apps") if args.apps else known
        if args.fix:
            if not ctx.host.is_root:
                die("--fix changes owners, modes and ACLs: run it as root")
            perms.fix(ctx, Survey(ctx), apps)
            ctx.out.write()
            ctx.forget()
        return run_check(ctx, apps)

    if cmd == "status":
        known = ctx.known_apps()
        apps = resolve(ctx, args.apps, known, "apps") if args.apps else known
        if not apps:
            ctx.out.write(f"no apps under {ctx.settings.root}")
        try:
            containers = running(ctx)
        except HostError as exc:
            ctx.out.status("warn", f"cannot tell which containers run: {exc}", indent=0)
            containers = None
        ok = True
        for index, app in enumerate(apps):
            if index:
                ctx.out.write()
            ok = lifecycle.status_app(ctx, app, containers) and ok
        return 0 if ok else 1

    if cmd == "init":
        return init(ctx, args.app, args.image, args.port, args.domain, args.data,
                    args.env, args.site, args.dry_run)

    if cmd == "install":
        if args.all:
            apps = [a for a in ctx.apps() if ctx.sources(a).units]
        elif args.apps:
            apps = resolve(ctx, args.apps, ctx.apps(), "apps")
        else:
            die("name the apps to install, or pass --all")
        return install(ctx, apps, args.dry_run, args.force, args.restart)

    if cmd == "uninstall":
        apps = resolve(ctx, args.apps, ctx.known_apps(), "apps")
        return uninstall(ctx, apps, args.dry_run, args.force)

    if cmd in ("start", "stop", "restart"):
        installed = ctx.installed_apps()
        if args.all:
            apps = installed
        elif args.apps:
            apps = resolve(ctx, args.apps, installed, "installed apps")
        else:
            die(f"name the apps to {cmd}, or pass --all")
        action = {"start": lifecycle.start_app, "stop": lifecycle.stop_app,
                  "restart": lifecycle.restart_app}[cmd]
        ok = True
        for index, app in enumerate(apps):
            if index:
                ctx.out.write()
            ok = action(ctx, app) and ok
        if cmd == "stop" and args.all:
            # 'stop --all' before a backup has to mean nothing runs, whoever started it.
            ctx.out.write()
            ok = lifecycle.nothing_running(ctx) and ok
        return 0 if ok else 1

    if cmd == "update":
        installed = ctx.installed_apps()
        if args.all:
            apps = installed
        elif args.apps:
            apps = resolve(ctx, args.apps, installed, "installed apps")
        else:
            die("name the apps to update, or pass --all")
        return update(ctx, apps, args.dry_run)

    if cmd == "prune":
        app = resolve(ctx, [args.app], ctx.known_apps(), "apps")[0]
        return prune(ctx, app, args.dry_run, args.yes, args.keep_volumes, args.force)

    if cmd == "export":
        app = resolve(ctx, [args.app], ctx.apps(), "apps")[0]
        return export_app(ctx, app, args.target, args.live, args.dry_run)

    if cmd == "import":
        return import_app(ctx, args.what, args.dry_run)

    raise AssertionError(cmd)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "completion":          # for anyone: it only prints
        try:
            print(completion.shellcode(args.shell), end="")
        except RuntimeError as exc:
            die(f"{exc}; install redh with pipx (see README.md), which brings it along")
        return 0
    settings = load_settings(args.config)
    host = Host(settings)
    if not (host.is_root or host.is_user):
        die(f"run redh as root (or as {settings.user})")
    out = Printer(colour=False if args.no_color else None, verbose=args.verbose)
    ctx = Context(settings, host, out)
    try:
        if args.command in MUTATING or getattr(args, "fix", False):
            with exclusive(settings.home):
                return dispatch(ctx, args)
        return dispatch(ctx, args)
    except HostError as exc:
        out.status("FAIL", str(exc), [f"is linger on? loginctl enable-linger {settings.user}"],
                   indent=0)
        return 2
    finally:
        completion.remember(ctx)                # the names TAB offers, also after sudo


def run() -> None:
    """The redh command: main(), and a quiet end on ^C or when the reader of a pipe left."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit("\naborted.")
    except BrokenPipeError:
        # Piped into head/less and the reader went away. Redirect stdout to
        # devnull so the interpreter does not print its own traceback at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
