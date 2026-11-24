"""Where an app's files are: its sources under the apps root, its installed copy.

The convention is

    /opt/podman/<app>/quadlet/   Quadlet units, drop-ins and files they use
    /opt/podman/<app>/systemd/   plain units: timers and the services they start
    /opt/podman/<app>/caddy/     the Caddy snippet(s)

and installing copies quadlet/ to <Quadlet dir>/<app>/, one subdirectory per
app (Quadlet searches subdirectories since podman 4.7). Apps that still keep
their unit files at the top level of /opt/podman/<app> are understood too;
then only the unit files and their drop-in directories are installed.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import confine
from .common import die
from .confine import Escape
from .config import APP_NAME_RE, Settings
from .sites import HEADER_RE, app_of
from .units import (PLAIN_UNIT_EXTS, PlainUnit, QuadletUnit, is_dropin_dir, quadlet_ext,
                    read_file, sort_key)

SNIPPET_SUFFIXES = (".caddy", ".caddyfile")


def junk(name: str) -> bool:
    """Dotfiles, editor backups and redh's own temporary files."""
    return name.startswith(".") or name.endswith("~")


def walk_files(base: str, settings: Optional[Settings] = None,
               escapes: Optional[List[Escape]] = None) -> Dict[str, str]:
    """Every regular file below base, keyed by its path relative to base.

    A symlink counts if it leads to a regular file where it has to stay (see
    confine); one that leads out of there goes into escapes instead, and a
    symlink to a directory is not walked into. Without settings nothing is
    confined: that is for directories that are redh's own.
    """
    files: Dict[str, str] = {}

    def note(escape: Escape) -> None:
        if escapes is not None and all(known.link != escape.link for known in escapes):
            escapes.append(escape)

    try:
        for dirpath, dirnames, filenames, dirfd in confine.walk(settings, base):
            dirnames[:] = sorted(d for d in dirnames if not junk(d))
            for name in sorted(dirnames + [n for n in filenames if not junk(n)]):
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
                    if stat.S_ISLNK(st.st_mode):
                        st = confine.stat_of(settings, path)
                except Escape as escape:
                    note(escape)
                    continue
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    files[os.path.relpath(path, base)] = path
    except Escape as escape:
        note(escape)
    except OSError:
        pass
    return files


def unit_relpaths(files: Dict[str, str]) -> List[str]:
    """The Quadlet unit files in a file set, leaving out drop-in directories."""
    found = []
    for rel in files:
        parts = rel.split(os.sep)
        if quadlet_ext(parts[-1]) and not any(p.endswith(".d") for p in parts[:-1]):
            found.append(rel)
    return sorted(found)


def load_units(base: str, files: Dict[str, str],
               read: Callable[[str], str] = read_file) -> Tuple[List[QuadletUnit], List[Tuple[str, str]]]:
    units, unreadable = [], []
    for rel in unit_relpaths(files):
        search = [base]
        subdir = os.path.dirname(os.path.join(base, rel))
        if subdir != base:
            search.append(subdir)
        try:
            units.append(QuadletUnit.load(files[rel], search, read))
        except OSError as exc:
            unreadable.append((rel, exc.strerror or str(exc)))
    units.sort(key=sort_key)
    return units, unreadable


def is_snippet_name(name: str) -> bool:
    lower = name.lower()
    return lower == "caddyfile" or lower.endswith(SNIPPET_SUFFIXES)


@dataclass
class Sources:
    app: str
    dir: str
    exists: bool = False
    layout: str = "none"                 # "quadlet/", "top level" or "none"
    base: Optional[str] = None           # the directory that gets installed
    files: Dict[str, str] = field(default_factory=dict)   # install path -> source
    units: List[QuadletUnit] = field(default_factory=list)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)
    plain_units: List[str] = field(default_factory=list)
    stray: List[str] = field(default_factory=list)         # unit files outside quadlet/
    snippets: List[str] = field(default_factory=list)
    snippet_layout: str = "none"
    stray_snippets: List[str] = field(default_factory=list)
    escapes: List[Escape] = field(default_factory=list)    # symlinks out of the app: not read
    user_units: Dict[str, str] = field(default_factory=dict)   # systemd/: name -> path
    user_layout: str = "none"            # "systemd/" or "none"
    user_stray: List[str] = field(default_factory=list)     # in systemd/, but no unit file
    user_misnamed: Optional[str] = None  # 'SystemD': the directory, but spelled otherwise

    @property
    def linked_out(self) -> bool:
        """The app's directory itself is a symlink out of the apps root."""
        return any(escape.link == self.dir for escape in self.escapes)


@dataclass
class Installed:
    app: str
    dir: str
    exists: bool = False
    files: Dict[str, str] = field(default_factory=dict)
    units: List[QuadletUnit] = field(default_factory=list)
    unreadable: List[Tuple[str, str]] = field(default_factory=list)

    def services(self) -> List[str]:
        return [u.startable for u in self.units if u.startable]


def scan_sources(settings: Settings, app: str) -> Sources:
    src = Sources(app, settings.app_dir(app))
    if not os.path.isdir(src.dir):
        return src
    src.exists = True
    try:
        top = sorted(confine.listdir(settings, src.dir))
    except Escape as escape:
        src.escapes.append(escape)
        return src
    except OSError as exc:
        die(f"cannot read {src.dir}: {exc.strerror}")

    def at_top(name: str) -> str:
        return os.path.join(src.dir, name)

    def leads_to(name: str, kind: Callable[[int], bool]) -> bool:
        """Is this a file (stat.S_ISREG) or directory (stat.S_ISDIR)? A link out is noted."""
        try:
            return kind(confine.stat_of(settings, at_top(name)).st_mode)
        except Escape as escape:
            src.escapes.append(escape)
        except OSError:
            pass
        return False

    top_units = [n for n in top if quadlet_ext(n) and leads_to(n, stat.S_ISREG)]
    top_dropins = [n for n in top if is_dropin_dir(n) and leads_to(n, stat.S_ISDIR)]

    quadlet_dir = at_top(settings.quadlet_subdir)
    if leads_to(settings.quadlet_subdir, stat.S_ISDIR):
        src.layout = settings.quadlet_subdir + "/"
        src.base = quadlet_dir
        src.files = walk_files(quadlet_dir, settings, src.escapes)
        src.stray = [at_top(n) for n in top_units + top_dropins]
    elif top_units:
        src.layout = "top level"
        src.base = src.dir
        src.files = {n: at_top(n) for n in top_units}
        for name in top_dropins:
            for rel, path in walk_files(at_top(name), settings, src.escapes).items():
                src.files[os.path.join(name, rel)] = path

    if src.base:
        src.units, src.unreadable = load_units(src.base, src.files, confine.reader(settings))
        src.plain_units = sorted(rel for rel in src.files
                                 if os.path.splitext(rel)[1] in PLAIN_UNIT_EXTS)

    user_dir = at_top(settings.systemd_subdir)
    # By its name as listed: a case-insensitive file system (macOS) finds SystemD/ too.
    if settings.systemd_subdir in top and leads_to(settings.systemd_subdir, stat.S_ISDIR):
        src.user_layout = settings.systemd_subdir + "/"
        try:
            names = sorted(confine.listdir(settings, user_dir))
        except Escape as escape:
            src.escapes.append(escape)
            names = []
        except OSError:
            names = []
        for name in names:
            if junk(name):
                continue
            path = os.path.join(user_dir, name)
            try:
                mode = confine.stat_of(settings, path).st_mode
            except Escape as escape:
                src.escapes.append(escape)
                continue
            except OSError:
                continue
            if stat.S_ISREG(mode) and os.path.splitext(name)[1] in PLAIN_UNIT_EXTS:
                src.user_units[name] = path
            else:
                src.user_stray.append(name + ("/" if stat.S_ISDIR(mode) else ""))
    else:
        src.user_misnamed = next((n for n in top if n.lower() == settings.systemd_subdir.lower()
                                  and n != settings.systemd_subdir and leads_to(n, stat.S_ISDIR)),
                                 None)

    caddy_dir = at_top(settings.caddy_subdir)
    top_snippets = [at_top(n) for n in top if is_snippet_name(n) and leads_to(n, stat.S_ISREG)]
    if leads_to(settings.caddy_subdir, stat.S_ISDIR):
        src.snippet_layout = settings.caddy_subdir + "/"
        src.snippets = sorted(walk_files(caddy_dir, settings, src.escapes).values())
        src.stray_snippets = top_snippets
    elif top_snippets:
        src.snippet_layout = "top level"
        src.snippets = top_snippets
    return src


def scan_user_units(settings: Settings) -> Dict[str, Tuple[str, Optional[str]]]:
    """The unit files in the podman user's unit directory: name -> (path, the source it was
    installed from; None if redh did not install it)."""
    found: Dict[str, Tuple[str, Optional[str]]] = {}
    try:
        names = sorted(confine.listdir(settings, settings.user_unit_dir))
    except OSError:                       # none yet; or a link out of the home (check says so)
        return found
    for name in names:
        if junk(name) or os.path.splitext(name)[1] not in PLAIN_UNIT_EXTS:
            continue
        path = os.path.join(settings.user_unit_dir, name)
        source = None
        try:
            with confine.open_file(settings, path) as fh:
                match = HEADER_RE.match(fh.readline().decode("utf-8", "replace"))
            source = match.group(1) if match else None
        except OSError:
            pass                          # a link, or not a file: not redh's
        found[name] = (path, source)
    return found


def scan_installed(settings: Settings, app: str) -> Installed:
    inst = Installed(app, settings.install_dir(app))
    if os.path.isdir(inst.dir):
        inst.exists = True
        inst.files = walk_files(inst.dir, settings)      # the host check names links out of it
        inst.units, inst.unreadable = load_units(inst.dir, inst.files, confine.reader(settings))
    return inst


class Context:
    """Settings, host, printer, and a cache of what has been scanned so far."""

    def __init__(self, settings: Settings, host, out) -> None:
        self.settings = settings
        self.host = host
        self.out = out
        self._sources: Dict[str, Sources] = {}
        self._installed: Dict[str, Installed] = {}
        self._tree: Optional[Dict[str, str]] = None
        self._tree_escapes: List[Escape] = []
        self._user_files: Optional[Dict[str, Tuple[str, Optional[str]]]] = None
        self._user_units: Dict[str, List[PlainUnit]] = {}

    def forget(self) -> None:
        """Drop cached scans after files changed on disk."""
        self._sources.clear()
        self._installed.clear()
        self._tree = None
        self._user_files = None
        self._user_units.clear()

    def apps(self) -> List[str]:
        """Directories under the apps root that are apps."""
        root = self.settings.root
        try:
            names = sorted(os.listdir(root))
        except OSError as exc:
            die(f"cannot read {root}: {exc.strerror}")
        quadlet_top = os.path.relpath(self.settings.quadlet_dir, root).split(os.sep)[0]
        return [n for n in names
                if not n.startswith(".") and n not in self.settings.ignore
                and n != quadlet_top and APP_NAME_RE.match(n)
                and os.path.isdir(os.path.join(root, n))]

    def installed_apps(self) -> List[str]:
        """Subdirectories of the Quadlet directory, one per installed app; and the apps
        whose plain units are installed."""
        qdir = self.settings.quadlet_dir
        try:
            names = sorted(os.listdir(qdir))
        except OSError:
            names = []
        apps = {n for n in names
                if not junk(n) and not n.endswith(".d") and APP_NAME_RE.match(n)
                and os.path.isdir(os.path.join(qdir, n))}
        apps |= {owner for owner in map(self.owner_of, self.user_files().values()) if owner}
        return sorted(apps)

    def owner_of(self, entry: Tuple[str, Optional[str]]) -> Optional[str]:
        """The app a copy in the user unit directory was installed for."""
        source = entry[1]
        if not source:
            return None
        app = app_of(self.settings, source)
        return app if app and APP_NAME_RE.match(app) else None

    def user_files(self) -> Dict[str, Tuple[str, Optional[str]]]:
        """The unit files in the podman user's unit directory (see scan_user_units)."""
        if self._user_files is None:
            self._user_files = scan_user_units(self.settings)
        return self._user_files

    def user_units(self, app: str) -> List[PlainUnit]:
        """The app's plain units as installed: what systemd has."""
        if app not in self._user_units:
            units = []
            for entry in self.user_files().values():
                if self.owner_of(entry) == app:
                    try:
                        units.append(PlainUnit.load(entry[0], confine.reader(self.settings)))
                    except OSError:
                        pass
            self._user_units[app] = units
        return self._user_units[app]

    def known_apps(self) -> List[str]:
        return sorted(set(self.apps()) | set(self.installed_apps()))

    def sources(self, app: str) -> Sources:
        if app not in self._sources:
            self._sources[app] = scan_sources(self.settings, app)
        return self._sources[app]

    def installed(self, app: str) -> Installed:
        if app not in self._installed:
            self._installed[app] = scan_installed(self.settings, app)
        return self._installed[app]

    def units(self, app: str) -> List[QuadletUnit]:
        """The app's units: from the sources if it has any, else from the installed copy."""
        src = self.sources(app)
        return src.units if src.exists and src.units else self.installed(app).units

    def tree(self) -> Dict[str, str]:
        """Every file below the Quadlet directory, keyed by its relative path."""
        if self._tree is None:
            qdir = self.settings.quadlet_dir
            self._tree_escapes = []
            self._tree = (walk_files(qdir, self.settings, self._tree_escapes)
                          if os.path.isdir(qdir) else {})
        return self._tree

    def tree_escapes(self) -> List[Escape]:
        """The symlinks below the Quadlet directory that lead out of the podman user's home."""
        self.tree()
        return self._tree_escapes
