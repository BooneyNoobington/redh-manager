"""A pretend host: enough of systemd --user, podman, Caddy and Quadlet for the tests.

FakeHost is the real Host with only the process boundary replaced, so the
command lines redh builds are exercised; FakeSystem answers them from state
kept in memory and from the files the test laid out in a temporary directory.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import stat
import tempfile
import textwrap
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Set, Tuple

from redh import caddy
from redh.app import Context, load_units, walk_files
from redh.config import Settings
from redh.host import Host, Result
from redh.report import Printer
from redh.units import PLAIN_UNIT_EXTS, PlainUnit, QuadletUnit, quadlet_ext

GENERATOR = "/fake/libexec/podman/quadlet"

PERM_BITS = {"o": {"r": stat.S_IROTH, "w": stat.S_IWOTH, "x": stat.S_IXOTH},
             "g": {"r": stat.S_IRGRP, "w": stat.S_IWGRP, "x": stat.S_IXGRP}}

# What Quadlet makes of each type: (Type=, RemainAfterExit=) of the generated service.
SERVICE_TYPES = {".container": ("notify", "no"), ".kube": ("notify", "no"), ".pod": ("forking", "no"),
                 ".network": ("oneshot", "yes"), ".image": ("oneshot", "yes"),
                 ".artifact": ("oneshot", "yes"), ".volume": ("oneshot", "no"),
                 ".build": ("oneshot", "no")}


class FakeSystem:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.calls: List[List[str]] = []
        self.units: Dict[str, Dict[str, str]] = {}     # service -> properties
        self.fail_start: Set[str] = set()
        self.crash_loop: Set[str] = set()
        self.stuck: Set[str] = set()
        self.generator_errors: Dict[str, str] = {}      # unit file name -> message
        self.objects: Set[Tuple[str, str]] = set()      # (kind, name) podman knows
        self.timer = ("enabled", "active")
        self.journal_lines = ["Oct 05 13:10:01 host podman[4242]: Error: something went wrong"]
        # name -> {"unit": ..., "image": ..., "sources": [...]}: what 'podman ps' shows
        self.containers: Dict[str, Dict] = {}
        self.caddy_user: Optional[str] = None          # caddy.service's User=, None: no such unit
        self.hidden_from_caddy: List[str] = []         # directories the caddy user cannot enter
        self.images: Dict[str, str] = {}               # local image store: name -> ID
        self.registry: Dict[str, str] = {}             # what a pull would fetch: name -> ID
        self.pull_errors: Dict[str, str] = {}
        self.builds = 0
        self.live: Dict[str, QuadletUnit] = {}
        # Who may open what: worked out from the 'other' bits below open_above (the
        # test's temporary directory; above it the test user's own dirs are closed).
        self.open_above: Optional[str] = None
        self.acl: Dict[str, Dict[str, str]] = {}       # path -> {user: some of 'rwx'}
        self.groups: Dict[int, Set[str]] = {}          # gid -> members
        self.chowned: List[str] = []
        self.reloads: List[str] = []
        self.caddy_reload_fails = False
        self.listening: Set[int] = set()               # TCP ports something listens on
        self.volumes: Dict[str, str] = {}              # named volume -> its contents
        self.rsync_rc = 0
        self.rsync_copies_to: Optional[str] = None     # a directory: rsync really delivers there
        self.tar_fails = False
        self.plain: Dict[str, PlainUnit] = {}          # the user unit directory, as loaded
        self.enabled: Set[str] = set()
        self.verify_errors: List[str] = []             # what systemd-analyze verify says

    @contextmanager
    def as_caddy(self) -> Iterator[None]:
        """Lock the test user out of hidden_from_caddy, as the caddy user would be."""
        modes = [(d, os.stat(d).st_mode) for d in self.hidden_from_caddy]
        for d, _ in modes:
            os.chmod(d, 0)
        try:
            yield
        finally:
            for d, mode in modes:
                os.chmod(d, mode)

    def run_container(self, name: str, unit: str = "", image: str = "docker.io/x/y:1",
                      sources: Optional[List[str]] = None) -> None:
        """A container started some other way than through redh."""
        self.containers[name] = {"unit": unit, "image": image, "sources": sources or [],
                                 "image_id": self.local_image(image)}

    def local_image(self, ref: str) -> str:
        return self.images.setdefault(ref, self.registry.get(ref) or f"sha256:{abs(hash(ref)):012x}")

    # -- dispatch ------------------------------------------------------------

    def run(self, argv: List[str], env: Dict[str, str], user: Optional[str] = None) -> Result:
        self.calls.append(argv)
        tool = os.path.basename(argv[0])
        if tool == "systemctl" and argv[1] != "--user":
            return self.system_show(argv[1:])
        if tool == "systemctl":
            return self.systemctl(argv[2:])
        if tool == "podman":
            return self.podman(argv[1:])
        if tool == "quadlet":
            return self.generator(env.get("QUADLET_UNIT_DIRS", ""))
        if tool == "caddy":
            if user and user == self.caddy_user:
                with self.as_caddy():
                    return self.caddy(argv[1:])
            return self.caddy(argv[1:])
        if tool == "chmod":
            return self.chmod(argv[1:])
        if tool == "chown":
            self.chowned.append(argv[-1])
            return Result(argv, 0, "", "")
        if tool == "setfacl":
            return self.setfacl(argv[1:])
        if tool == "systemd-analyze":
            return Result(argv, 1 if self.verify_errors else 0, "",
                          "".join(line + "\n" for line in self.verify_errors))
        if tool == "journalctl":
            return Result(argv, 0, "\n".join(self.journal_lines) + "\n", "")
        return Result(argv, 127, "", f"{tool}: command not found")

    # -- systemd -------------------------------------------------------------

    def generated(self) -> Dict[str, QuadletUnit]:
        """What the generator would produce from the Quadlet directory right now."""
        seen: Dict[str, QuadletUnit] = {}
        qdir = self.s.quadlet_dir
        if not os.path.isdir(qdir):
            return {}
        # Top level first, then subdirectories — like Quadlet.
        dirs = [qdir] + sorted(os.path.join(qdir, d) for d in os.listdir(qdir)
                               if os.path.isdir(os.path.join(qdir, d)) and not d.endswith(".d"))
        for directory in dirs:
            for name in sorted(os.listdir(directory)):
                path = os.path.join(directory, name)
                if quadlet_ext(name) and os.path.isfile(path) and name not in seen \
                        and name not in self.generator_errors:
                    seen[name] = QuadletUnit.load(path, [directory])
        return {u.service: u for u in seen.values()}

    def plain_units(self) -> Dict[str, PlainUnit]:
        """The unit files in the user unit directory, as the user manager reads them."""
        found: Dict[str, PlainUnit] = {}
        directory = self.s.user_unit_dir
        for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
            path = os.path.join(directory, name)
            if os.path.splitext(name)[1] in PLAIN_UNIT_EXTS and os.path.isfile(path):
                found[name] = PlainUnit.load(path)
        return found

    def daemon_reload(self) -> None:
        live = self.generated()
        plain = self.plain_units()
        for name in list(self.units):
            if name not in live and name not in plain:
                if self.units[name]["ActiveState"] == "inactive":
                    del self.units[name]
                else:
                    self.units[name]["LoadState"] = "not-found"
        for name, unit in live.items():
            state = self.units.setdefault(name, self.blank())
            kind, remain = SERVICE_TYPES[unit.ext]
            state.update(LoadState="loaded", SourcePath=unit.path, Type=kind, RemainAfterExit=remain)
        for name, unit in plain.items():
            state = self.units.setdefault(name, self.blank())
            kind = (unit.ini.get("Service", "Type") or "simple") if unit.ext == ".service" else ""
            remain = unit.ini.get("Service", "RemainAfterExit") or "no"
            state.update(LoadState="loaded", SourcePath=unit.path, Type=kind,
                         RemainAfterExit="yes" if remain in ("yes", "true", "1") else "no")
        self.live = live
        self.plain = plain

    @staticmethod
    def blank() -> Dict[str, str]:
        return {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
                "Result": "success", "NRestarts": "0", "ExecMainStatus": "0",
                "ActiveEnterTimestamp": "", "InactiveEnterTimestamp": "", "SourcePath": "",
                "Type": "simple", "RemainAfterExit": "no"}

    def state(self, name: str) -> Dict[str, str]:
        return self.units.get(name) or {**self.blank(), "LoadState": "not-found"}

    def systemctl(self, args: List[str]) -> Result:
        verb = args[0]
        names = args[args.index("--") + 1:] if "--" in args else args[1:]
        if verb == "daemon-reload":
            self.daemon_reload()
            return Result(args, 0, "", "")
        if verb == "show":
            props = args[1].split("=", 1)[1].split(",")
            blocks = []
            for name in names:
                state = {"Id": name, **self.state(name)}
                if name == "podman-auto-update.timer":
                    state["NextElapseUSecRealtime"] = "Tue 2026-10-06 00:00:00 CEST"
                if name in self.plain:
                    state["UnitFileState"] = ("enabled" if name in self.enabled else
                                              "disabled" if self.plain[name].starts else "static")
                blocks.append("\n".join(f"{p}={state.get(p, '')}" for p in props))
            return Result(args, 0, "\n\n".join(blocks) + "\n", "")
        if verb in ("start", "restart"):
            if verb == "restart":
                self.stop(names)
            ok = all([self.start(name) for name in names])
            err = "" if ok else "Job for x.service failed because the control process exited."
            return Result(args, 0 if ok else 1, "", err)
        if verb == "stop":
            self.stop(names)
            return Result(args, 0, "", "")
        if verb == "reset-failed":
            for name in names:
                state = self.units.get(name)
                if state and state["ActiveState"] == "failed":
                    state.update(ActiveState="inactive", SubState="dead", Result="success")
            return Result(args, 0, "", "")
        if verb == "is-system-running":
            return Result(args, 0, "running\n", "")
        if verb in ("enable", "disable", "reenable"):
            unknown = [n for n in names if n not in self.plain]
            if unknown and verb != "disable":
                return Result(args, 1, "", f"Failed to {verb} unit: Unit file {unknown[0]} does not exist.")
            for name in names:
                if verb == "disable":
                    self.enabled.discard(name)
                elif self.plain[name].starts:
                    self.enabled.add(name)
            return Result(args, 0, "", "")
        if verb == "is-enabled":
            return Result(args, 0 if self.timer[0] == "enabled" else 1, self.timer[0] + "\n", "")
        if verb == "is-active":
            return Result(args, 0 if self.timer[1] == "active" else 3, self.timer[1] + "\n", "")
        if verb == "list-units":
            return Result(args, 0, "", "")
        return Result(args, 1, "", f"unknown verb {verb}")

    def image_of(self, unit: QuadletUnit) -> str:
        """The image name a container unit runs, through .image and .build units."""
        image = unit.get("Image") or ""
        other = next((u for u in self.live.values() if u.filename == image), None)
        if other is not None:
            return other.get("ImageTag" if other.ext == ".build" else "Image") or image
        return image

    def start(self, name: str) -> bool:
        state, unit = self.units[name], self.live.get(name)
        if state["ActiveState"] == "active":
            return True                                 # nothing to do, like systemd
        if name in self.fail_start:
            state.update(ActiveState="failed", SubState="failed", Result="exit-code",
                         ExecMainStatus="125")
            return False
        if name in self.crash_loop:
            state.update(ActiveState="activating", SubState="auto-restart",
                         NRestarts=str(int(state["NRestarts"]) + 3))
            return True
        if unit is not None and unit.ext == ".container":
            # Requires= on a .build task: starting the container runs the build too.
            needed = next((u for u in self.live.values() if u.filename == unit.get("Image")), None)
            if needed is not None and needed.ext == ".build":
                self.start(needed.service)
        if state.get("Type") == "oneshot":
            if unit is not None and unit.ext == ".build" and unit.get("ImageTag"):
                self.builds += 1
                self.images[unit.get("ImageTag")] = f"sha256:{self.builds:012d}"
            if state.get("RemainAfterExit") == "no":
                state.update(ActiveState="inactive", SubState="dead", Result="success",
                             InactiveEnterTimestamp="Mon 2026-10-05 13:10:00 CEST")
            else:
                state.update(ActiveState="active", SubState="exited", Result="success",
                             ActiveEnterTimestamp="Mon 2026-10-05 13:10:01 CEST")
            return True
        timer = name.endswith(".timer")
        state.update(ActiveState="active", SubState="waiting" if timer else "running", Result="success",
                     ActiveEnterTimestamp="Mon 2026-10-05 13:10:01 CEST")
        if timer:
            state["NextElapseUSecRealtime"] = "Tue 2026-10-06 00:23:00 CEST"
        if unit is not None and unit.ext == ".container":
            image = self.image_of(unit)
            self.containers[unit.resource_name] = {
                "unit": name, "image": image, "image_id": self.local_image(image),
                "sources": [s for _, s in unit.bind_sources() if s.startswith("/")]}
        return True

    def stop(self, names: List[str]) -> None:
        for name in names:
            state = self.units.get(name)
            if state and name not in self.stuck and state["ActiveState"] != "failed":
                if state["ActiveState"] == "active":
                    state.update(ActiveState="inactive", SubState="dead",
                                 InactiveEnterTimestamp="Mon 2026-10-05 14:00:00 CEST")
                for cname in [c for c, info in self.containers.items() if info["unit"] == name]:
                    del self.containers[cname]

    def start_all(self) -> None:
        self.daemon_reload()
        self.systemctl(["start", "--", *self.units])

    # -- podman --------------------------------------------------------------

    def system_show(self, args: List[str]) -> Result:
        """systemctl for the system manager — only caddy.service exists."""
        if args[0] == "reload":
            self.reloads.append(args[1])
            return Result(args, 1 if self.caddy_reload_fails else 0, "",
                          "Job for caddy.service failed." if self.caddy_reload_fails else "")
        if args[0] == "show" and args[1] == "caddy.service" and self.caddy_user:
            return Result(args, 0, f"LoadState=loaded\nUser={self.caddy_user}\nSupplementaryGroups=\n", "")
        return Result(args, 0, "LoadState=not-found\nUser=\nSupplementaryGroups=\n", "")

    def podman(self, args: List[str]) -> Result:
        if args == ["--version"]:
            return Result(args, 0, "podman version 5.4.2\n", "")
        if args[0] == "ps":
            return Result(args, 0, "".join(f"{name}\n" for name in self.containers), "")
        if args[:2] == ["image", "inspect"]:
            ref = args[-1]
            if ref not in self.images:
                return Result(args, 125, "", f"Error: {ref}: image not known")
            return Result(args, 0, self.images[ref] + "\n", "")
        if args[0] == "pull":
            ref = args[-1]
            if ref in self.pull_errors:
                return Result(args, 125, "", f"Error: initializing source docker://{ref}: "
                                             f"{self.pull_errors[ref]}")
            self.images[ref] = self.registry.get(ref) or self.local_image(ref)
            return Result(args, 0, self.images[ref] + "\n", "")
        if args[0] == "auto-update":
            lines = []
            for name, info in self.containers.items():
                unit = self.live.get(info["unit"])
                policy = (unit.get_all("AutoUpdate") or [""])[-1] if unit is not None else ""
                if policy:
                    remote = self.registry.get(info["image"], info["image_id"])
                    pending = "pending" if remote != info["image_id"] else "false"
                    lines.append(f"{info['unit']}\t{info['image']}\t{policy}\t{pending}")
            return Result(args, 0, "\n".join(lines) + "\n", "")
        if args[:2] == ["container", "inspect"]:
            records = [{"Id": name, "Name": name, "ImageName": info["image"], "IsInfra": False,
                        "Image": info.get("image_id", ""),
                        "State": {"Status": "running"},
                        "Config": {"Labels": {"PODMAN_SYSTEMD_UNIT": info["unit"]} if info["unit"] else {}},
                        "Mounts": [{"Type": "bind", "Source": s, "Destination": "/data"}
                                   for s in info["sources"]]}
                       for name, info in self.containers.items() if name in args[2:]]
            return Result(args, 0, json.dumps(records), "")
        if args[0] == "inspect":
            return Result(args, 0, "healthy\n", "")
        if args[:2] == ["volume", "export"]:
            with open(args[3], "w") as fh:
                fh.write(self.volumes.get(args[4], ""))
            return Result(args, 0, "", "")
        if args[:2] == ["volume", "create"]:
            self.objects.add(("volume", args[2]))
            return Result(args, 0, args[2] + "\n", "")
        if args[:2] == ["volume", "import"]:
            with open(args[3]) as fh:
                self.volumes[args[2]] = fh.read()
            return Result(args, 0, "", "")
        if args[:2] == ["image", "exists"]:
            known = args[2] in self.images or ("image", args[2]) in self.objects
            return Result(args, 0 if known else 1, "", "")
        if len(args) == 3 and args[1] == "exists":
            return Result(args, 0 if (args[0], args[2]) in self.objects else 1, "", "")
        if args[0] == "unshare":
            shutil.rmtree(args[-1])
            return Result(args, 0, "", "")
        kind = {"rm": "container", "pod": "pod", "volume": "volume", "network": "network"}[args[0]]
        name = args[-1]
        if (kind, name) not in self.objects:
            return Result(args, 1, "", f"Error: no such {kind} {name}")
        self.objects.discard((kind, name))
        return Result(args, 0, name + "\n", "")

    # -- permissions, POSIX ACLs ------------------------------------------------

    def chmod(self, args: List[str]) -> Result:
        spec, path = args
        bits = stat.S_IMODE(os.stat(path).st_mode)
        if spec.isdigit():
            bits = int(spec, 8)
        else:                                          # 'o-rwx', 'go-rwx'
            who, perms = spec.split("-")
            for w in who:
                for p in perms:
                    bits &= ~PERM_BITS[w][p]
        os.chmod(path, bits)                           # the group bits are the ACL mask
        return Result(args, 0, "", "")

    def setfacl(self, args: List[str]) -> Result:
        if args[0] == "-x":
            self.acl.get(args[2], {}).pop(args[1].split(":")[1], None)
            return Result(args, 0, "", "")
        assert args[0] == "-m", args
        _, user, perms = args[1].split(":")
        perms = perms.replace("-", "")
        self.acl.setdefault(args[2], {})[user] = perms
        # setfacl widens the mask so that it covers every named entry.
        bits = stat.S_IMODE(os.stat(args[2]).st_mode)
        for p in perms:
            bits |= PERM_BITS["g"][p]
        os.chmod(args[2], bits)
        return Result(args, 0, "", "")

    def allows(self, user: str, path: str, letter: str) -> bool:
        """POSIX ACL rules for a user who neither owns the file nor is in its group."""
        bits = os.stat(path).st_mode
        entry = self.acl.get(path, {}).get(user)
        if entry is not None:
            return letter in entry and bool(bits & PERM_BITS["g"][letter])   # masked
        return bool(bits & PERM_BITS["o"][letter])

    # -- the Quadlet generator -------------------------------------------------

    def generator(self, unit_dirs: str) -> Result:
        out, err, seen = [], [], set()
        for directory in unit_dirs.split(":"):
            files = walk_files(directory)
            units, _ = load_units(directory, files)
            for unit in units:
                if unit.filename in seen:
                    continue
                seen.add(unit.filename)
                if unit.filename in self.generator_errors:
                    err.append(f'quadlet-generator[7]: converting "{unit.filename}": '
                               f'{self.generator_errors[unit.filename]}')
                else:
                    out.append(f"---{unit.service}---\n[Unit]\nSourcePath={unit.path}\n")
                err.append(f"quadlet-generator[7]: Loading source unit file {unit.path}")
        return Result([], 1 if self.generator_errors else 0, "\n".join(out), "\n".join(err))

    # -- Caddy ---------------------------------------------------------------

    def caddy(self, args: List[str]) -> Result:
        config = args[args.index("--config") + 1]
        files = [os.path.realpath(config)] + list(caddy.imported_files(config))
        logs = [json.dumps({"level": "warn", "msg": caddy.NO_MATCH, "pattern": e.pattern})
                for e in caddy.scan_imports(config) if not e.matches and "*" in e.pattern]
        for path in files:
            with open(path) as fh:
                for number, line in enumerate(fh, 1):
                    if "BROKEN" in line:
                        return Result(args, 1, "", "\n".join(logs + [
                            f"Error: adapting config using caddyfile: "
                            f"{path}:{number}: unrecognized directive: BROKEN"]))
        # Like the adapted JSON: every path a directive names ends up somewhere in it.
        strings = []
        for path in files:
            with open(path) as fh:
                for line in fh:
                    words = line.split()
                    if words and words[0] != "import" and not words[0].startswith("#"):
                        strings += [w for w in words if w.startswith(("/", "unix"))]
        return Result(args, 0, json.dumps({"apps": {"http": {"strings": strings}}}), "\n".join(logs))


class FakeHost(Host):
    def __init__(self, settings: Settings, system: FakeSystem) -> None:
        super().__init__(settings)
        self.system = system

    def _exec(self, argv, extra_env, timeout, cwd) -> Result:
        return self.system.run(list(argv), dict(extra_env))

    def accessible(self, user, paths, mode, extra_groups=()):
        system = self.system
        if system.open_above is None or user not in ("nobody", system.caddy_user):
            return None
        letter = {os.R_OK: "r", os.W_OK: "w", os.X_OK: "x"}[mode]

        def reaches(path):
            if not system.allows(user, path, letter):
                return False
            directory = os.path.dirname(path)
            while directory.startswith(system.open_above + os.sep):
                if not system.allows(user, directory, "x"):
                    return False
                directory = os.path.dirname(directory)
            return True
        return {p for p in paths if reaches(p)}

    def listening_ports(self):
        return set(self.system.listening)

    def _stream(self, argv, extra_env, cwd, stdin, stdout) -> Result:
        """tar for real, on real files; only 'podman unshare' is left out."""
        self.system.calls.append(argv)
        assert argv[:3] == ["podman", "unshare", "tar"], argv
        if self.system.tar_fails:
            return Result(argv, 2, "", "tar: data/x: Cannot open: Permission denied\n")
        proc = subprocess.run(argv[2:], stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                              text=True, cwd=cwd,
                              env=dict(os.environ, COPYFILE_DISABLE="1"))   # macOS: no ._files
        return Result(argv, proc.returncode, "", proc.stderr)

    def _attached(self, argv) -> int:
        self.system.calls.append(argv)
        if self.system.rsync_rc == 0 and self.system.rsync_copies_to:
            shutil.copy(argv[-2], self.system.rsync_copies_to)
        return self.system.rsync_rc

    def acl_entries(self, path):
        return {user: perms or "---" for user, perms in self.system.acl.get(path, {}).items()}

    def group_members(self, gid):
        return set(self.system.groups.get(gid, set()))

    def subordinate_uids(self, path="/etc/subuid"):
        return []

    def run_as(self, user, argv, timeout=None) -> Result:
        return self.system.run(list(argv), {}, user=user)

    @contextmanager
    def impersonate(self, user, extra_groups=()):
        if user != self.system.caddy_user:
            yield False
            return
        with self.system.as_caddy():
            yield True

    def generator(self) -> Optional[str]:
        return GENERATOR

    def linger(self) -> bool:
        return True


class Lab:
    """A temporary /opt/podman, /etc/caddy and a FakeHost wired together."""

    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="redh-test-")
        home = os.path.join(self.tmp, "opt", "podman")
        os.makedirs(home)
        os.chmod(home, 0o700)                          # a home directory, as useradd makes it
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        caddy_bin = os.path.join(bin_dir, "caddy")
        with open(caddy_bin, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(caddy_bin, 0o755)
        self.settings = Settings(
            user="podman", uid=os.getuid(), gid=os.getgid(), home=home, root=home,
            quadlet_dir=os.path.join(home, ".config", "containers", "systemd"),
            caddyfile=os.path.join(self.tmp, "etc", "caddy", "Caddyfile"),
            caddy_dir=os.path.join(self.tmp, "etc", "caddy", "Caddyfile.d"),
            caddy=caddy_bin, settle=0,
            completion_cache=os.path.join(self.tmp, "var", "cache", "redh", "apps.json"))
        self.system = FakeSystem(self.settings)
        self.system.open_above = self.tmp
        self.host = FakeHost(self.settings, self.system)
        self.stream = io.StringIO()
        self.out = Printer(colour=False, verbose=True, stream=self.stream)
        self.ctx = Context(self.settings, self.host, self.out)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str, mode: int = 0o644) -> str:
        """Write a file relative to the podman home (or absolute)."""
        path = rel if os.path.isabs(rel) else os.path.join(self.settings.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text).lstrip())
        os.chmod(path, mode)
        return path

    def output(self) -> str:
        text = self.stream.getvalue()
        self.stream.seek(0)
        self.stream.truncate()
        return text

    def fresh(self) -> Context:
        """A context with empty caches, as the next redh run would see it."""
        self.ctx = Context(self.settings, self.host, self.out)
        return self.ctx


def nextcloud(lab: Lab, data_dir: bool = True, in_place: bool = False) -> None:
    """A tidy app: quadlet/ and caddy/, everything as it should be.

    The main Caddyfile imports the installed snippets — or, with in_place, the
    snippet straight from /opt/podman, the way it was done before.
    """
    if data_dir:
        os.makedirs(os.path.join(lab.settings.home, "nextcloud", "data"), exist_ok=True)
    data = os.path.join(lab.settings.home, "nextcloud", "data")
    lab.write("nextcloud/quadlet/nextcloud.container", f"""
        [Unit]
        Description=Nextcloud

        [Container]
        Image=docker.io/library/nextcloud:30
        AutoUpdate=registry
        Network=nextcloud.network
        Volume={data}:/var/www/html:Z
        Volume=nextcloud-db.volume:/db
        PublishPort=127.0.0.1:8080:80
        HealthCmd=curl -f http://localhost/

        [Install]
        WantedBy=default.target
        """)
    lab.write("nextcloud/quadlet/nextcloud.network", """
        [Network]
        """)
    lab.write("nextcloud/quadlet/nextcloud-db.volume", """
        [Volume]
        """)
    lab.write("nextcloud/caddy/nextcloud.caddy", """
        cloud.example.org {
            reverse_proxy 127.0.0.1:8080
        }
        """)
    imports = (f"{lab.settings.root}/*/caddy/nextcloud.caddy" if in_place
               else f"{lab.settings.caddy_dir}/*.caddy")
    lab.write(lab.settings.caddyfile, f"""
        {{
            email admin@example.org
        }}
        import {imports}
        """)


def strip_ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text)
