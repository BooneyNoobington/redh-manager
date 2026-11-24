"""Env files and other secrets in plain text, and who besides their owner gets at them.

Looked at are files whose name has, as a part of its own, 'env' (.env, db.env,
env.list) or a word for a secret: pw, password, secret, token, key, credentials
(.ca.pw, api-key, secrets.yaml, mail.key) — but not Rentenversicherung.pdf,
nor documents, pictures and public keys, whatever their name. They are looked
for in an app's directory (a few levels deep, not into node_modules and the
like), in its install directory, at the top of the apps root and of the Quadlet
directory; and every file a unit names in EnvironmentFile= counts, wherever it is.

Each of these is a FAIL — strict, because a secret should belong to its owner
alone, not stay safe by luck:

    someone gets at it: every user of the host (tested as 'nobody'), or
    Caddy's user, the one service that faces the network;
    it is open, and only a closed directory above it keeps others out;
    its group has members besides its owner and the podman user;
    it belongs to someone other than root, the podman user and its
    containers' subordinate UIDs.

As root, 'gets at it' is tested by taking on those users' identities, so
directories, ACLs and groups count the way the kernel counts them. Without
root it is worked out from the permission bits.

The podman user decides what is in those directories and what units name, so
none of that gets to steer root. An env file that is a symlink out of its app
is a FAIL of its own and not looked at (see confine), unless a container mounts
the directory: there a link means something inside the container. 'check --fix'
only touches what lies below the apps root and the Quadlet directory; a file a
unit names elsewhere is reported, not changed.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import stat
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import confine
from .common import join_names, mode_of, plural
from .confine import Escape
from .units import QuadletUnit

DEPTH = 3
EVERYONE = "nobody"
SKIP_DIRS = frozenset({"node_modules", ".git", "venv", ".venv", "site-packages",
                       "__pycache__", "vendor"})
# Templates hold no secrets; code and shell plugins only mention env in their names.
SKIP_SUFFIXES = (".example", ".sample", ".template", ".tmpl", ".tpl", ".dist", ".default",
                 ".py", ".pyc", ".js", ".mjs", ".cjs", ".ts", ".go", ".rb", ".php", ".java",
                 ".rs", ".c", ".h", ".md", ".html", ".css", ".map", ".lock", ".zsh", ".fish")
# Documents and pictures say 'password' in their titles (a paperless export), and a
# public key is meant to be read.
SKIP_SUFFIXES += (".pub", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
                  ".heic", ".tif", ".tiff", ".bmp", ".mp3", ".mp4", ".mov", ".mkv", ".wav",
                  ".doc", ".docx", ".odt", ".ods", ".xls", ".xlsx", ".ppt", ".pptx", ".epub", ".eml")
# A part of the name on its own, between dots, dashes and the like — not inside a word.
SECRET_PARTS = frozenset({"env", "envs", "environment", "envfile", "dotenv",
                          "pw", "passwd", "password", "passwords", "secret", "secrets",
                          "token", "tokens", "key", "privkey", "credentials"})
NAME_PARTS = re.compile(r"[^a-z0-9]+")


def looks_secret(name: str) -> bool:
    lower = name.lower()
    return bool(SECRET_PARTS & set(NAME_PARTS.split(lower))) and not lower.endswith(SKIP_SUFFIXES)


def is_file(settings, path: str, escapes: List[Escape]) -> bool:
    """Does path lead to a regular file? A symlink out of where it has to stay is noted instead."""
    try:
        return stat.S_ISREG(confine.stat_of(settings, path).st_mode)
    except Escape as escape:
        escapes.append(escape)
    except OSError:
        pass
    return False


def find(settings, base: str, escapes: List[Escape], depth: int = DEPTH) -> List[str]:
    """Env-looking files below base, at most depth levels down."""
    found: List[str] = []
    top = base.rstrip(os.sep).count(os.sep)
    try:
        for dirpath, dirnames, filenames, _ in confine.walk(settings, base):
            level = dirpath.count(os.sep) - top
            dirnames[:] = [] if level + 1 >= depth else sorted(
                d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                path = os.path.join(dirpath, name)
                if looks_secret(name) and is_file(settings, path, escapes):
                    found.append(path)
    except Escape as escape:
        escapes.append(escape)
    except OSError:
        pass
    return found


def referenced(settings, units: Iterable[QuadletUnit], escapes: List[Escape]) -> List[str]:
    """Files the units read with EnvironmentFile=, in [Container] or [Service]."""
    found: List[str] = []
    for u in units:
        here = os.path.dirname(u.path)
        names = u.env_files() + [w.lstrip("-") for w in u.ini.get_words("Service", "EnvironmentFile")]
        for name in names:
            if "%" in name:
                continue
            path = name if os.path.isabs(name) else os.path.normpath(os.path.join(here, name))
            if is_file(settings, path, escapes):
                found.append(path)
    return found


def shield(path: str) -> Optional[str]:
    """The nearest directory above path that others cannot pass, going by its bits."""
    directory = os.path.dirname(path)
    while True:
        try:
            if not os.stat(directory).st_mode & stat.S_IXOTH:
                return directory
        except OSError:
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def name_of(lookup: Callable, ident: int, attr: str) -> str:
    try:
        return getattr(lookup(ident), attr)
    except KeyError:
        return str(ident)


@dataclass
class Issue:
    path: str
    text: str                                   # 'quadlet/x.env (-rw-r--r-- podman:podman): ...'
    commands: List[List[str]] = field(default_factory=list)   # what 'check --fix' runs
    held_back: str = ""                         # why --fix leaves it alone


def assess(ctx, survey, paths: Sequence[str], show: Callable[[str], str],
           mirror: Callable[[str], Optional[str]] = lambda path: None,
           mounts: Sequence[str] = ()) -> Tuple[List[Issue], List[str]]:
    """(an Issue per env file that is not the owner's alone, the names of those that are)."""
    s, host = ctx.settings, ctx.host
    unique: Dict[str, str] = {}
    for path in paths:
        unique.setdefault(os.path.realpath(path), path)
    paths = list(unique.values())
    if not paths:
        return [], []

    readable = host.accessible(EVERYONE, paths, os.R_OK)
    writable = host.accessible(EVERYONE, paths, os.W_OK)
    by_caddy = None
    if survey.caddy_user and survey.caddy_user != "root":
        by_caddy = host.accessible(survey.caddy_user, paths, os.R_OK, survey.caddy_groups)

    issues: List[Issue] = []
    private: List[str] = []
    for path in paths:
        try:
            st = confine.stat_of(s, path)
        except OSError:
            continue
        bits = stat.S_IMODE(st.st_mode)
        # Without root, 'everyone' is read from the bits of the file and the directories above.
        everyone_reads = (path in readable) if readable is not None else \
            bool(bits & stat.S_IROTH) and shield(path) is None
        everyone_writes = (path in writable) if writable is not None else \
            bool(bits & stat.S_IWOTH) and shield(path) is None

        problems: List[str] = []
        if everyone_reads:
            problems.append("every user of this host can read it")
        if everyone_writes:
            problems.append("every user of this host can change it")
        if by_caddy is not None and path in by_caddy and not everyone_reads:
            problems.append(f"{survey.caddy_user}, the user Caddy runs as, can read it")
        if bits & (stat.S_IROTH | stat.S_IWOTH) and not (everyone_reads or everyone_writes):
            # Safe by luck: one opened directory away from the first problem.
            closed = shield(path)
            problems.append(f"it is open to everyone; only {closed} ({mode_of(closed)}) "
                            f"keeps them out" if closed else "it is open to everyone")
        owner = name_of(pwd.getpwuid, st.st_uid, "pw_name")
        members = sorted(host.group_members(st.st_gid) - {"root", s.user, owner})
        if bits & (stat.S_IRGRP | stat.S_IWGRP) and members:
            group = name_of(grp.getgrgid, st.st_gid, "gr_name")
            verb = "read and change" if bits & stat.S_IWGRP else "read"
            problems.append(f"group {group} ({join_names(members, 4)}) can {verb} it")
        foreign = st.st_uid not in (0, s.uid) and not any(
            first <= st.st_uid < first + count for first, count in survey.subuids)
        if foreign:
            problems.append(f"it belongs to {owner}, who can read and change it")

        if not problems:
            private.append(show(path))
            continue
        issue = Issue(path, f"{show(path)} ({mode_of(path)}): " + "; ".join(problems))
        real = os.path.realpath(path)
        mount = next((m for m in mounts if real.startswith(m.rstrip(os.sep) + os.sep)), None)
        if mount:
            # Name the mounted directory the way the path names it, not resolved.
            above = os.path.dirname(path)
            while above != os.path.dirname(above) and os.path.realpath(above) != mount:
                above = os.path.dirname(above)
            mount = above if os.path.realpath(above) == mount else mount
        if mount:
            # Inside the container a non-root user may read it through exactly these bits.
            issue.held_back = (f"a container mounts {mount}: if it reads this file as a non-root "
                               f"user, give it to that user instead (podman unshare chown UID:GID "
                               f"{path}) and then take the bits away")
        elif confine.top_of(s, path) is None:
            # Unit files are the podman user's: naming a file there must not get it changed.
            issue.held_back = f"a unit names it, but it lies outside {s.root}: not redh's to change"
        else:
            for target in filter(None, (path, mirror(path))):
                if foreign:
                    issue.commands.append(["chown", f"{s.user}:", target])
                if bits & 0o077:
                    issue.commands.append(["chmod", "go-rwx", target])
        issues.append(issue)
    return issues, private


def link_issues(survey, escapes: Sequence[Escape], known: Iterable[Escape],
                show: Callable[[str], str]) -> List[Issue]:
    """An Issue per env file that is a symlink out of where it has to stay; known ones are
    reported as symlinks already. Nothing follows these, so only the link can be fixed."""
    seen = {escape.link for escape in known}
    issues: List[Issue] = []
    for escape in escapes:
        here = os.path.realpath(os.path.dirname(escape.link))
        if escape.link in seen or any(here == m or here.startswith(m.rstrip(os.sep) + os.sep)
                                      for m in survey.mounts):
            continue
        seen.add(escape.link)
        issues.append(Issue(escape.link, f"{show(escape.link)} points to {escape.target}, "
                                         f"outside {escape.top}: not looked at",
                            held_back=f"remove the link, or keep the file below {escape.top}"))
    return issues


def report(f, issues: Sequence[Issue], private: Sequence[str]) -> None:
    for issue in issues:
        f.fail("secrets", issue.text, [" ".join(c) for c in issue.commands]
               + ([issue.held_back] if issue.held_back else []))
    if private:
        f.ok("secrets", f"{plural(len(private), 'file')} with secrets only the owner can read: "
                        f"{join_names(private, 4)}")


def app_issues(ctx, survey, app: str) -> Tuple[List[Issue], List[str]]:
    s = ctx.settings
    src, inst = ctx.sources(app), ctx.installed(app)
    escapes: List[Escape] = []
    paths = find(s, src.dir, escapes) if src.exists else []
    paths += find(s, inst.dir, escapes) if inst.exists else []
    paths += referenced(s, src.units + inst.units, escapes)

    def copy_of(path: str) -> Optional[str]:
        """The installed copy of a file in quadlet/; it is fixed along with its source."""
        if src.base and inst.exists and path.startswith(src.base + os.sep):
            rel = os.path.relpath(path, src.base)
            return inst.files.get(rel)
        return None

    def mirrored(path: str) -> bool:
        return path.startswith(inst.dir + os.sep) and os.path.relpath(path, inst.dir) in src.files

    def show(path: str) -> str:
        return os.path.relpath(path, src.dir) if path.startswith(src.dir + os.sep) else path

    mounts = sorted(survey.mounts, key=len, reverse=True)
    issues, private = assess(ctx, survey, [p for p in paths if not mirrored(p)], show, copy_of, mounts)
    return link_issues(survey, escapes, src.escapes + ctx.tree_escapes(), show) + issues, private


def host_issues(ctx, survey) -> Tuple[List[Issue], List[str]]:
    """Env files lying loose at the top of the apps root and of the Quadlet directory."""
    s = ctx.settings
    escapes: List[Escape] = []
    paths = find(s, s.root, escapes, depth=1) if os.path.isdir(s.root) else []
    if os.path.isdir(s.quadlet_dir):
        paths += find(s, s.quadlet_dir, escapes, depth=1)
        loose = []
        for rel, path in ctx.tree().items():
            if os.sep not in rel and rel.endswith(".container"):
                try:
                    loose.append(QuadletUnit.load(path, read=confine.reader(s)))
                except OSError:
                    pass
        paths += referenced(s, loose, escapes)
    issues, private = assess(ctx, survey, paths, show=lambda path: path,
                             mounts=sorted(survey.mounts, key=len, reverse=True))
    return link_issues(survey, escapes, ctx.tree_escapes(), lambda path: path) + issues, private


def check_app(f, ctx, survey, app: str) -> None:
    report(f, *app_issues(ctx, survey, app))


def check_host(f, ctx, survey) -> None:
    report(f, *host_issues(ctx, survey))
