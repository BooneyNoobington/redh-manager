"""Shared vocabulary: errors and small helpers."""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import stat
import sys
from typing import Iterable, List, NoReturn


class HostError(RuntimeError):
    """Something on the host is unreachable or broken; ends the command."""


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def die(message: str) -> NoReturn:
    sys.exit(f"error: {message}")


def plural(count: int, word: str, suffix: str = "s") -> str:
    return f"{count} {word}{'' if count == 1 else suffix}"


def rmtree(path: str) -> List[str]:
    """shutil.rmtree that carries on past errors and returns them."""
    errors: List[str] = []

    def record(failed_path: str, exc: BaseException) -> None:
        errors.append(f"{failed_path}: {getattr(exc, 'strerror', None) or exc}")

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda _f, p, exc: record(p, exc))
    else:
        shutil.rmtree(path, onerror=lambda _f, p, info: record(p, info[1]))
    return errors


def join_names(names: Iterable[str], limit: int = 6) -> str:
    """'a, b, c' — or 'a, b, c and 4 more' for long lists."""
    names = list(names)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" and {len(names) - limit} more"


def human_size(size: float) -> str:
    """'412 MB'."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit == "B" or size >= 10 else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def mode_of(path: str) -> str:
    """'drwx------ podman:podman'."""
    try:
        st = os.stat(path)
    except OSError:
        return "?"
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return f"{stat.filemode(st.st_mode)} {owner}:{group}"
