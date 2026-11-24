"""What installing an app would change, worked out without changing anything.

Besides the app's own install directory this looks at the rest of the Quadlet
directory. Quadlet loads each file *name* only once — the first one it finds,
and files at the top level are found before any subdirectory — so a copy of
one of the app's files elsewhere either shadows the installed one (an old
hand-made install: removed by 'redh install') or belongs to another app (a
conflict: one of the two has to be renamed).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from . import confine
from .app import Installed, Sources, unit_relpaths
from .units import is_global_dropin_dir, quadlet_ext


def same_content(settings, a: str, b: str) -> bool:
    try:
        return confine.read_bytes(settings, a) == confine.read_bytes(settings, b)
    except OSError:
        return False


def dest_mode(source_mode: int) -> int:
    """Installed copies keep the source's permissions, and the owner can always read them."""
    return (stat.S_IMODE(source_mode) & 0o777) | 0o600


def installed_matches(host, source: str, dest: str) -> bool:
    try:
        src_st, dest_st = confine.stat_of(host.s, source), os.lstat(dest)
    except OSError:
        return False
    if not stat.S_ISREG(dest_st.st_mode):
        return False
    if stat.S_IMODE(dest_st.st_mode) != dest_mode(src_st.st_mode):
        return False
    if host.is_root and not host.is_user and dest_st.st_uid != host.s.uid:
        return False
    return same_content(host.s, source, dest)


@dataclass
class Plan:
    app: str
    src: Sources
    inst: Installed
    add: List[str] = field(default_factory=list)
    change: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    same: List[str] = field(default_factory=list)
    legacy: List[Tuple[str, Optional[str], bool]] = field(default_factory=list)  # old copy, its source, identical?
    conflicts: List[Tuple[str, str]] = field(default_factory=list)              # path, owning app

    @property
    def in_sync(self) -> bool:
        return not (self.add or self.change or self.remove or self.legacy)

    def lines(self) -> List[str]:
        lines = [f"+ {rel}" for rel in self.add]
        lines += [f"~ {rel}" for rel in self.change]
        lines += [f"- {rel}" for rel in self.remove]
        lines += [f"x {path}  (old copy{'' if same else ', differs from the source'})"
                  for path, _, same in self.legacy]
        return lines

    def touched(self) -> Tuple[Set[str], bool]:
        """Unit files whose definition changes, and whether a change reaches every unit.

        Drop-ins shared by several units and the files units read (environment
        files, kube YAML) count as touching everything.
        """
        names: Set[str] = set()
        everything = False
        changed = self.add + self.change + self.remove
        changed += [os.path.basename(path) for path, _, _ in self.legacy]
        for rel in changed:
            parts = rel.split(os.sep)
            if len(parts) > 1 and parts[-2].endswith(".d"):
                target = parts[-2][:-2]
                if is_global_dropin_dir(parts[-2]) or os.path.splitext(target)[0].endswith("-"):
                    everything = True
                else:
                    names.add(target)
            elif quadlet_ext(parts[-1]):
                names.add(parts[-1])
            else:
                everything = True
        return names, everything


def copies_elsewhere(ctx, app: str, files: Dict[str, str]):
    """Files with the same name as one of the app's, outside its install directory.

    Returns (legacy, conflicts): legacy entries are (path, matching source,
    identical?) and sit at the top level or in a drop-in directory there;
    conflicts are (path, app) inside another app's install directory.
    """
    units = {os.path.basename(rel): files[rel] for rel in unit_relpaths(files)}
    dropins: Dict[str, str] = {}
    for rel, path in files.items():
        parts = rel.split(os.sep)
        if len(parts) >= 2 and parts[-2].endswith(".d") and not is_global_dropin_dir(parts[-2]):
            dropins[os.path.join(parts[-2], parts[-1])] = path

    legacy: List[Tuple[str, Optional[str], bool]] = []
    conflicts: List[Tuple[str, str]] = []
    for rel, path in ctx.tree().items():
        parts = rel.split(os.sep)
        if len(parts) > 1 and parts[0] == app:
            continue
        if len(parts) > 1 and parts[-2].endswith(".d"):
            source = dropins.get(os.path.join(parts[-2], parts[-1]))
        else:
            source = units.get(parts[-1])
        if source is None:
            continue
        owner = parts[0] if len(parts) > 1 and not parts[0].endswith(".d") else None
        if owner:
            conflicts.append((path, owner))
        else:
            legacy.append((path, source, same_content(ctx.settings, source, path)))
    return legacy, conflicts


def make_plan(ctx, app: str) -> Plan:
    src, inst = ctx.sources(app), ctx.installed(app)
    plan = Plan(app, src, inst)
    for rel, source in src.files.items():
        dest = inst.files.get(rel)
        if dest is None:
            plan.add.append(rel)
        elif installed_matches(ctx.host, source, dest):
            plan.same.append(rel)
        else:
            plan.change.append(rel)
    plan.remove = sorted(set(inst.files) - set(src.files))
    plan.legacy, plan.conflicts = copies_elsewhere(ctx, app, src.files or inst.files)
    return plan
