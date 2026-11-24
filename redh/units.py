"""Reading Quadlet unit files.

Only as much of systemd's syntax as redh needs: sections, repeated keys, line
continuations, comments and drop-in merging. The naming rules follow Quadlet's
own generator (pkg/systemd/quadlet in the podman sources). Nothing here talks
to systemd; host.py does that.
"""

from __future__ import annotations

import os
import shlex
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# extension -> (section, suffix of the generated service, key naming the podman object)
QUADLET_TYPES: Dict[str, Tuple[str, str, Optional[str]]] = {
    ".container": ("Container", "", "ContainerName"),
    ".pod": ("Pod", "-pod", "PodName"),
    ".kube": ("Kube", "", None),
    ".network": ("Network", "-network", "NetworkName"),
    ".volume": ("Volume", "-volume", "VolumeName"),
    ".image": ("Image", "-image", None),
    ".build": ("Build", "-build", None),
    ".artifact": ("Artifact", "-artifact", None),
}

# Display order: what actually runs first, the plumbing after it.
KIND_ORDER = ("pod", "container", "kube", "network", "volume", "image",
              "build", "artifact")

# Ordinary systemd units. Quadlet ignores them in its directory.
PLAIN_UNIT_EXTS = (".service", ".timer", ".socket", ".target", ".path",
                   ".mount", ".slice")

# [Install] keys that make another unit pull this one in.
INSTALL_KEYS = ("WantedBy", "RequiredBy", "UpheldBy")

# Targets that exist for the system manager but not for a user manager.
SYSTEM_ONLY_TARGETS = ("multi-user.target", "graphical.target")


def quadlet_ext(name: str) -> Optional[str]:
    ext = os.path.splitext(name)[1]
    return ext if ext in QUADLET_TYPES else None


def is_dropin_dir(name: str) -> bool:
    """foo.container.d, foo-.container.d or the global container.d."""
    if not name.endswith(".d"):
        return False
    inner = name[:-2]
    return quadlet_ext(inner) is not None or "." + inner in QUADLET_TYPES


def is_global_dropin_dir(name: str) -> bool:
    """container.d and friends apply to every unit of that type, in every app."""
    return name.endswith(".d") and "." + name[:-2] in QUADLET_TYPES


def split_words(value: str) -> List[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return value.split()


class IniFile:
    """systemd-style key/value file: ordered, repeatable keys per section."""

    def __init__(self) -> None:
        self.sections: Dict[str, List[Tuple[str, str]]] = {}

    @classmethod
    def parse(cls, text: str) -> "IniFile":
        ini = cls()
        ini.update(text)
        return ini

    def update(self, text: str) -> None:
        """Merge more text in, the way a drop-in is merged."""
        section: Optional[str] = None
        pending: Optional[str] = None
        for raw in text.splitlines() + [""]:
            line = raw.strip()
            if pending is not None:
                if line.startswith(("#", ";")):
                    continue        # comments inside a continuation are dropped
                if line.endswith("\\"):
                    pending += " " + line[:-1].strip()
                    continue
                line, pending = (pending + " " + line).strip(), None
            elif line.endswith("\\"):
                pending = line[:-1].strip()
                continue
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                self.sections.setdefault(section, [])
            elif section is not None and "=" in line:
                key, value = line.split("=", 1)
                self.sections[section].append((key.strip(), value.strip()))

    def has(self, section: str) -> bool:
        return section in self.sections

    def get(self, section: str, key: str) -> Optional[str]:
        """Last value of key, surrounding double quotes removed; None if unset."""
        values = [v for k, v in self.sections.get(section, ()) if k == key]
        if not values:
            return None
        value = values[-1]
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        return value

    def get_all(self, section: str, key: str) -> List[str]:
        """Every value of key in order; an empty assignment resets the list."""
        result: List[str] = []
        for k, v in self.sections.get(section, ()):
            if k == key:
                if v == "":
                    result = []
                else:
                    result.append(v)
        return result

    def get_words(self, section: str, key: str) -> List[str]:
        words: List[str] = []
        for value in self.get_all(section, key):
            words.extend(split_words(value))
        return words


def dropin_dirs(filename: str) -> List[str]:
    """Drop-in directory names Quadlet consults for a unit, least specific first."""
    stem, ext = os.path.splitext(filename)
    names = [ext[1:] + ".d"]
    base = stem.split("@", 1)[0]
    parts = base.split("-")
    for i in range(1, len(parts)):
        names.append("-".join(parts[:i]) + "-" + ext + ".d")
    if "@" in stem and not stem.endswith("@"):
        names.append(base + "@" + ext + ".d")
    names.append(filename + ".d")
    return names


def read_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


class QuadletUnit:
    """One .container/.network/... file, merged with its drop-ins."""

    def __init__(self, filename: str, path: str, ini: IniFile,
                 dropins: Sequence[str] = ()) -> None:
        self.filename = filename
        self.path = path
        self.ini = ini
        self.dropins = list(dropins)
        self.ext = os.path.splitext(filename)[1]
        self.kind = self.ext[1:]
        self.stem = filename[: -len(self.ext)]
        self.section = QUADLET_TYPES[self.ext][0]

    @classmethod
    def load(cls, path: str, dropin_search: Sequence[str] = (),
             read: Callable[[str], str] = read_file) -> "QuadletUnit":
        """Read path and merge the drop-ins found below any of dropin_search.

        Whatever root reads from the podman user's directories needs
        read=confine.reader(settings).
        """
        filename = os.path.basename(path)
        ini = IniFile.parse(read(path))
        found: Dict[str, str] = {}
        for name in dropin_dirs(filename):          # more specific ones win
            for base in dropin_search:
                directory = os.path.join(base, name)
                try:
                    entries = sorted(os.listdir(directory))
                except OSError:
                    continue
                for entry in entries:
                    full = os.path.join(directory, entry)
                    if entry.endswith(".conf") and os.path.isfile(full):
                        found[entry] = full
        dropins = [found[name] for name in sorted(found)]
        for dropin in dropins:
            ini.update(read(dropin))
        return cls(filename, path, ini, dropins)

    def __repr__(self) -> str:
        return f"QuadletUnit({self.path!r})"

    # -- names ---------------------------------------------------------------

    @property
    def is_template(self) -> bool:
        return self.stem.endswith("@")

    @property
    def service_base(self) -> str:
        name = self.get("ServiceName")
        if name:
            return name
        suffix = QUADLET_TYPES[self.ext][1]
        if self.is_template:
            return self.stem[:-1] + suffix + "@"
        return self.stem + suffix

    @property
    def service(self) -> str:
        """The unit the generator writes: foo.service, foo-network.service, foo@.service."""
        return self.service_base + ".service"

    @property
    def startable(self) -> Optional[str]:
        """The service to start and stop; None for a template without DefaultInstance=."""
        if not self.is_template:
            return self.service
        instance = self.ini.get("Install", "DefaultInstance")
        return f"{self.service_base}{instance}.service" if instance else None

    @property
    def resource_name(self) -> Optional[str]:
        """Name of the podman container/pod/network/volume; None if it needs systemd specifiers."""
        key = QUADLET_TYPES[self.ext][2]
        if key is None or "@" in self.stem:
            return None
        name = self.get(key)
        if not name:
            name = "systemd-%N" if self.ext == ".container" else "systemd-" + self.stem
        name = name.replace("%N", self.service_base)
        return None if "%" in name else name

    # -- values --------------------------------------------------------------

    def get(self, key: str, section: Optional[str] = None) -> Optional[str]:
        return self.ini.get(section or self.section, key)

    def get_all(self, key: str, section: Optional[str] = None) -> List[str]:
        return self.ini.get_all(section or self.section, key)

    def _own(self, key: str, *exts: str) -> List[str]:
        return self.get_all(key) if self.ext in exts else []

    def publish_ports(self) -> List[str]:
        return self._own("PublishPort", ".container", ".pod", ".kube")

    def volume_specs(self) -> List[str]:
        return self._own("Volume", ".container", ".pod", ".build")

    def mount_specs(self) -> List[str]:
        return self._own("Mount", ".container")

    def network_specs(self) -> List[str]:
        return self._own("Network", ".container", ".pod", ".kube", ".build")

    def env_files(self) -> List[str]:
        if self.ext != ".container":
            return []
        return self.ini.get_words(self.section, "EnvironmentFile")

    def install_targets(self) -> List[str]:
        targets: List[str] = []
        for key in INSTALL_KEYS:
            targets.extend(self.ini.get_words("Install", key))
        return targets

    def uses_host_network(self) -> bool:
        return any(spec.split(":", 1)[0] == "host" for spec in self.network_specs())

    def bind_sources(self) -> List[Tuple[str, str]]:
        """(key=value as written, source) for every Volume=/Mount= that names a host path."""
        found = []
        for spec in self.volume_specs():
            source = spec.split(":", 1)[0]
            if source.startswith((".", "/")):
                found.append((f"Volume={spec}", source))
        for spec in self.mount_specs():
            fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
            source = fields.get("source") or fields.get("src") or ""
            if fields.get("type", "volume") == "bind" and source:
                found.append((f"Mount={spec}", source))
        return found

    def references(self) -> List[Tuple[str, str]]:
        """Other Quadlet files this one names, as (key, filename).

        Quadlet turns each into Requires= and After= on the other unit's
        service, so stopping that service stops this one too.
        """
        refs: List[Tuple[str, str]] = []

        def add(key: str, value: Optional[str]) -> None:
            if value and quadlet_ext(value):
                refs.append((key, value))

        for spec in self.network_specs():
            add("Network", spec.split(":", 1)[0])
        for spec in self.volume_specs():
            add("Volume", spec.split(":", 1)[0])
        for spec in self.mount_specs():
            for part in spec.split(","):
                key, _, value = part.partition("=")
                if key.strip() in ("source", "src"):
                    add("Mount", value.strip())
        if self.ext == ".container":
            add("Pod", self.get("Pod"))
        if self.ext in (".container", ".volume"):
            add("Image", self.get("Image"))
        return refs

    def requires(self) -> List[str]:
        """Units named in [Unit] whose stop propagates to this one."""
        names: List[str] = []
        for key in ("Requires", "BindsTo", "PartOf", "Requisite"):
            names.extend(self.ini.get_words("Unit", key))
        return names


def sort_key(unit: QuadletUnit) -> Tuple[int, str]:
    return KIND_ORDER.index(unit.kind), unit.filename


# -- plain units -----------------------------------------------------------------

# What may precede an Exec*= command: ignore failure, argv[0], no variables, privileges.
EXEC_PREFIXES = "-@:+!"
EXEC_KEYS = ("ExecStartPre", "ExecStart", "ExecStartPost", "ExecReload", "ExecStop", "ExecStopPost")


class PlainUnit:
    """A plain systemd unit of the podman user: a timer, the service it starts, and the like."""

    def __init__(self, name: str, path: str, ini: IniFile) -> None:
        self.name = name
        self.path = path
        self.ini = ini
        self.ext = os.path.splitext(name)[1]

    @classmethod
    def load(cls, path: str, read: Callable[[str], str] = read_file) -> "PlainUnit":
        return cls(os.path.basename(path), path, IniFile.parse(read(path)))

    def __repr__(self) -> str:
        return f"PlainUnit({self.path!r})"

    @property
    def starts(self) -> bool:
        """It has an [Install] section, so it is enabled, and started with the app.
        Without one it only runs when something else starts it: a timer's service."""
        return any(self.ini.get_words("Install", key) for key in INSTALL_KEYS)

    def install_targets(self) -> List[str]:
        return [t for key in INSTALL_KEYS for t in self.ini.get_words("Install", key)]

    def commands(self) -> List[str]:
        """The programs its Exec*= lines run, as written (with %h and the like)."""
        found = []
        for key in EXEC_KEYS:
            for line in self.ini.get_all("Service", key):
                words = line.split()
                if words:
                    found.append(words[0].lstrip(EXEC_PREFIXES) or (words[1] if len(words) > 1 else ""))
        return [c for c in found if c]
