"""redh init: a new app directory, with a skeleton that already follows the conventions.

    /opt/podman/<app>/                 podman's, 0700
    ├── quadlet/<app>.container        image, auto-update, a port on loopback, boot
    ├── quadlet/<app>.env              --env: for secrets
    ├── caddy/Caddyfile                the site, proxying to that port
    └── data/                          --data: mounted into the container

What only a person knows is left on a line marked TODO(redh init): 'redh
check' lists those, and 'redh install' refuses while any is left. The host
port is the first one from 8000 up that no app publishes and nothing listens
on; the site's address follows the domain the other sites use.

Directories are 0700, files 0600, everything the podman user's. That user
owns the apps root, so redh creates everything through a descriptor of the
new directory, never by a path below it: no symlink swapped in between two
steps can send a file elsewhere. The directory goes to the podman user last.
"""

from __future__ import annotations

import datetime
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import caddy, confine, sites
from .app import Context
from .checks import PLACEHOLDER, fully_qualified, marked
from .common import die, plural, rmtree
from .config import APP_NAME_RE
from .units import QuadletUnit, quadlet_ext

FIRST_PORT = 8000         # where the search for a free host port starts
LIKELY_PORT = 80          # the container port nobody named: a guess, marked TODO
# Nothing that could end a value early or mean something else in a unit file or Caddyfile.
UNSAFE = set(" \t\n\r{}#;%\"'\\")
TODO = f"# {PLACEHOLDER}:"


@dataclass
class Skeleton:
    app: str
    dir: str
    image: str = ""                      # '' leaves a TODO
    port: Optional[int] = None           # inside the container; None leaves a TODO
    host_port: Optional[int] = None      # on 127.0.0.1; None publishes nothing
    domain: str = ""
    domain_todo: bool = False
    data: str = ""                       # where data/ appears in the container
    env: bool = False
    site: bool = True
    notes: List[Tuple[str, str]] = field(default_factory=list)

    def unit(self, settings, today: str) -> str:
        a = self.app
        data_dir = os.path.join(self.dir, "data")
        lines = ["[Unit]",
                 f"Description={a}",
                 "",
                 "[Container]"]
        if not self.image:
            lines.append(f"{TODO} the image, fully qualified (docker.io/library/nginx:1)")
        lines += [f"Image={self.image}",
                  "# podman-auto-update.timer pulls newer images and restarts the container.",
                  "AutoUpdate=registry"]
        if self.host_port is not None:
            lines.append("# Loopback only" + (": Caddy is the way in (caddy/Caddyfile)."
                                               if self.site else "."))
            if self.port is None:
                lines.append(f"{TODO} the port {a} listens on inside the container")
            lines.append(f"PublishPort=127.0.0.1:{self.host_port}:{self.port or LIKELY_PORT}")
        if self.data:
            lines += [f"# What {a} keeps lives in data/, next to quadlet/.",
                      f"Volume={data_dir}:{self.data}:Z"]
        else:
            lines += [f"# Bind mounts take absolute paths; mkdir {data_dir} first.",
                      f"#Volume={data_dir}:/data:Z"]
        if self.env:
            lines += [f"# Secrets: {a}.env next to this file, the podman user's alone.",
                      f"EnvironmentFile={a}.env"]
        else:
            lines += [f"# Secrets: KEY=value lines in {a}.env next to this file, mode 0600.",
                      f"#EnvironmentFile={a}.env"]
        lines += ["",
                  "[Service]",
                  "Restart=on-failure",
                  "# The first start pulls the image.",
                  "TimeoutStartSec=900",
                  "",
                  "[Install]",
                  "WantedBy=default.target",
                  ""]
        header = [f"# {a}: made by redh init on {today}. Every key: man 5 podman-systemd.unit"]
        if any(line.startswith(TODO) for line in lines):
            header.append(f"# Fill in the TODO lines, then delete them; 'redh check {a}' "
                          f"lists what is left.")
        return "\n".join(header + [""] + lines)

    def env_file(self) -> str:
        return (f"# Secrets for {self.app}: KEY=value, one per line. Only the podman user\n"
                f"# can read this file; 'redh check' says so when that changes.\n")

    def snippet(self, settings, today: str) -> str:
        installed = sites.dest_name(self.app, "caddy", "caddy/Caddyfile")
        lines = [f"# {self.app}: made by redh init on {today}.",
                 f"# 'redh install {self.app}' puts it into {settings.caddy_dir}/{installed}.",
                 ""]
        if self.domain_todo:
            lines.append(f"{TODO} the site's address")
        lines += [f"{self.domain} {{",
                  f"\treverse_proxy 127.0.0.1:{self.host_port}",
                  "}",
                  ""]
        return "\n".join(lines)

    def entries(self, settings, today: str) -> List[Tuple[str, Optional[str]]]:
        """(path relative to the app's directory, content; None for a directory)."""
        q, c = settings.quadlet_subdir, settings.caddy_subdir
        found: List[Tuple[str, Optional[str]]] = [
            (q, None), (f"{q}/{self.app}.container", self.unit(settings, today))]
        if self.env:
            found.append((f"{q}/{self.app}.env", self.env_file()))
        if self.site:
            found += [(c, None), (f"{c}/Caddyfile", self.snippet(settings, today))]
        if self.data:
            found.append(("data", None))
        return found


# -- what the skeleton needs to know ---------------------------------------

def unsafe(value: str) -> bool:
    return not value or bool(UNSAFE & set(value))


def ports_taken(ctx: Context) -> Dict[int, str]:
    """Host port -> who has it: every app's units, loose ones, and what listens."""
    taken: Dict[int, str] = {}

    def add(unit: QuadletUnit, who: str) -> None:
        for spec in unit.publish_ports():
            parsed = caddy.parse_publish(spec)
            if parsed:
                for port in range(parsed[1], parsed[2] + 1):
                    taken.setdefault(port, f"{who} publishes it ({unit.filename})")

    for app in ctx.known_apps():
        for unit in ctx.sources(app).units + ctx.installed(app).units:
            add(unit, app)
    for rel, path in ctx.tree().items():
        if os.sep not in rel and quadlet_ext(rel):
            try:
                add(QuadletUnit.load(path, read=confine.reader(ctx.settings)), path)
            except OSError:
                pass
    for port in ctx.host.listening_ports():
        taken.setdefault(port, "something on this host listens on it")
    return taken


def unit_owner(ctx: Context, app: str) -> Optional[str]:
    """Who already has <app>.container, or a unit that becomes <app>.service."""
    filename, service = f"{app}.container", f"{app}.service"
    for other in ctx.apps():
        if any(u.filename == filename or u.service == service for u in ctx.sources(other).units):
            return f"{other} ({ctx.settings.app_dir(other)})"
    for rel in ctx.tree():
        if os.path.basename(rel) == filename:
            return os.path.join(ctx.settings.quadlet_dir, rel)
    return None


def parent_domain(address: str) -> Optional[str]:
    """'internal' for forge.internal or https://*.internal:443; None for an IP and the like."""
    host = address.split("://", 1)[-1].split("/", 1)[0].lower()
    if host.count(":") == 1:
        host = host.partition(":")[0]
    if host.startswith("*."):
        return host[2:] or None
    if "." not in host or host.replace(".", "").isdigit():
        return None
    return host.split(".", 1)[1]


def site_domain(ctx: Context) -> Tuple[Optional[str], int]:
    """The domain most of Caddy's sites are under, and how many are."""
    s = ctx.settings
    paths = [s.caddyfile]
    try:
        paths += sorted(os.path.join(s.caddy_dir, n) for n in os.listdir(s.caddy_dir)
                        if n.endswith(".caddy"))
    except OSError:
        pass
    for app in ctx.apps():
        paths += ctx.sources(app).snippets
    addresses = set()
    for path in paths:
        try:
            addresses.update(caddy.site_addresses(confine.read_text(s, path)))
        except OSError:
            continue
    counts = Counter(filter(None, map(parent_domain, addresses)))
    if not counts:
        return None, 0
    return counts.most_common(1)[0]


def parse_port(value: str) -> Tuple[Optional[int], int]:
    """'8080' -> (None, 8080); '8081:80' -> (8081, 80)."""
    outer, _, inner = value.rpartition(":")
    try:
        ports = [int(p) for p in ((outer, inner) if outer else (inner,))]
    except ValueError:
        die(f"--port takes PORT or HOST_PORT:PORT, not {value!r}")
    if not all(0 < p < 65536 for p in ports):
        die(f"--port {value}: ports go from 1 to 65535")
    return (ports[0], ports[1]) if outer else (None, ports[0])


def plan(ctx: Context, app: str, image: Optional[str] = None, port: Optional[str] = None,
         domain: Optional[str] = None, data: Optional[str] = None, env: bool = False,
         site: bool = True) -> Skeleton:
    """The skeleton for a new app; dies on anything that would make it wrong."""
    s = ctx.settings
    if os.sep in app.rstrip(os.sep) and os.path.dirname(os.path.abspath(app)) == s.root:
        app = os.path.basename(os.path.abspath(app))      # /opt/podman/web/ means web
    app = app.rstrip(os.sep)
    if not APP_NAME_RE.match(app):
        die(f"{app!r} cannot be an app: letters, digits, '.', '_' and '-', "
            f"starting with a letter or digit")
    if app in s.ignore:
        die(f"{app} is in ignore= in {s.config_path}, so redh would not see it")
    if not os.path.isdir(s.root):
        die(f"{s.root} does not exist")
    sk = Skeleton(app, s.app_dir(app), env=env, site=site)
    if os.path.lexists(sk.dir):
        if os.path.islink(sk.dir) or not os.path.isdir(sk.dir):
            die(f"{sk.dir} exists and is not a directory")
        if os.listdir(sk.dir):
            die(f"{sk.dir} already exists → redh check {app}")
    if ctx.installed(app).exists:
        die(f"{app} is still installed from before ({ctx.installed(app).dir}/) "
            f"→ redh uninstall {app}, or choose another name")
    owner = unit_owner(ctx, app)
    if owner:
        die(f"{owner} already has {app}.container or {app}.service; Quadlet would "
            f"load only one of them")

    if image is not None:
        if unsafe(image) or not fully_qualified(image):
            die(f"--image needs a fully-qualified image, not {image!r}: like "
                + (f"docker.io/library/{image}" if "/" not in image else f"docker.io/{image}"))
        sk.image = image
    if data is not None:
        if unsafe(data) or ":" in data or not data.startswith("/"):
            die(f"--data takes the absolute path inside the container, like /data; not {data!r}")
        sk.data = data

    wanted_host = None
    if port is not None:
        wanted_host, sk.port = parse_port(port)
    if site or port is not None:
        taken = ports_taken(ctx)
        if wanted_host is not None:
            if wanted_host in taken:
                die(f"port {wanted_host} is taken: {taken[wanted_host]}")
            sk.host_port = wanted_host
        else:
            sk.host_port = next(p for p in range(FIRST_PORT, 65536) if p not in taken)
            sk.notes.append(("info", f"host port {sk.host_port} on 127.0.0.1: the first from "
                                     f"{FIRST_PORT} up that is free"))

    if site:
        if domain is not None:
            if unsafe(domain):
                die(f"--domain takes a site address, like {app}.example.org; not {domain!r}")
            sk.domain = domain
        else:
            parent, count = site_domain(ctx)
            if parent:
                sk.domain = f"{app}.{parent}"
                sk.notes.append(("info", f"site {sk.domain}, like {plural(count, 'other site')} "
                                         f"(--domain chooses another)"))
            else:
                sk.domain, sk.domain_todo = f"{app}.example.org", True
    elif domain is not None:
        die("--domain and --no-caddy do not go together")
    return sk


# -- making it ---------------------------------------------------------------

def create(ctx: Context, sk: Skeleton, entries: List[Tuple[str, Optional[str]]]) -> None:
    host = ctx.host
    made = False
    try:
        os.mkdir(sk.dir, 0o700)
        made = True
    except FileExistsError:
        pass
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds: Dict[str, int] = {}
    try:
        fds[""] = os.open(sk.dir, flags)
        if not made and os.listdir(fds[""]):
            raise FileExistsError(f"{sk.dir} is no longer empty")
        for rel, content in entries:
            parent, name = fds[os.path.dirname(rel)], os.path.basename(rel)
            if content is None:
                os.mkdir(name, 0o700, dir_fd=parent)
                fds[rel] = fd = os.open(name, flags, dir_fd=parent)
                os.fchmod(fd, 0o700)
                host.chown_fd(fd)
                continue
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fd, 0o600)
                host.chown_fd(fd)
                fh.write(content)
        # Last: until now only root could get in.
        os.fchmod(fds[""], 0o700)
        host.chown_fd(fds[""])
    except BaseException:
        if made:
            rmtree(sk.dir)
        raise
    finally:
        for fd in fds.values():
            os.close(fd)


def init(ctx: Context, app: str, image: Optional[str] = None, port: Optional[str] = None,
         domain: Optional[str] = None, data: Optional[str] = None, env: bool = False,
         site: bool = True, dry_run: bool = False) -> int:
    out, s = ctx.out, ctx.settings
    sk = plan(ctx, app, image, port, domain, data, env, site)
    app = sk.app
    entries = sk.entries(s, datetime.date.today().isoformat())

    out.heading(f"init {app}")
    out.write(f"  + {sk.dir}/")
    for rel, content in entries:
        out.write(f"  + {rel}{'/' if content is None else ''}")
    for level, text in sk.notes:
        out.status(level, text)
    left = [line for rel, content in entries if content is not None
            for line in marked(rel, content.splitlines())]
    if dry_run:
        out.status("skip", "dry run: nothing created")
    else:
        try:
            create(ctx, sk, entries)
        except OSError as exc:
            out.status("FAIL", f"cannot create {sk.dir}: {exc.strerror or exc}")
            return 1
        out.status("ok", f"created: {s.user}'s alone, directories 0700, files 0600")
        ctx.forget()
    if left:
        out.status("info", f"{plural(len(left), 'line')} marked {PLACEHOLDER} to fill in", left)
    out.status("info", f"then: redh check {app} && redh install {app} && redh start {app}")
    return 0
