"""Settings: an optional INI file plus defaults that match the usual layout."""

from __future__ import annotations

import configparser
import os
import pwd
import re
from dataclasses import dataclass
from typing import FrozenSet, Optional

from .common import die

DEFAULT_CONFIG = "/etc/redh-manager.conf"
COMPLETION_CACHE = "/var/cache/redh/apps.json"    # the apps' names, for tab completion
SECTION = "redh"

# An app is a directory name; nothing that could climb out of the apps root.
APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

KEYS = ("user", "root", "quadlet_dir", "quadlet_subdir", "caddy_subdir", "systemd_subdir",
        "user_unit_dir", "caddyfile", "caddy_dir", "caddy", "caddy_service", "settle", "ignore")


@dataclass
class Settings:
    user: str
    uid: int
    gid: int
    home: str
    root: str                      # where the apps live: <root>/<app>
    quadlet_dir: str               # the podman user's Quadlet directory
    quadlet_subdir: str = "quadlet"
    caddy_subdir: str = "caddy"
    systemd_subdir: str = "systemd"     # plain units: timers and the services they start
    user_unit_dir: str = ""             # where those go; default <home>/.config/systemd/user
    caddyfile: str = "/etc/caddy/Caddyfile"
    caddy_dir: str = "/etc/caddy/Caddyfile.d"   # where the apps' snippets get installed
    caddy: str = "caddy"
    caddy_service: str = "caddy.service"   # tells redh which user Caddy runs as
    settle: float = 5.0            # seconds to watch units after starting them
    ignore: FrozenSet[str] = frozenset()
    config_path: Optional[str] = None
    completion_cache: str = COMPLETION_CACHE

    def __post_init__(self) -> None:
        if not self.user_unit_dir:
            self.user_unit_dir = os.path.join(self.home, ".config", "systemd", "user")

    @property
    def runtime_dir(self) -> str:
        return f"/run/user/{self.uid}"

    def app_dir(self, app: str) -> str:
        return os.path.join(self.root, app)

    def install_dir(self, app: str) -> str:
        return os.path.join(self.quadlet_dir, app)


def load_settings(path: Optional[str] = None) -> Settings:
    explicit = path is not None
    path = path or os.environ.get("REDH_CONFIG") or DEFAULT_CONFIG

    parser = configparser.ConfigParser(interpolation=None,
                                       inline_comment_prefixes=("#", ";"))
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                parser.read_file(fh)
        except (OSError, configparser.Error) as exc:
            die(f"{path}: {exc}")
    elif explicit:
        die(f"config file not found: {path}")
    else:
        path = None

    extra = [s for s in parser.sections() if s != SECTION]
    if extra:
        die(f"{path}: unknown section(s) {', '.join(extra)}; "
            f"everything goes under [{SECTION}].")
    section = parser[SECTION] if parser.has_section(SECTION) else {}
    unknown = sorted(set(section) - set(KEYS))
    if unknown:
        die(f"{path}: unknown key(s) {', '.join(unknown)}; "
            f"known keys: {', '.join(KEYS)}.")

    user = section.get("user", "podman")
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        die(f"user {user!r} does not exist"
            + (f" (user= in {path})" if path else
               f"; set user= in {DEFAULT_CONFIG}"))
    home = entry.pw_dir.rstrip("/") or "/"

    try:
        settle = float(section.get("settle", "5"))
    except ValueError:
        die(f"{path}: settle= must be a number of seconds.")

    ignore = frozenset(re.split(r"[\s,]+", section.get("ignore", "").strip())) - {""}

    def absolute(key: str, default: str) -> str:
        value = os.path.expanduser(section.get(key, default)).rstrip("/") or "/"
        if not os.path.isabs(value):
            die(f"{path}: {key}= must be an absolute path, not {value!r}.")
        return value

    def plain(key: str, default: str) -> str:
        value = section.get(key, default).strip("/")
        if not value or "/" in value or value.startswith("."):
            die(f"{path}: {key}= must be a single directory name.")
        return value

    return Settings(
        user=user,
        uid=entry.pw_uid,
        gid=entry.pw_gid,
        home=home,
        root=absolute("root", home),
        quadlet_dir=absolute("quadlet_dir",
                             os.path.join(home, ".config/containers/systemd")),
        quadlet_subdir=plain("quadlet_subdir", "quadlet"),
        caddy_subdir=plain("caddy_subdir", "caddy"),
        systemd_subdir=plain("systemd_subdir", "systemd"),
        user_unit_dir=absolute("user_unit_dir", os.path.join(home, ".config/systemd/user")),
        caddyfile=absolute("caddyfile", "/etc/caddy/Caddyfile"),
        caddy_dir=absolute("caddy_dir", "/etc/caddy/Caddyfile.d"),
        caddy=section.get("caddy", "caddy"),
        caddy_service=section.get("caddy_service", "caddy.service"),
        settle=max(settle, 0.0),
        ignore=ignore,
        config_path=path,
    )
