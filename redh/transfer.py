"""redh export, redh import: move an app to another host, as one archive.

    <app>-<date>-<time>.redh.tar.gz
        .redh/manifest.json         which app, from where, what else is in here
        .redh/volumes/<name>.tar    its named volumes
        <app>/                      its directory under /opt/podman

Only what an install cannot make again goes in: the Quadlet copies and the
Caddy snippet come back with 'redh install' on the other side.

Files in an app's directory belong to its containers' subordinate UIDs, and
those differ from host to host. So tar runs in 'podman unshare', as the podman
user: it stores the UIDs the way the containers see them (400994 becomes 995),
and on the other side they go back through that host's mapping. It also means
that extracting does not happen as root. Root only reads the archive first, to
make sure every member stays below <app>/ and .redh/.

A running app is stopped for packing and started again before anything is
sent, because a copy of a database mid-write is not a backup of it.

Everything ends up as the file of the person who ran sudo, mode 0600: the
archive holds the app's env files, unencrypted.
"""

from __future__ import annotations

import datetime
import gzip
import json
import os
import pwd
import re
import shutil
import socket
import tarfile
import tempfile
import zlib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

from . import caddy, confine, lifecycle
from .app import Context
from .checks import full_ref
from .common import die, human_size, join_names, plural
from .config import APP_NAME_RE
from .prune import delete_app_dir
from .running import running
from .units import PlainUnit, QuadletUnit

FORMAT = 1
SUFFIX = ".redh.tar.gz"
MANIFEST = ".redh/manifest.json"
VOLUMES = ".redh/volumes"
# Where a plain unit's commands may live and still be there on the other host.
SYSTEM_PROGRAMS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/", "/usr/lib/", "/usr/libexec/")
OUTGOING = ".redh-outgoing"      # below Downloads: where an archive waits to be sent
FILE_NAME = re.compile(r"^(.+)-\d{8}-\d{6}" + re.escape(SUFFIX) + "$")
USER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
HOST_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")
# rsync: a partial file stays hidden, so that import never takes one for an archive.
RSYNC = ["rsync", "--partial-dir=.rsync-partial", "--times", "--perms", "--chmod=F600",
         "--protect-args", "--info=progress2"]


@dataclass
class Person:
    """Who ran redh: behind sudo that is SUDO_USER, not root."""
    name: str
    uid: int
    gid: int
    home: str

    @property
    def downloads(self) -> str:
        return os.path.join(self.home, "Downloads")


def invoker(host) -> Person:
    entry = pwd.getpwuid(os.geteuid())
    if host.is_root and os.environ.get("SUDO_USER"):
        with suppress(KeyError):
            entry = pwd.getpwnam(os.environ["SUDO_USER"])
    return Person(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir)


@dataclass
class Destination:
    remote: str = ""             # what rsync gets: user@host:path/
    directory: str = ""          # or a directory on this host


def parse_destination(text: Optional[str], me: Person) -> Destination:
    """None: my Downloads. host, user@host, [user@]host:path: rsync. A path: that directory."""
    if text is None:
        return Destination(directory=me.downloads)
    if ":" not in text and ("/" in text or text in (".", "..")):
        return Destination(directory=os.path.abspath(text))
    target, _, path = text.partition(":")
    user, _, host = target.rpartition("@")
    user = user or me.name
    if not HOST_NAME.match(host) or not USER_NAME.match(user) or path.startswith(":"):
        die(f"cannot send to {text!r}: name a host, user@host, user@host:path or a directory")
    # Relative to the home directory there, wherever that is.
    return Destination(remote=f"{user}@{host}:{(path or 'Downloads').rstrip('/') or ''}/")


# -- what belongs to an app ----------------------------------------------------

def named_volumes(ctx: Context, units: Sequence[QuadletUnit]) -> List[str]:
    """The named volumes the app's units make or mount, and that exist."""
    names: List[str] = [u.resource_name for u in units if u.ext == ".volume" and u.resource_name]
    for u in units:
        for spec in u.volume_specs():
            source, colon, _ = spec.partition(":")
            if colon and not source.startswith((".", "/", "%")) and not source.endswith(".volume"):
                names.append(source)
        for spec in u.mount_specs():
            fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
            source = fields.get("source") or fields.get("src") or ""
            if fields.get("type", "volume") == "volume" and source and not source.endswith(".volume"):
                names.append(source)
    return [n for n in dict.fromkeys(names) if APP_NAME_RE.match(n)
            and ctx.host.podman("volume", "exists", n, timeout=30).ok]


def left_out(ctx: Context, app: str, units: Sequence[QuadletUnit]) -> List[str]:
    """What the app needs that does not fit into its archive."""
    s = ctx.settings
    base = s.app_dir(app) + os.sep
    mine = {u.filename for u in units}
    built = {full_ref(u.get("ImageTag")) for u in units if u.ext == ".build" and u.get("ImageTag")}
    notes: List[str] = []
    for u in units:
        paths = [source for _, source in u.bind_sources()]
        paths += [spec.split(":", 1)[0] for spec in u.volume_specs() if spec.startswith("%h/")]
        paths += u.env_files()
        for path in dict.fromkeys(paths):
            path = path.replace("%h", s.home)
            if path.startswith("/") and not path.startswith(base):
                notes.append(f"{u.filename}: {path} is outside {base} and stays here")
        for key, target in u.references():
            if target not in mine:
                notes.append(f"{u.filename}: {key}={target} belongs to another app")
        image = (u.get("Image") or "") if u.ext == ".container" else ""
        if image.startswith("localhost/") and full_ref(image) not in built:
            notes.append(f"{u.filename}: {image} was built by hand; build it there too")
    for name, path in sorted(ctx.sources(app).user_units.items()):
        try:
            plain = PlainUnit.load(path, confine.reader(s))
        except OSError:
            continue
        for command in plain.commands() + plain.ini.get_all("Service", "EnvironmentFile"):
            command = command.lstrip("-").replace("%h", s.home)
            if (command.startswith("/") and not command.startswith(base)
                    and not command.startswith(SYSTEM_PROGRAMS)):
                notes.append(f"{s.systemd_subdir}/{name}: {command} is outside {base} and stays here")
    return notes


# -- export ------------------------------------------------------------------

def writable_directory(ctx: Context, me: Person, path: str, make: bool) -> None:
    """Make sure an archive can go there, before anything has been stopped."""
    if make and not os.path.isdir(path):
        try:
            os.mkdir(path, 0o755)
            ctx.host.chown_to(path, me.uid, me.gid)
        except OSError as exc:
            die(f"cannot create {path}: {exc.strerror}")
    if not os.path.isdir(path):
        die(f"no such directory: {path}")


def pack(ctx: Context, app: str, me: Person, directory: str, volumes: Sequence[str],
         live: bool) -> Optional[str]:
    """Write the archive; returns its path, or None after saying what went wrong."""
    out, host, s = ctx.out, ctx.host, ctx.settings
    now = datetime.datetime.now().astimezone()
    name = f"{app}-{now:%Y%m%d-%H%M%S}{SUFFIX}"
    final, part = os.path.join(directory, name), os.path.join(directory, f".{name}.part")
    out.write(out.dim(f"  packing {app} …"))
    staging = tempfile.mkdtemp(prefix=".redh-export-", dir=s.root)
    try:
        # Root writes all it writes here first, and only then hands it over, deepest
        # first: once the podman user owns it, any part of a path could become a symlink.
        meta = os.path.join(staging, ".redh")
        os.mkdir(meta, 0o700)
        manifest = os.path.join(staging, MANIFEST)
        fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"format": FORMAT, "app": app, "created": now.isoformat(timespec="seconds"),
                       "host": socket.gethostname(), "root": s.root, "live": live,
                       "volumes": list(volumes)}, fh, indent=1)
        handover = [manifest]
        if volumes:
            os.mkdir(os.path.join(staging, VOLUMES), 0o700)
            handover.append(os.path.join(staging, VOLUMES))
        for path in handover + [meta, staging]:
            host.chown(path)
        for volume in volumes:
            result = host.podman("volume", "export", "--output",
                                 os.path.join(staging, VOLUMES, f"{volume}.tar"), volume, timeout=None)
            if not result.ok:
                out.status("FAIL", f"could not export volume {volume}: {result.message()}")
                return None

        tar = ["podman", "unshare", "tar", "--numeric-owner", "-czf", "-", "-C", staging, MANIFEST]
        tar += ([VOLUMES] if volumes else []) + ["-C", s.root, app]
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                os.fchmod(fd, 0o600)
                if host.is_root:
                    os.fchown(fd, me.uid, me.gid)
                result = host.stream(tar, stdout=fh)
            lines = [line for line in result.err.splitlines() if line.strip()]
            # 1 is tar's 'a file changed as I read it': what --live has asked for.
            if not (result.ok or (live and result.rc == 1)):
                out.status("FAIL", f"packing failed, nothing exported: {result.message()}", lines[:10])
                return None
            if lines:
                out.status("warn", "tar has something to say", lines[:10])
            os.replace(part, final)
        finally:
            with suppress(OSError):
                os.unlink(part)
    except OSError as exc:
        out.status("FAIL", f"packing failed, nothing exported: {exc.strerror or exc}")
        return None
    finally:
        delete_app_dir(ctx, staging)
    return final


def send(ctx: Context, me: Person, path: str, target: str) -> bool:
    """rsync as the person, whose ssh keys and agent these are; root has none."""
    out = ctx.out
    out.write(out.dim(f"  sending to {target} …"))
    agent = os.environ.get("SSH_AUTH_SOCK")
    rc = ctx.host.attached(me.name, RSYNC + [path, target],
                           {"HOME": me.home, **({"SSH_AUTH_SOCK": agent} if agent else {})})
    if rc == 0:
        out.status("ok", f"sent to {target}")
        os.unlink(path)
        with suppress(OSError):
            os.rmdir(os.path.dirname(path))
        return True
    kept = os.path.join(me.downloads, os.path.basename(path))
    os.replace(path, kept)
    with suppress(OSError):
        os.rmdir(os.path.dirname(path))
    details = [f"the archive stays in {kept}; as {me.name}, without sudo:",
               f"  {' '.join(RSYNC)} {kept} {target}"]
    if not agent and ctx.host.is_root:
        details.append("sudo drops SSH_AUTH_SOCK, so ssh had no agent to ask; "
                       "without sudo it has yours")
    out.status("FAIL", f"could not send it (rsync: exit status {rc})", details)
    return False


def export_app(ctx: Context, app: str, target: Optional[str] = None, live: bool = False,
               dry_run: bool = False) -> int:
    out, s = ctx.out, ctx.settings
    me = invoker(ctx.host)
    dest = parse_destination(target, me)
    if dest.remote and not shutil.which("rsync"):
        die("rsync is not installed: apt install rsync (the other host needs it too)")

    units = ctx.units(app)
    volumes = named_volumes(ctx, units)
    containers = [c for c in running(ctx) if c.app == app]
    out.heading(f"export {app}")
    out.write(f"  + {s.app_dir(app)}/")
    for volume in volumes:
        out.write(f"  + volume {volume}")
    out.write(f"  → {dest.remote or dest.directory + '/'}")
    for note in left_out(ctx, app, units):
        out.status("warn", f"not in the archive: {note}")
    if dry_run:
        if containers and not live:
            out.status("info", f"it would stop {plural(len(containers), 'running container')} "
                               f"for packing, and start them again")
        out.status("skip", "dry run: nothing stopped, packed or sent")
        return 0

    if dest.remote:
        writable_directory(ctx, me, me.downloads, make=True)
        directory = os.path.join(me.downloads, OUTGOING)
        writable_directory(ctx, me, directory, make=True)
    else:
        directory = dest.directory
        writable_directory(ctx, me, directory, make=directory == me.downloads)

    services = ctx.installed(app).services()
    restart = not live and any(c.unit in services for c in containers)
    path = None
    started = True
    try:
        if containers and live:
            out.status("warn", "packing while it runs: what a database is writing right now "
                               "may not survive this")
        if containers and not live and not lifecycle.stop_app(ctx, app, heading=False):
            out.status("FAIL", "it still runs, so nothing was exported (--live packs it anyway)")
        else:
            path = pack(ctx, app, me, directory, volumes, live)
    finally:
        if restart:
            started = lifecycle.start_app(ctx, app, heading=False)
    if path is None:
        return 1

    out.status("ok", f"{os.path.basename(path)}  {human_size(os.path.getsize(path))}, "
                     f"{me.name}'s alone (0600): it holds {app}'s secrets, unencrypted")
    sent = send(ctx, me, path, dest.remote) if dest.remote else True
    if sent:
        out.status("info", f"then, on the other host: sudo redh import {app}")
    return 0 if sent and started else 1


# -- import ------------------------------------------------------------------

def read_manifest(path: str) -> Optional[Dict]:
    """The manifest of a redh archive; None if this is not one. Only reads the beginning."""
    try:
        with tarfile.open(path, mode="r|gz") as tar:
            for member in tar:
                if member.isdir():
                    continue
                if member.name != MANIFEST or not member.isfile() or member.size > 1 << 20:
                    return None
                manifest = json.load(tar.extractfile(member))
                return manifest if isinstance(manifest, dict) else None
    except (OSError, EOFError, ValueError, tarfile.TarError, zlib.error):
        pass
    return None


def archives_in(directory: str) -> List[Tuple[str, Dict]]:
    """(path, manifest) of the redh archives in a directory, oldest first."""
    try:
        names = sorted(n for n in os.listdir(directory)
                       if n.endswith(SUFFIX) and not n.startswith("."))
    except OSError:
        return []
    found = [(os.path.join(directory, n), read_manifest(os.path.join(directory, n))) for n in names]
    return sorted(((p, m) for p, m in found if m), key=lambda a: str(a[1].get("created")))


def archive_names(prefix: str, **_) -> List[str]:
    """For tab completion, which runs as the person: the apps waiting in their Downloads."""
    me = pwd.getpwuid(os.geteuid())
    try:
        names = os.listdir(os.path.join(me.pw_dir, "Downloads"))
    except OSError:
        return []
    apps = {m.group(1) for m in map(FILE_NAME.match, names) if m}
    return sorted(a for a in apps if a.startswith(prefix))


def choose(me: Person, what: Optional[str]) -> Tuple[str, Dict]:
    """The archive meant: a path, an app's newest one, or the only one there is."""
    if what and (os.sep in what or what.endswith(SUFFIX)):
        manifest = read_manifest(what)
        if manifest is None:
            die(f"{what} is not an archive from redh export")
        return what, manifest
    found = archives_in(me.downloads)
    if what:
        found = [a for a in found if a[1].get("app") == what]
    if not found:
        die(f"no archive {'of ' + what + ' ' if what else ''}in {me.downloads} (*{SUFFIX})")
    apps = sorted({str(m.get("app")) for _, m in found})
    if len(apps) > 1:
        die(f"archives of {plural(len(apps), 'app')} in {me.downloads}: {join_names(apps, 8)}; "
            f"say which: redh import APP")
    return found[-1]


def unsafe_member(member: tarfile.TarInfo, app: str, links: Sequence[str],
                  read_by_root: Sequence[str]) -> Optional[str]:
    """Why this member must not be extracted."""
    name = member.name
    parts = PurePosixPath(name).parts
    if name.startswith("/") or ".." in parts or not parts:
        return "it climbs out of the archive"
    if parts[0] == ".redh":
        return None if member.isfile() or member.isdir() else "only files belong in .redh/"
    if parts[0] != app:
        return f"it is not below {app}/"
    if len(parts) == 1 and not member.isdir():
        return f"{app} is not a directory"
    if member.ischr() or member.isblk():
        return "it is a device"
    if any(str(PurePosixPath(*parts[:i])) in links for i in range(1, len(parts))):
        return "it lies behind a symlink"
    if member.islnk():
        target = PurePosixPath(member.linkname).parts
        if member.linkname.startswith("/") or ".." in target or target[:1] != (app,):
            return f"it is a hard link to {member.linkname}"
    if member.issym() and len(parts) > 1 and (len(parts) == 2 or parts[1] in read_by_root):
        # Root reads these on 'redh install'. In data/ a link means something in the container.
        target = PurePosixPath(os.path.normpath(os.path.join(os.path.dirname(name), member.linkname)))
        if member.linkname.startswith("/") or target.parts[:1] != (app,):
            return f"it is a symlink to {member.linkname}, and root reads what is in there"
    return None


def check_archive(path: str, app: str, volumes: Sequence[str],
                  read_by_root: Sequence[str]) -> Tuple[List[str], int]:
    """(what is wrong with the archive, how many members it has)."""
    problems: List[str] = []
    links: List[str] = []
    seen = set()
    count = 0
    try:
        with gzip.open(path, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r|") as tar:
                for member in tar:
                    count += 1
                    why = unsafe_member(member, app, links, read_by_root)
                    if why:
                        problems.append(f"{member.name}: {why}")
                    if member.issym() or member.islnk():      # a hard link can be to a symlink
                        links.append(str(PurePosixPath(member.name)))
                    seen.add(member.name)
            while raw.read(1 << 20):                          # gzip's checksum is at the very end
                pass
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        problems.append(f"it is damaged or incomplete: {exc}")
    if not problems and app not in seen:
        problems.append(f"{app}/ is not in it")
    problems += [f"volume {v} is not in it" for v in volumes if f"{VOLUMES}/{v}.tar" not in seen]
    return problems, count


def import_app(ctx: Context, what: Optional[str] = None, dry_run: bool = False) -> int:
    out, host, s = ctx.out, ctx.host, ctx.settings
    me = invoker(host)
    path, manifest = choose(me, what)
    app = manifest.get("app")
    volumes = manifest.get("volumes", [])
    if manifest.get("format") != FORMAT:
        die(f"{path} has format {manifest.get('format')!r}; this redh reads {FORMAT}: update it")
    if not (isinstance(app, str) and APP_NAME_RE.match(app) and isinstance(volumes, list)
            and all(isinstance(v, str) and APP_NAME_RE.match(v) for v in volumes)):
        die(f"{path}: its manifest makes no sense")

    target = s.app_dir(app)
    if not os.path.isdir(s.root):
        die(f"{s.root} does not exist")
    if app in s.ignore:
        die(f"{app} is in ignore= in {s.config_path}, so redh would not see it")
    if os.path.lexists(target) and (os.path.islink(target) or not os.path.isdir(target)
                                    or os.listdir(target)):
        die(f"{target} already exists; an import replaces nothing (redh prune {app} removes it)")
    if ctx.installed(app).exists:
        die(f"{app} is still installed from before ({ctx.installed(app).dir}/) → redh uninstall {app}")
    taken = [v for v in volumes if host.podman("volume", "exists", v, timeout=30).ok]
    if taken:
        die(f"{join_names(taken)} already exist{'s' if len(taken) == 1 else ''}; "
            f"an import replaces nothing")

    out.heading(f"import {app}")
    source = str(manifest.get("host") or "another host")
    out.write(f"  {os.path.basename(path)}  from {source}, {manifest.get('created')}, "
              f"{human_size(os.path.getsize(path))}")
    out.write(f"  + {target}/")
    for volume in volumes:
        out.write(f"  + volume {volume}")
    out.write(out.dim("  checking the archive …"))
    problems, count = check_archive(path, app, volumes, (s.quadlet_subdir, s.caddy_subdir))
    if problems:
        out.status("FAIL", "nothing imported: this archive is not safe to unpack",
                   problems[:10] + ([f"... and {len(problems) - 10} more"] if len(problems) > 10 else []))
        return 1
    out.status("ok", f"{count} entries, all below {app}/")
    if manifest.get("live"):
        out.status("warn", "it was packed while the app ran; a database may need repairing")
    if dry_run:
        out.status("skip", "dry run: nothing unpacked")
        return 0

    staging = tempfile.mkdtemp(prefix=".redh-import-", dir=s.root)
    try:
        host.chown(staging)
        with open(path, "rb") as fh:
            result = host.stream(["podman", "unshare", "tar", "--numeric-owner", "-xzpf", "-",
                                  "-C", staging], stdin=fh)
        if not result.ok:
            out.status("FAIL", f"unpacking failed, nothing imported: {result.message()}",
                       [line for line in result.err.splitlines() if line.strip()][:10])
            return 1
        unpacked = os.path.join(staging, app)
        if os.path.islink(unpacked) or not os.path.isdir(unpacked):
            out.status("FAIL", f"unpacking left no {app}/, nothing imported")
            return 1
        for volume in volumes:
            created = host.podman("volume", "create", volume, timeout=60)
            result = host.podman("volume", "import", volume,
                                 os.path.join(staging, VOLUMES, f"{volume}.tar"),
                                 timeout=None) if created.ok else created
            if not result.ok:
                out.status("FAIL", f"could not import volume {volume}: {result.message()}",
                           [f"{app}/ is not in place yet. To try again, remove the volume first: "
                            f"runuser -u {s.user} -- podman volume rm {volume}"])
                return 1
            out.status("ok", f"volume {volume}")
        # By descriptor: staging/ is the podman user's, who could swap a symlink in by now.
        fd = os.open(unpacked, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o700)
            moved = os.fstat(fd)
        finally:
            os.close(fd)
        if os.path.isdir(target):
            os.rmdir(target)
        os.rename(unpacked, target)
        placed = os.lstat(target)
        if (placed.st_dev, placed.st_ino) != (moved.st_dev, moved.st_ino):
            if not os.path.isdir(target) or os.path.islink(target):
                os.unlink(target)
            out.status("FAIL", f"{app}/ was swapped for something else while it was unpacked; "
                               f"nothing imported")
            return 1
    except OSError as exc:
        out.status("FAIL", f"could not put it in place: {exc.strerror or exc}")
        return 1
    finally:
        delete_app_dir(ctx, staging)
    ctx.forget()
    out.status("ok", f"unpacked to {target}/")

    for note in after_import(ctx, app, manifest):
        out.status("warn", note)
    out.status("info", f"{os.path.basename(path)} still holds {app}'s secrets; "
                       f"once this works: rm {path}")
    out.status("info", f"then: redh check {app} && redh install {app} && redh start {app}")
    return 0


def after_import(ctx: Context, app: str, manifest: Dict) -> List[str]:
    """What comes with the archive but is only true where it came from."""
    s = ctx.settings
    notes: List[str] = []
    old_root = str(manifest.get("root") or s.root)
    if old_root != s.root:
        notes.append(f"it came from {old_root}, this host's apps are in {s.root}: "
                     f"absolute paths in its unit files still name the old place")
    source = str(manifest.get("host") or "")
    short = source.split(".")[0]
    if short and short != socket.gethostname().split(".")[0]:
        for snippet in ctx.sources(app).snippets:
            with suppress(OSError):                     # confine.Escape too: not read, then
                addresses = caddy.site_addresses(confine.read_text(s, snippet))
                named = [a for a in addresses if short in a.split(".")]
                if named:
                    notes.append(f"{os.path.relpath(snippet, ctx.sources(app).dir)} still answers "
                                 f"to {join_names(named, 3)}, a name of {source}")
    return notes
