"""Caddy: which snippets the main Caddyfile loads, and checking them.

Caddy runs on the host and pulls the per-app snippets in with 'import'. A
snippet usually only makes sense in that context — inside a site block, using
named snippets defined in the main file — so a snippet is checked by adapting
a copy of the real configuration in which the import lines load that one
app's snippet and no other app's. A broken snippet in one app then cannot
hide behind, or be blamed on, another.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import confine

IMPORT_RE = re.compile(r"^(\s*)import\s+(\S+)(.*)$")
COMMENT_RE = re.compile(r"(^|\s)#.*$")
MAX_DEPTH = 8
GLOB_CHARS = "*?["


def _is_file(path: str, settings) -> bool:
    """With settings: a symlink out of an app does not count, so nothing here reads it."""
    try:
        return stat.S_ISREG(confine.stat_of(settings, path).st_mode)
    except OSError:
        return False


def resolve_pattern(token: str, base_dir: str) -> str:
    """An import token as Caddy resolves it: relative to the importing file."""
    token = token.strip('"`')
    return token if os.path.isabs(token) else os.path.join(base_dir, token)


def expand(pattern: str, base_dir: str, settings=None) -> List[str]:
    """Files an import token names, as real paths; [] for a named snippet.

    Like Caddy (Go's filepath.Glob), a directory the current user cannot
    read simply contributes no matches.
    """
    path = resolve_pattern(pattern, base_dir)
    matches = glob.glob(path) if any(ch in path for ch in GLOB_CHARS) else [path]
    return sorted({os.path.realpath(m) for m in matches if _is_file(m, settings)})


def is_path_import(token: str, base_dir: str) -> bool:
    """'import /x/*.caddy', 'import sites.caddy' — not 'import tls', a named snippet."""
    token = token.strip('"`')
    return (any(ch in token for ch in "/." + GLOB_CHARS)
            or os.path.exists(resolve_pattern(token, base_dir)))


@dataclass
class Import:
    file: str                  # the file with the import line
    line: int
    pattern: str               # absolute path or glob
    matches: List[str]         # real paths it matches right now


def scan_imports(caddyfile: str, settings=None) -> List[Import]:
    """Every path import reachable from the Caddyfile, in reading order.

    As root, pass settings: below the apps root, reading stays where it has to
    (see confine). The scan as Caddy's own user goes without: that user is what
    it is about, and it may only be allowed to pass through those directories.
    """
    found: List[Import] = []
    visited: Set[str] = set()

    def visit(path: str, depth: int) -> None:
        real = os.path.realpath(path)
        if real in visited:
            return
        visited.add(real)
        try:
            lines = confine.read_text(settings, path).splitlines()
        except OSError:
            return
        base = os.path.dirname(path)
        for number, line in enumerate(lines, 1):
            match = IMPORT_RE.match(line)
            if not match or not is_path_import(match.group(2), base):
                continue
            entry = Import(path, number, resolve_pattern(match.group(2), base),
                           expand(match.group(2), base, settings))
            found.append(entry)
            if depth < MAX_DEPTH:
                for target in entry.matches:
                    visit(target, depth + 1)

    visit(caddyfile, 0)
    return found


def imported_files(caddyfile: str, settings=None) -> Dict[str, Import]:
    """Every file the Caddyfile pulls in, directly or not: real path -> the import that does."""
    files: Dict[str, Import] = {}
    for entry in scan_imports(caddyfile, settings):
        for target in entry.matches:
            files.setdefault(target, entry)
    return files


def blocker(path: str, pattern: str) -> Optional[Tuple[str, str]]:
    """Where the *effective* user is stopped on the way to path via pattern.

    Returns (path, 'enter' | 'list' | 'read'), or None if nothing stops it.
    Every directory needs x to be passed; one that a wildcard lists needs r too.
    """
    effective = os.access in os.supports_effective_ids

    def can(target: str, mode: int) -> bool:
        return os.access(target, mode, effective_ids=effective)

    parts = pattern.split(os.sep)
    listed = {os.path.realpath(os.sep.join(parts[:i]) or os.sep)
              for i, part in enumerate(parts) if any(ch in part for ch in GLOB_CHARS)}
    steps = path.split(os.sep)
    for i in range(1, len(steps)):
        directory = os.sep.join(steps[:i]) or os.sep
        if not can(directory, os.X_OK):
            return directory, "enter"
        if directory in listed and not can(directory, os.R_OK):
            return directory, "list"
    if not can(path, os.R_OK):
        return path, "read"
    return None


class Isolator:
    """Writes a copy of the Caddy config that loads exactly one app snippet.

    Snippets are found where the configuration imports them: in place below
    the apps root, or installed into one directory (installed_dir) — then the
    import of that directory is swapped for the snippet's source, so it is
    checked as it will be once installed.
    """

    def __init__(self, caddyfile: str, app_snippets: Set[str], workdir: str,
                 installed_dir: Optional[str] = None, settings=None) -> None:
        self.caddyfile = caddyfile
        self.app_snippets = app_snippets
        self.workdir = workdir
        self.installed_dir = os.path.realpath(installed_dir) if installed_dir else None
        self.settings = settings
        self.originals: Dict[str, str] = {}   # temp path -> the file it stands for
        self._keep = ""
        self._found = False

    def build(self, keep: str) -> Optional[str]:
        """Path of the rewritten main file; None if nothing imports `keep`."""
        self._keep, self._found = keep, False
        path = self._rewrite(self.caddyfile, 0)
        return path if self._found else None

    def _rewrite(self, path: str, depth: int) -> str:
        try:
            lines = confine.read_text(self.settings, path).splitlines()
        except OSError:
            return path
        out: List[str] = []
        changed = False
        for line in lines:
            match = IMPORT_RE.match(line)
            if match and self.installed_dir and os.path.realpath(os.path.dirname(
                    resolve_pattern(match.group(2), os.path.dirname(path)))) == self.installed_dir:
                out.append(f"{match.group(1)}import {self._keep}{match.group(3)}")
                self._found = changed = True
                continue
            targets = expand(match.group(2), os.path.dirname(path), self.settings) if match else []
            if not targets:
                out.append(line)
                continue
            indent, _, rest = match.groups()
            replacement = []
            for target in targets:
                if target in self.app_snippets:
                    if target != self._keep:
                        continue
                    self._found = True
                elif depth < MAX_DEPTH:
                    target = self._rewrite(target, depth + 1)
                replacement.append(f"{indent}import {target}{rest}")
            changed = changed or replacement != [line]
            out.extend(replacement)
        if not changed:
            return path
        temp = os.path.join(self.workdir, f"{len(self.originals)}-{os.path.basename(path)}")
        with open(temp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        self.originals[temp] = path
        return temp

    def unmap(self, text: str) -> str:
        """Put the real file names back into a Caddy message."""
        for temp, original in self.originals.items():
            text = text.replace(temp, original)
        return text


@dataclass
class AdaptResult:
    ok: bool
    mode: str                                   # how the snippet was checked
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def parse_output(stderr: str) -> Tuple[List[str], List[dict]]:
    """Caddy's stderr: 'Error: ...' lines, and the JSON log records."""
    errors, records = [], []
    for line in stderr.splitlines():
        text = line.strip()
        if text.startswith("{"):
            try:
                records.append(json.loads(text))
                continue
            except ValueError:
                pass
        if text.startswith("Error:"):
            errors.append(text[len("Error:"):].strip())
    return errors, records


def adapt_snippet(host, caddy_bin: str, caddyfile: str, snippet: str,
                  app_snippets: Set[str], installed_dir: Optional[str] = None,
                  settings=None) -> AdaptResult:
    """Adapt the configuration with only this snippet among all app snippets."""
    workdir = tempfile.mkdtemp(prefix="redh-caddy-")
    try:
        isolator = Isolator(caddyfile, app_snippets, workdir, installed_dir, settings)
        config = isolator.build(snippet) if os.path.isfile(caddyfile) else None
        mode = f"in {caddyfile}"
        if config is None:
            config, mode = snippet, "on its own"
        result = host.run([caddy_bin, "adapt", "--adapter", "caddyfile",
                           "--config", config], timeout=60)
        errors, records = parse_output(result.err)
        errors = [isolator.unmap(e) for e in errors]
        if not result.ok and not errors:
            errors = [isolator.unmap(result.message())]
        warnings = []
        for record in records:
            if record.get("level") != "warn":
                continue
            where = record.get("file") or ""
            if where and os.path.realpath(where) == snippet:
                line = f":{record['line']}" if record.get("line") else ""
                warnings.append(f"{os.path.basename(snippet)}{line}: {record.get('msg', '')}")
        return AdaptResult(result.ok, mode, errors, warnings)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


NO_MATCH = "No files matching import glob pattern"

# What Caddy does with a path it finds in its configuration, by kind.
NEEDS = {"file": "r", "dir": "rx", "socket": "rw"}


def kind_of(path: str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return "missing"
    if stat.S_ISSOCK(st.st_mode):
        return "socket"
    return "dir" if stat.S_ISDIR(st.st_mode) else "file"


def referenced_paths(host, caddy_bin: str, caddyfile: str, root: str) -> Dict[str, str]:
    """Paths below root that Caddy's configuration makes it open: path -> kind.

    Read from Caddy's own adapted JSON, so every directive counts: certificate
    and key files, CA roots, file_server roots, unix sockets, log files.
    """
    result = host.run([caddy_bin, "adapt", "--adapter", "caddyfile", "--config", caddyfile],
                      timeout=60)
    try:
        config = json.loads(result.out) if result.ok else None
    except ValueError:
        config = None
    prefix = root.rstrip(os.sep) + os.sep
    found: Dict[str, str] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            # A reverse_proxy dial to a socket reads 'unix//path' (or 'unix+h2c//path').
            value = node[node.index("/") + 1:] if node.startswith("unix") and "/" in node else node
            if value.startswith(prefix) and "{" not in value:
                path = os.path.normpath(value)
                found[path] = kind_of(path)

    walk(config)
    return found


def validate(host, caddy_bin: str, caddyfile: str, user: Optional[str] = None):
    """'caddy validate' on the real configuration, as the user Caddy runs as.

    Returns (result, errors, log records). Caddy only *warns* about an import
    glob that matches nothing — and Go's glob skips directories the user cannot
    read — so those records matter as much as the errors.
    """
    argv = [caddy_bin, "validate", "--adapter", "caddyfile", "--config", caddyfile]
    result = host.run_as(user, argv, timeout=120) if user else host.run(argv, timeout=120)
    errors, records = parse_output(result.err)
    if not result.ok and not errors:
        errors = [result.message()]
    return result, errors, records


# -- upstreams --------------------------------------------------------------

def site_addresses(text: str) -> List[str]:
    """The addresses of a Caddyfile's site blocks: 'a.example, b.example {' at the top."""
    found: List[str] = []
    depth = 0
    for raw in text.splitlines():
        line = COMMENT_RE.sub("", raw).strip()
        if depth == 0 and line.endswith("{") and not line.startswith(("{", "(")):
            found.extend(word for word in re.split(r"[\s,]+", line[:-1]) if word)
        depth = max(depth + line.count("{") - line.count("}"), 0)
    return found


LOOPBACK_HOSTS = ("localhost", "::1", "", "0.0.0.0", "::")


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS or host.startswith("127.")


def parse_upstream(token: str) -> Optional[Tuple[str, int]]:
    """'http://127.0.0.1:8080' -> ('127.0.0.1', 8080); None without an explicit port."""
    for scheme in ("http://", "https://", "h2c://"):
        if token.startswith(scheme):
            token = token[len(scheme):]
    if token.startswith("unix/") or "{" in token:
        return None
    token = token.split("/", 1)[0]
    if token.startswith("["):
        host, _, rest = token[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif ":" in token:
        host, _, port = token.rpartition(":")
    else:
        return None
    if not port.isdigit():
        return None
    return host.lower(), int(port)


def proxy_targets(text: str) -> List[Tuple[int, str, int]]:
    """(line, host, port) for every reverse_proxy upstream with an explicit port."""
    targets: List[Tuple[int, str, int]] = []
    depth = 0
    block = 0          # brace depth inside a reverse_proxy block, 0 when outside

    def collect(number: int, words: Iterable[str]) -> None:
        for word in words:
            parsed = parse_upstream(word)
            if parsed:
                targets.append((number, *parsed))

    for number, raw in enumerate(text.splitlines(), 1):
        line = COMMENT_RE.sub("", raw)
        words = line.split()
        if words and block and words[0] == "to":
            collect(number, words[1:])
        if words and words[0] == "reverse_proxy":
            args = [w for w in words[1:] if w != "{"]
            if args and (args[0].startswith(("@", "/")) or args[0] == "*"):
                args = args[1:]                    # a matcher, not an upstream
            collect(number, args)
            if words[-1] == "{":
                block = depth + 1
        depth += line.count("{") - line.count("}")
        if block and depth < block:
            block = 0
    return targets


def parse_publish(spec: str) -> Optional[Tuple[str, int, int, str]]:
    """PublishPort= -> (bind address, first host port, last host port, the rest).

    None when podman picks the host port itself.
    """
    spec, _, proto = spec.strip().partition("/")
    address = ""
    if spec.startswith("["):
        address, _, spec = spec[1:].partition("]")
        parts = spec.lstrip(":").split(":")
        if len(parts) != 2:
            return None
        host_part, container = parts
    else:
        parts = spec.split(":")
        if len(parts) == 2:
            host_part, container = parts
        elif len(parts) == 3:
            address, host_part, container = parts
        else:
            return None
    low, _, high = host_part.partition("-")
    if not low.isdigit() or (high and not high.isdigit()):
        return None
    rest = container + (f"/{proto}" if proto else "")
    return address, int(low), int(high or low), rest


def all_interfaces(address: str) -> bool:
    return address in ("", "0.0.0.0", "::")
