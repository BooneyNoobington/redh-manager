"""Tab completion: commands and options through argcomplete, and the apps' names.

Completion runs as whoever presses TAB, and that is usually not root — also
for 'sudo redh start <TAB>': the shell completes before sudo runs anything.
/opt/podman is the podman user's alone, so whenever redh runs as root it
remembers the names of the apps in /var/cache/redh/apps.json, and completion
reads them there. Only names: they are no secret, every user sees them in ps
(conmon, rootlessport) anyway. Whoever can read the apps root (root, the
podman user) gets them straight from there.

argcomplete is optional: without it, everything else works the same.
"""

from __future__ import annotations

import json
import os
import re
import stat
from typing import Callable, Dict, List, Optional

from .app import Context
from .config import COMPLETION_CACHE, load_settings

try:
    import argcomplete
except ImportError:                       # running from a checkout without it
    argcomplete = None

SHELLS = ("bash", "zsh", "fish")
KINDS = ("apps", "installed", "known")    # with sources / installed / either


def names_of(ctx: Context) -> Dict[str, List[str]]:
    apps, installed = ctx.apps(), ctx.installed_apps()
    return {"apps": apps, "installed": installed,
            "known": sorted(set(apps) | set(installed))}


def remember(ctx: Context) -> None:
    """As root, write the apps' names where completion can read them. Never fails."""
    path = ctx.settings.completion_cache
    if not ctx.host.is_root or not os.path.isdir(ctx.settings.root):
        return
    try:
        text = json.dumps(names_of(ctx), indent=1) + "\n"
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read() == text:
                    return
        except OSError:
            pass
        directory = os.path.dirname(path)
        os.makedirs(directory, 0o755, exist_ok=True)
        st = os.lstat(directory)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            return                        # not root's alone: write nothing there
        temp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                os.fchmod(fd, 0o644)
                fh.write(text)
            os.replace(temp, path)
        except BaseException:
            os.unlink(temp)
            raise
    except (OSError, ValueError):
        pass


def names(kind: str, config: Optional[str] = None) -> List[str]:
    """The names to offer: from the apps root if this user can read it, else remembered."""
    try:
        settings = load_settings(config)
    except SystemExit:                    # an unreadable config: the remembered names
        settings = None
    if settings and os.access(settings.root, os.R_OK | os.X_OK):
        return names_of(Context(settings, None, None))[kind]
    cache = settings.completion_cache if settings else COMPLETION_CACHE
    try:
        with open(cache, encoding="utf-8") as fh:
            return list(json.load(fh).get(kind, []))
    except (OSError, ValueError):
        if argcomplete is not None:
            argcomplete.warn("redh: no app names yet; 'sudo redh list' once remembers them")
        return []


def completer(kind: str) -> Callable[..., List[str]]:
    """An argcomplete completer for app arguments."""
    def complete(prefix: str, parsed_args=None, **_) -> List[str]:
        return [n for n in names(kind, getattr(parsed_args, "config", None))
                if n.startswith(prefix)]
    return complete


# The word that starts redh's own command line: redh, or a path to it.
PROGRAM = re.compile(r"(?:^|(?<=\s))(?:\S*/)?redh(?=\s|$)")


def strip_wrapper(environ) -> None:
    """'sudo redh check b' -> 'redh check b'. argcomplete's zsh code hands over the whole
    line (bash-completion has cut 'sudo' off already); argcomplete takes its first word
    for the program, and would read 'redh' as the command."""
    line, point = environ.get("COMP_LINE", ""), environ.get("COMP_POINT", "")
    found = PROGRAM.search(line)
    if found and found.start() > 0 and point.isdigit() and int(point) >= found.start():
        environ["COMP_LINE"] = line[found.start():]
        environ["COMP_POINT"] = str(int(point) - found.start())


def activate(parser) -> None:
    """Answer the shell if it is asking for completions (and exit); else nothing."""
    if argcomplete is not None and "_ARGCOMPLETE" in os.environ:
        strip_wrapper(os.environ)
        argcomplete.autocomplete(parser)


def shellcode(shell: str) -> str:
    if argcomplete is None:
        raise RuntimeError("argcomplete is not installed")
    return argcomplete.shellcode(["redh"], shell=shell)
