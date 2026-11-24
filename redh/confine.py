"""Reading what the podman user owns, as root, without being sent elsewhere.

Everything below the apps root and the Quadlet directory is the podman user's
to change at any moment, and so an escaped container's. A symlink planted there
must not make root copy /etc/shadow into a file that user can read. Looking
first is no help: any part of a path can turn into a symlink between the look
and the open, and O_NOFOLLOW only guards the last part.

So root opens those files here: one part of the path at a time, each relative
to a descriptor of the directory before it, none of them following a symlink.
A symlink met on the way is resolved by hand, and only as long as it stays in
the directory the file has to stay in:

    <root>/<app>/...     the app's own directory
    <root>/<name>        the apps root
    Quadlet directory    the podman user's home, if it lies below that

Nothing is ever opened above that directory, so whatever the links say, and
whenever they change, a read cannot come out anywhere else. The walk to it
starts where the podman user has no say any more: at its home, or the apps root
if that lies elsewhere. Any other path is not the podman user's to change and
is opened as usual. Hard links are the kernel's to stop (fs.protected_hardlinks).
"""

from __future__ import annotations

import errno
import io
import os
import shutil
import stat
from contextlib import suppress
from typing import BinaryIO, Callable, Iterator, List, Optional, Sequence, Tuple, TypeVar

from .config import Settings

T = TypeVar("T")

MAX_LINKS = 40                      # as many as the kernel follows
AT_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# O_NONBLOCK: a FIFO somebody put there must not leave root waiting forever.
AT_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# What O_NOFOLLOW fails with on a symlink: ELOOP, on Linux with O_DIRECTORY ENOTDIR, on BSD EMLINK.
MAYBE_A_LINK = (errno.ELOOP, errno.ENOTDIR, errno.EMLINK)


class Escape(OSError):
    """A symlink leads out of the directory it has to stay in."""

    def __init__(self, link: str, target: str, top: str) -> None:
        super().__init__(errno.ELOOP, f"symlink to {target}, outside {top}", link)
        self.link, self.target, self.top = link, target, top

    def __str__(self) -> str:
        return f"{self.link} points to {self.target}, outside {self.top}"


def _below(path: str, top: str) -> Optional[List[str]]:
    """The parts of an absolute path below top, [] for top itself; None if it lies elsewhere."""
    path = os.path.normpath(path)
    for base in (top, os.path.realpath(top)):
        prefix = base.rstrip(os.sep) + os.sep
        if path == base:
            return []
        if path.startswith(prefix):
            return path[len(prefix):].split(os.sep)
    return None


def top_of(settings: Optional[Settings], path: str) -> Optional[str]:
    """The directory a read of path has to stay in; None if the podman user has no say over it."""
    if settings is None:
        return None
    for special in (settings.quadlet_dir, settings.user_unit_dir):
        if special and _below(path, special) is not None:
            # Copies redh installed, and links to an app from when that was done by hand.
            inside_home = _below(special, settings.home) is not None
            return settings.home if inside_home else special
    parts = _below(path, settings.root)
    if parts is None:
        return None
    return os.path.join(settings.root, parts[0]) if len(parts) > 1 else settings.root


def _walk(top: str, start: int, parts: Sequence[str], leaf: Callable[[str, int], T]) -> T:
    """leaf(name, descriptor of its directory) for the end of parts below top, which is start."""
    stack = [(start, top)]              # the directories walked through, innermost last
    todo = list(reversed(parts))
    link, target, followed = top, "", 0
    try:
        while todo:
            name = todo.pop()
            if name == os.pardir and len(stack) > 1:
                os.close(stack.pop()[0])
                continue
            if name == os.pardir:
                # Above top: all right only if the rest of the way comes straight back in.
                back = _below(os.path.join(os.path.dirname(top), *reversed(todo)), top)
                if back is None:
                    raise Escape(link, target, top)
                todo = back[::-1]
                continue
            fd, where = stack[-1]
            try:
                if not todo:
                    return leaf(name, fd)
                stack.append((os.open(name, AT_DIR, dir_fd=fd), os.path.join(where, name)))
            except OSError as exc:
                if exc.errno not in MAYBE_A_LINK:
                    raise
                try:
                    target = os.readlink(name, dir_fd=fd)
                except OSError:
                    raise exc from None
                link = os.path.join(where, name)
                followed += 1
                if followed > MAX_LINKS:
                    raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), link) from None
                if os.path.isabs(target):
                    rest = _below(target, top)
                    if rest is None:
                        raise Escape(link, target, top) from None
                    while len(stack) > 1:
                        os.close(stack.pop()[0])
                else:
                    rest = target.split(os.sep)
                todo.extend(reversed([part for part in rest if part not in ("", os.curdir)]))
        return leaf(os.curdir, stack[-1][0])
    finally:
        for fd, _ in stack[1:]:
            os.close(fd)


def _open_dir(name: str, fd: int) -> int:
    return os.open(name, AT_DIR, dir_fd=fd)


def _open_file(name: str, fd: int) -> int:
    return os.open(name, AT_FILE, dir_fd=fd)


def _lstat(name: str, fd: int) -> os.stat_result:
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if stat.S_ISLNK(st.st_mode):
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), name)
    return st


def _open_top(settings: Settings, top: str) -> int:
    """A descriptor of top, reached from where the podman user has no say any more."""
    anchor = next((a for a in (settings.home, settings.root) if _below(top, a) is not None), top)
    start = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return _walk(anchor, start, _below(top, anchor) or [], _open_dir)
    finally:
        os.close(start)


def _at(settings: Optional[Settings], path: str, leaf: Callable[[str, int], T],
        plain: Callable[[str], T]) -> T:
    if "\0" in path:                    # a unit file can say anything
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    top = top_of(settings, path)
    if top is None:
        return plain(path)
    start = _open_top(settings, top)
    try:
        return _walk(top, start, _below(path, top) or [], leaf)
    finally:
        os.close(start)


def stat_of(settings: Optional[Settings], path: str) -> os.stat_result:
    """os.stat, but an Escape where a symlink leads out of where path has to stay."""
    return _at(settings, path, _lstat, os.stat)


def open_dir(settings: Optional[Settings], path: str) -> int:
    """A descriptor of the directory path, or an Escape; the caller closes it."""
    return _at(settings, path, _open_dir, lambda p: os.open(p, os.O_RDONLY | os.O_DIRECTORY))


def open_file(settings: Optional[Settings], path: str) -> BinaryIO:
    """The regular file at path, open for reading, or an Escape."""
    fd = _at(settings, path, _open_file, lambda p: os.open(p, os.O_RDONLY | os.O_NONBLOCK))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_bytes(settings: Optional[Settings], path: str) -> bytes:
    with open_file(settings, path) as fh:
        return fh.read()


def read_text(settings: Optional[Settings], path: str) -> str:
    with io.TextIOWrapper(open_file(settings, path), encoding="utf-8", errors="replace") as fh:
        return fh.read()


def reader(settings: Settings) -> Callable[[str], str]:
    """read_text for what only wants a path, like QuadletUnit.load."""
    return lambda path: read_text(settings, path)


def listdir(settings: Optional[Settings], path: str) -> List[str]:
    fd = open_dir(settings, path)
    try:
        return os.listdir(fd)
    finally:
        os.close(fd)


def walk(settings: Optional[Settings], base: str) -> Iterator[Tuple[str, List[str], List[str], int]]:
    """Like os.fwalk(base), but a symlink out of where base has to stay is an Escape, and a
    directory swapped for a symlink halfway is not walked into."""
    fd = open_dir(settings, base)
    try:
        for dirpath, dirnames, filenames, dirfd in os.fwalk(os.curdir, dir_fd=fd):
            yield os.path.normpath(os.path.join(base, dirpath)), dirnames, filenames, dirfd
    finally:
        os.close(fd)


# -- writing -------------------------------------------------------------------
#
# Root writes into the podman user's directories too: the Quadlet copies, the
# user units. The same rule holds: nothing by path. A directory is reached as
# above (made where missing, and handed to its owner); below the directory redh
# installs into, every directory has to be a real one. A file is written new
# (O_EXCL) next to its name and renamed over it; removing never follows a link.

def _make(fd: int, name: str, owner: Callable[[int], None]) -> int:
    """name in the directory fd, made if missing (and then handed to owner); not a link."""
    created = False
    try:
        os.mkdir(name, 0o755, dir_fd=fd)
        created = True
    except FileExistsError:
        pass
    try:
        new = os.open(name, AT_DIR, dir_fd=fd)
    except OSError as exc:
        if exc.errno in MAYBE_A_LINK:
            raise OSError(exc.errno, "a symlink or not a directory; redh writes only into "
                                     "real directories", name) from None
        raise
    if created:
        try:
            owner(new)
        except BaseException:
            os.close(new)
            raise
    return new


def _reach(settings: Optional[Settings], path: str, owner: Callable[[int], None]) -> int:
    try:
        return open_dir(settings, path)
    except FileNotFoundError:
        parent = os.path.dirname(path)
        if parent == path:
            raise
        fd = _reach(settings, parent, owner)
        try:
            return _make(fd, os.path.basename(path), owner)
        finally:
            os.close(fd)


def directory(settings: Optional[Settings], base: str, rel: str = "",
              owner: Callable[[int], None] = lambda fd: None, create: bool = True) -> int:
    """A descriptor of base/rel to write in; the caller closes it.

    base is reached like a read (an Escape if a link leads out), and made if
    missing. Below it, every part of rel has to be a real directory: an install
    must not land wherever a planted link points, not even inside the home.
    """
    fd = _reach(settings, os.path.normpath(base), owner) if create else open_dir(settings, base)
    try:
        for name in [part for part in rel.split(os.sep) if part not in ("", os.curdir)]:
            if name == os.pardir:
                raise OSError(errno.EINVAL, "no way up", rel)
            if create:
                new = _make(fd, name, owner)
            else:
                try:
                    new = os.open(name, AT_DIR, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in MAYBE_A_LINK:
                        raise OSError(exc.errno, "a symlink or not a directory", name) from None
                    raise
            os.close(fd)
            fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_file(dir_fd: int, name: str, data, mode: int,
               owner: Callable[[int], None] = lambda fd: None) -> None:
    """data (bytes, or a file to copy) to name in that directory, atomically."""
    temp = f".{name}.redh-tmp"
    with suppress(FileNotFoundError):
        os.unlink(temp, dir_fd=dir_fd)             # a leftover, or a link somebody put there
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "wb") as fh:
            if isinstance(data, (bytes, bytearray)):
                fh.write(data)
            else:
                shutil.copyfileobj(data, fh)
            os.fchmod(fd, mode)
            owner(fd)
        os.replace(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with suppress(OSError):
            os.unlink(temp, dir_fd=dir_fd)
        raise


def remove(dir_fd: int, name: str) -> None:
    """Delete name in that directory: a file, a link itself, or a whole tree; nothing followed."""
    try:
        sub = os.open(name, AT_DIR, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno not in MAYBE_A_LINK:
            raise
        os.unlink(name, dir_fd=dir_fd)
        return
    try:
        for child in os.listdir(sub):
            remove(sub, child)
    finally:
        os.close(sub)
    os.rmdir(name, dir_fd=dir_fd)


def remove_empty(dir_fd: int) -> None:
    """Remove the empty directories below this one (not itself), deepest first."""
    for name in os.listdir(dir_fd):
        try:
            sub = os.open(name, AT_DIR, dir_fd=dir_fd)
        except OSError:
            continue
        try:
            remove_empty(sub)
        finally:
            os.close(sub)
        with suppress(OSError):
            os.rmdir(name, dir_fd=dir_fd)
