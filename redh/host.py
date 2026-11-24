"""Running commands: as root, as the podman user, and against its user manager.

redh normally runs as root (like the borg backups). Everything that has to
happen as the podman user — systemctl --user, podman, the Quadlet generator —
goes through runuser with that user's XDG_RUNTIME_DIR, so it reaches the same
user manager that lingering started. Run as the podman user itself, the same
commands simply run directly.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .common import HostError
from .config import Settings

# The generator binary moved around between podman versions and distros.
GENERATOR_CANDIDATES = (
    "/usr/libexec/podman/quadlet",
    "/usr/lib/podman/quadlet",
    "/usr/lib/systemd/user-generators/podman-user-generator",
    "/usr/lib/systemd/system-generators/podman-system-generator",
)

# Properties redh reads for a unit's state.
STATE_PROPS = ("LoadState", "ActiveState", "SubState", "Result", "NRestarts",
               "ExecMainStatus", "ActiveEnterTimestamp", "InactiveEnterTimestamp",
               "Type", "RemainAfterExit")


def is_task(state: Mapping[str, str]) -> bool:
    """A oneshot that runs to completion and is inactive again afterwards.

    Quadlet makes .build and .volume units like that (no RemainAfterExit=):
    'inactive (dead)' after a run is their success, not a failure.
    """
    return state.get("Type") == "oneshot" and state.get("RemainAfterExit") == "no"


def finished(state: Mapping[str, str]) -> bool:
    """A task that has run, and succeeded."""
    return (is_task(state) and state.get("ActiveState") == "inactive"
            and state.get("Result", "success") == "success"
            and bool(state.get("InactiveEnterTimestamp")))


@dataclass
class Result:
    argv: List[str]
    rc: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def message(self) -> str:
        """The most telling line of output, for a one-line error report."""
        for text in (self.err, self.out):
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            if lines:
                return lines[-1]
        return f"exit status {self.rc}"


class Host:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.euid = os.geteuid()
        self._generator: Optional[str] = None
        self._generator_searched = False

    @property
    def is_root(self) -> bool:
        return self.euid == 0

    @property
    def is_user(self) -> bool:
        return self.euid == self.s.uid

    @property
    def manager_hint(self) -> str:
        """How a person reaches the same user manager from a root shell."""
        return f"systemctl --user -M {self.s.user}@"

    # -- running -------------------------------------------------------------

    def user_env(self) -> Dict[str, str]:
        runtime = self.s.runtime_dir
        return {"XDG_RUNTIME_DIR": runtime,
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}

    def run(self, argv: Sequence[str], *, as_user: bool = False,
            env: Optional[Mapping[str, str]] = None,
            timeout: Optional[float] = None) -> Result:
        argv = list(argv)
        extra = dict(env or {})
        cwd = None
        if as_user:
            argv, extra, cwd = self._as_user(argv, extra)
        return self._exec(argv, extra, timeout, cwd)

    def _as_user(self, argv: List[str], env: Mapping[str, str]) -> Tuple[List[str], Dict[str, str], str]:
        extra = {**self.user_env(), **env}
        if not self.is_user:
            argv = (["runuser", "-u", self.s.user, "--", "env", f"HOME={self.s.home}"]
                    + [f"{k}={v}" for k, v in extra.items()] + argv)
            extra = {}
        return argv, extra, self.s.home

    def stream(self, argv: Sequence[str], *, stdin=None, stdout=None) -> Result:
        """Run a command as the podman user with a file on its stdin or stdout: an
        archive, which is too big to go through memory."""
        command, extra, cwd = self._as_user(list(argv), {})
        return self._stream(command, extra, cwd, stdin, stdout)

    def attached(self, user: str, argv: Sequence[str],
                 env: Optional[Mapping[str, str]] = None) -> int:
        """Run a command as a person, on this terminal: ssh may have something to ask."""
        command = list(argv)
        if env:
            command = ["env", *[f"{k}={v}" for k, v in env.items()], *command]
        if self.is_root and user != "root":
            command = ["runuser", "-u", user, "--", *command]
        return self._attached(command)

    def run_as(self, user: str, argv: Sequence[str], timeout: Optional[float] = None) -> Result:
        """Run a command as some other system user (only root can; else as ourselves)."""
        argv = list(argv)
        if self.is_root and user != "root":
            # runuser keeps root's environment; root's XDG directories would send
            # the program (Caddy: its storage) somewhere it cannot write.
            unset = [arg for var in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                                     "XDG_RUNTIME_DIR") for arg in ("-u", var)]
            argv = ["runuser", "-u", user, "--", "env", *unset] + argv
        return self._exec(argv, {}, timeout, "/")

    def service_identity(self, service: str) -> Optional[Tuple[str, List[str]]]:
        """(user, extra groups) a system service runs as; None if there is no such service."""
        result = self.run(["systemctl", "show", service,
                           "--property=LoadState,User,SupplementaryGroups"], timeout=30)
        props = dict(line.split("=", 1) for line in result.out.splitlines() if "=" in line)
        if props.get("LoadState") != "loaded":
            return None
        return props.get("User") or "root", props.get("SupplementaryGroups", "").split()

    @contextmanager
    def impersonate(self, user: str, extra_groups: Sequence[str] = ()) -> Iterator[bool]:
        """Take on user's identity for file access inside this process.

        Yields whether it did: only root can, and only for a user that exists.
        Used to look at files the way a service sees them — the kernel decides,
        so ACLs and group memberships count exactly as they do for the service.
        """
        identity = None
        if self.is_root and user != "root":
            try:
                entry = pwd.getpwnam(user)
                groups = set(os.getgrouplist(user, entry.pw_gid))
                for name in extra_groups:
                    try:
                        groups.add(grp.getgrnam(name).gr_gid)
                    except KeyError:
                        pass
                identity = (entry.pw_uid, entry.pw_gid, sorted(groups))
            except KeyError:
                pass
        if identity is None:
            yield False
            return
        saved_groups, saved_gid = os.getgroups(), os.getegid()
        os.setgroups(identity[2])
        os.setegid(identity[1])
        os.seteuid(identity[0])
        try:
            yield True
        finally:
            os.seteuid(0)
            os.setegid(saved_gid)
            os.setgroups(saved_groups)

    def accessible(self, user: str, paths: Sequence[str], mode: int,
                   extra_groups: Sequence[str] = ()) -> Optional[Set[str]]:
        """The paths user may open with mode (os.R_OK, os.W_OK), as the kernel decides.

        None when that cannot be tested: only root can take on another identity.
        """
        with self.impersonate(user, extra_groups) as switched:
            if not switched:
                return None
            effective = os.access in os.supports_effective_ids
            return {p for p in paths if os.access(p, mode, effective_ids=effective)}

    def group_members(self, gid: int) -> Set[str]:
        """Everyone in a group: listed members, and users whose primary group it is."""
        try:
            members = set(grp.getgrgid(gid).gr_mem)
        except KeyError:
            return set()
        return members | {entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid}

    def subordinate_uids(self, path: str = "/etc/subuid") -> List[Tuple[int, int]]:
        """(first, count) ranges the podman user's containers map their users to."""
        ranges = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.strip().split(":")
                    if len(parts) == 3 and parts[0] in (self.s.user, str(self.s.uid)) \
                            and parts[1].isdigit() and parts[2].isdigit():
                        ranges.append((int(parts[1]), int(parts[2])))
        except OSError:
            pass
        return ranges

    def _exec(self, argv: List[str], extra_env: Mapping[str, str],
              timeout: Optional[float], cwd: Optional[str]) -> Result:
        environ = dict(os.environ, **extra_env)
        if cwd and not os.path.isdir(cwd):
            cwd = None
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  errors="replace", env=environ, cwd=cwd,
                                  timeout=timeout, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return Result(argv, 127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(argv, 124, "", f"{argv[0]}: timed out after {timeout:.0f}s")
        return Result(argv, proc.returncode, proc.stdout, proc.stderr)

    def _stream(self, argv: List[str], extra_env: Mapping[str, str], cwd: Optional[str],
                stdin, stdout) -> Result:
        try:
            proc = subprocess.run(argv, env=dict(os.environ, **extra_env), cwd=cwd,
                                  stdin=subprocess.DEVNULL if stdin is None else stdin,
                                  stdout=subprocess.PIPE if stdout is None else stdout,
                                  stderr=subprocess.PIPE, text=True, errors="replace")
        except FileNotFoundError:
            return Result(argv, 127, "", f"{argv[0]}: command not found")
        return Result(argv, proc.returncode, proc.stdout or "", proc.stderr)

    def _attached(self, argv: List[str]) -> int:
        try:
            return subprocess.run(argv).returncode
        except FileNotFoundError:
            return 127

    # -- systemd -------------------------------------------------------------

    def systemctl(self, *args: str, timeout: Optional[float] = None) -> Result:
        return self.run(["systemctl", "--user", *args], as_user=True, timeout=timeout)

    def show(self, units: Sequence[str],
             props: Sequence[str] = STATE_PROPS) -> Dict[str, Dict[str, str]]:
        """systemctl show for several units at once, keyed by the name asked for."""
        units = list(dict.fromkeys(units))
        if not units:
            return {}
        result = self.systemctl("show", "--property=" + ",".join(("Id", *props)),
                                "--", *units, timeout=60)
        if not result.ok and not result.out.strip():
            raise HostError(f"cannot reach {self.s.user}'s user manager: "
                            f"{result.message()}")
        blocks = [dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                  for block in result.out.strip("\n").split("\n\n")]
        if len(blocks) == len(units):
            # One block per unit, in order — also right for aliases, whose Id differs.
            return dict(zip(units, blocks))
        # systemctl skipped a name it rejected outright; match by Id instead.
        by_id = {block.get("Id"): block for block in blocks}
        return {unit: by_id.get(unit, {"LoadState": "unknown"}) for unit in units}

    def daemon_reload(self) -> Result:
        return self.systemctl("daemon-reload", timeout=120)

    def journal(self, unit: str, since: float, lines: int = 8) -> List[str]:
        """The newest journal lines of a user unit since a point in time."""
        argv = ["journalctl", "--no-pager", "--quiet", "--output=short",
                f"--lines={lines}", f"--since=@{int(since)}"]
        if self.is_root and not self.is_user:
            # 'journalctl --user-unit' would match root's own UID; spell it out.
            uid = self.s.uid
            result = self.run(argv + [f"_UID={uid}", f"_SYSTEMD_USER_UNIT={unit}", "+",
                                      f"_UID={uid}", f"USER_UNIT={unit}"], timeout=30)
        else:
            result = self.run(argv + ["--user", f"--unit={unit}"], as_user=True,
                              timeout=30)
        return [line for line in result.out.splitlines() if line.strip()]

    # -- podman and the generator ---------------------------------------------

    def podman(self, *args: str, timeout: Optional[float] = 120) -> Result:
        return self.run(["podman", *args], as_user=True, timeout=timeout)

    def podman_version(self) -> Optional[tuple]:
        result = self.podman("--version", timeout=30)
        match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", result.out)
        if not result.ok or not match:
            return None
        return tuple(int(part or 0) for part in match.groups())

    def generator(self) -> Optional[str]:
        if not self._generator_searched:
            self._generator_searched = True
            self._generator = next((path for path in GENERATOR_CANDIDATES
                                    if os.access(path, os.X_OK)), None)
        return self._generator

    def generator_dryrun(self, unit_dirs: Sequence[str]) -> Result:
        """Run the Quadlet generator over exactly these directories, writing nothing."""
        generator = self.generator()
        if generator is None:
            return Result([], 127, "", "Quadlet generator not found")
        return self.run([generator, "-user", "-dryrun"], as_user=True,
                        env={"QUADLET_UNIT_DIRS": ":".join(unit_dirs)}, timeout=120)

    # -- the user ------------------------------------------------------------

    def linger(self) -> bool:
        return os.path.exists(f"/var/lib/systemd/linger/{self.s.user}")

    def chown(self, path: str) -> None:
        """Hand a file redh created to the podman user (only root has to)."""
        if self.is_root and not self.is_user:
            os.lchown(path, self.s.uid, self.s.gid)

    def chown_fd(self, fd: int) -> None:
        """The same for a file redh holds open."""
        if self.is_root and not self.is_user:
            os.fchown(fd, self.s.uid, self.s.gid)

    def chown_to(self, path: str, uid: int, gid: int) -> None:
        """Give a file to someone else (only root can)."""
        if self.is_root:
            os.lchown(path, uid, gid)

    def listening_ports(self) -> Set[int]:
        """TCP ports something on this host listens on (rootless podman's forwarders too)."""
        ports: Set[int] = set()
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(table, encoding="ascii") as fh:
                    next(fh, None)
                    for line in fh:
                        fields = line.split()
                        if len(fields) > 3 and fields[3] == "0A":       # LISTEN
                            ports.add(int(fields[1].rpartition(":")[2], 16))
            except (OSError, ValueError):
                continue
        return ports

    def acl_entries(self, path: str) -> Dict[str, str]:
        """Named user entries of a path's ACL: user -> 'r-x' and the like."""
        result = self.run(["getfacl", "--absolute-names", "--omit-header", path], timeout=30)
        entries = {}
        for line in result.out.splitlines():
            parts = line.split("#")[0].strip().split(":")
            if len(parts) == 3 and parts[0] == "user" and parts[1]:
                entries[parts[1]] = parts[2]
        return entries
