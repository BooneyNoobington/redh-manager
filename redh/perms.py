"""The permission policy under the apps root: 'redh check' tests it, '--fix' applies it.

    /opt/podman          podman  0700
    /opt/podman/<app>    podman  0700
    Caddy's user         nothing below /opt/podman: it reads its snippets from /etc/caddy

No exceptions. Whatever Caddy's configuration still points to below the
apps root — a snippet imported in place, a file_server root, a certificate,
a CA root — is a FAIL, to be moved out. Until it is, --fix leaves the way to
it open and says so, rather than break a running site; it never grants
anything.

Rules are tested, not read off the permission bits: as root, redh takes on
the identities of 'nobody' and of Caddy's user and asks the kernel, so ACLs,
groups and every directory above count the way they count for them.
"""

from __future__ import annotations

import os
import pwd
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import confine, envfiles
from .common import join_names, mode_of, plural
from .envfiles import EVERYONE, name_of

OPS = {"r": os.R_OK, "w": os.W_OK, "x": os.X_OK}
VERBS = {"r": "list", "w": "change", "x": "enter"}
LOCKOUT = "a container mounts it; taking its bits away could lock the container's user out"
WHAT = {"file": "reads", "dir": "serves", "socket": "connects to", "missing": "names"}


@dataclass
class Breach:
    path: str
    problems: List[str]
    commands: List[List[str]] = field(default_factory=list)
    held_back: str = ""


def caddy_user(survey) -> Optional[str]:
    return survey.caddy_user if survey.caddy_user not in (None, "root") else None


def caddy_reads(ctx, survey) -> Dict[str, str]:
    """Paths below the apps root Caddy's configuration opens: path -> how."""
    reads: Dict[str, str] = {}
    for app in ctx.apps():
        for snippet in ctx.sources(app).snippets:
            if os.path.realpath(snippet) in survey.imports:
                reads[snippet] = "imports"
    for path, kind in survey.caddy_paths.items():
        reads.setdefault(path, WHAT[kind])
    return reads


def blind(survey) -> bool:
    """Caddy has a configuration, but redh cannot ask it what that opens."""
    return survey.caddyfile_exists and not survey.caddy_bin


def evaluate(ctx, survey, dirs: List[str]) -> Tuple[List[Breach], bool]:
    """Which of these directories break the policy; and whether access could be tested."""
    s, host = ctx.settings, ctx.host
    caddy = caddy_user(survey)
    allowed: Dict[Tuple[str, str], Optional[Set[str]]] = {}
    for who in filter(None, (EVERYONE, caddy)):
        groups = survey.caddy_groups if who == caddy else ()
        for letter, op in OPS.items():
            allowed[(who, letter)] = host.accessible(who, dirs, op, groups)
    tested = allowed.get((EVERYONE, "r")) is not None
    reads = caddy_reads(ctx, survey)

    breaches: List[Breach] = []
    for path in dirs:
        try:
            # An app directory that is a symlink out of the apps root is no directory of ours:
            # 'chown' and 'chmod' would hit whatever it points to.
            st = confine.stat_of(s, path)
        except OSError:
            continue
        problems: List[str] = []
        if st.st_uid != s.uid:
            problems.append(f"it belongs to {name_of(pwd.getpwuid, st.st_uid, 'pw_name')}, "
                            f"not {s.user}")
        grant = ""
        if tested:
            for letter in "rwx":
                if path in (allowed[(EVERYONE, letter)] or ()):
                    problems.append(f"every user can {VERBS[letter]} it")
                elif caddy and path in (allowed[(caddy, letter)] or ()):
                    problems.append(f"Caddy ({caddy}) can {VERBS[letter]} it")
            if caddy:
                grant = host.acl_entries(path).get(caddy, "---").strip("-")
        if grant and not any(p.startswith("Caddy") for p in problems):
            problems.append(f"an ACL entry still grants Caddy ({caddy}) {grant}")
        if not problems:
            continue

        breach = Breach(path, problems)
        if st.st_uid != s.uid:
            breach.commands.append(["chown", f"{s.user}:", path])
        needed = sorted(p for p in reads if p.startswith(path.rstrip(os.sep) + os.sep))
        if needed:
            # Closing it would take a site down: what Caddy opens has to move first.
            breach.held_back = (f"Caddy's configuration still opens {plural(len(needed), 'path')} "
                                f"below it ({join_names(needed, 2)}); move "
                                f"{'it' if len(needed) == 1 else 'them'} out first")
        elif blind(survey):
            breach.held_back = (f"{s.caddy} not found, so redh cannot tell what Caddy's "
                                f"configuration opens below it")
        elif os.path.realpath(path) in survey.mounts and stat.S_IMODE(st.st_mode) & 0o077:
            breach.held_back = LOCKOUT
        else:
            breach.commands.append(["chmod", "700", path])
            if grant:
                breach.commands.append(["setfacl", "-x", f"u:{caddy}", path])
        breaches.append(breach)
    return breaches, tested


def report(f, breaches: List[Breach], tested: bool, host_level: bool) -> None:
    for b in breaches:
        f.fail("perms", f"{b.path} ({mode_of(b.path)}): " + "; ".join(b.problems),
               [" ".join(c) for c in b.commands] + ([b.held_back] if b.held_back else []))
    if not tested and host_level:
        f.info("perms", "only root can test who gets in (ACLs, groups); owners checked")
    elif tested and not breaches:
        f.ok("perms", "only its owner gets in")


def report_reads(f, ctx, paths: List[str], reads: Dict[str, str]) -> None:
    """A FAIL for each path below the apps root Caddy's configuration opens — but for
    snippets imported in place: the caddy topic speaks about those."""
    s = ctx.settings
    for path in paths:
        how = reads[path]
        if how == "imports":
            continue
        hint = ("a CA root belongs in the system trust store" if how == "reads"
                and path.endswith((".crt", ".pem")) else f"move it out of {s.root}")
        f.fail("perms", f"Caddy {how} {path}, but nothing below {s.root} is Caddy's", [hint])


def check_host(f, ctx, survey) -> None:
    s = ctx.settings
    if not os.path.isdir(s.root):
        return
    reads = caddy_reads(ctx, survey)
    apps = [os.path.join(s.root, app) for app in ctx.apps()]
    loose = [p for p in sorted(reads) if not any(p.startswith(a + os.sep) for a in apps)]
    report_reads(f, ctx, loose, reads)
    if blind(survey):
        f.warn("perms", f"{s.caddy} not found: cannot ask Caddy what its configuration opens "
                        f"below {s.root}")
    report(f, *evaluate(ctx, survey, [s.root]), host_level=True)


def check_app(f, ctx, survey, app: str) -> None:
    src = ctx.sources(app)
    if not src.exists:
        return
    reads = caddy_reads(ctx, survey)
    report_reads(f, ctx, [p for p in sorted(reads) if p.startswith(src.dir + os.sep)], reads)
    report(f, *evaluate(ctx, survey, [src.dir]), host_level=False)


def pending(ctx, survey, apps: List[str]) -> List[Tuple[str, List[List[str]], str]]:
    """(path, commands, why it is held back) for everything not as it should be."""
    dirs = [ctx.settings.root] + [ctx.sources(app).dir for app in apps
                                  if ctx.sources(app).exists]
    todo = [(b.path, b.commands, b.held_back) for b in evaluate(ctx, survey, dirs)[0]]
    issues = envfiles.host_issues(ctx, survey)[0]
    for app in apps:
        issues += envfiles.app_issues(ctx, survey, app)[0]
    return todo + [(i.path, i.commands, i.held_back) for i in issues]


def fix(ctx, survey, apps: List[str]) -> bool:
    """Run every command the policy and the env file checks ask for; True if all worked."""
    out, host = ctx.out, ctx.host
    out.heading("fix")
    ok = True
    ran = 0
    held = 0
    for path, commands, held_back in pending(ctx, survey, apps):
        for command in commands:
            result = host.run(command, timeout=60)
            ran += 1
            ok = ok and result.ok
            out.status("ok" if result.ok else "FAIL",
                       " ".join(command) + ("" if result.ok else f" — {result.message()}"))
        if held_back:
            held += 1
            out.status("skip", f"{path}: {held_back}")
    if not ran and not held:
        out.status("ok", "nothing to fix")
    return ok
