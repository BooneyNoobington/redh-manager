# redh-manager

Management frontend for rootless Podman containers that run as Quadlet units
under the unprivileged `podman` user, one app per directory in `/opt/podman`.

```
redh init APP [--image …]           a new app directory, with a skeleton to fill in
redh list [--json]                  one line per app, and what podman runs
redh check [APP ...]                is everything where and how it should be?
redh status [APP ...]               what systemd says about each unit
redh install APP ... | --all        (re)install the Quadlet files, reload, verify
redh start|stop|restart APP ... | --all
redh update APP ... | --all [-n]    by hand, what podman auto-update does
redh uninstall APP ...              stop and remove the installed units; the data stays
redh prune APP                      stop, uninstall, remove networks and volumes,
                                    delete /opt/podman/APP
redh export APP [DESTINATION]       pack the app into one archive, send it with rsync
redh import [APP|FILE]              unpack such an archive to /opt/podman/APP
```

Run it as root, like the borg backups. Everything that has to happen as the
podman user (`systemctl --user`, `podman`, the Quadlet generator) goes through
`runuser` with that user's `XDG_RUNTIME_DIR`, so it talks to the same user
manager that lingering started.

## Install

Python 3.9 or newer; standard library only, but for tab completion. Podman 4.7 or newer: that is the
first release in which Quadlet looks into subdirectories, and redh installs every
app into its own one.

redh is a Python package with a `redh` command. pipx installs it system-wide:
into a virtual environment of its own under `/opt/pipx`, root's, with the
command linked into `/usr/local/bin` — where `sudo redh` finds it.

```sh
sudo apt install pipx
git clone https://github.com/BooneyNoobington/redh-manager.git ~/src/redh-manager
sudo pipx install --global ~/src/redh-manager
sudo redh check
```

To update, pull and build again from the same checkout, new version number or
not:

```sh
git -C ~/src/redh-manager pull
sudo pipx upgrade --global redh-manager
```

* The code that runs as root is the copy in `/opt/pipx/venvs/redh-manager`,
  which only root can change. The checkout is only the source; it can stay
  yours, and installing leaves nothing behind in it.
* Installing fetches two things from PyPI: the build backend (`flit_core`)
  and argcomplete, for tab completion. Everything else is Python's standard
  library.
* `sudo pipx list --global` shows what is installed,
  `sudo pipx uninstall --global redh-manager` removes it.
* After a Python upgrade (a Debian release upgrade) the virtual environment
  needs rebuilding: `sudo pipx reinstall-all --global`.
* Keep the checkout outside `/opt/podman`, or redh takes it for an app.

### Tab completion

TAB completes commands, options and app names — also after `sudo`. Once, in
`~/.zshrc` after `compinit` (with oh-my-zsh: after it is sourced), or in
`~/.bashrc`:

```sh
eval "$(redh completion zsh)"       # or: bash, fish
```

`start`, `stop`, `restart` and `update` offer the installed apps, `install`
the apps with sources, everything else any app. Completion runs as you, not
as root, and `/opt/podman` is closed to you; so every time redh runs as root
it writes the apps' names to `/var/cache/redh/apps.json`, which completion
reads. Only names, readable by everyone — `ps` shows the containers' names
anyway. Until the first `sudo redh …`, TAB knows no apps. The completion
comes from argcomplete, which pipx installs along with redh.

In the checkout, `python3 -m redh` runs the code as it is, without installing.
As root, add `-B` (`sudo python3 -B -m redh check`), or root leaves its
`__pycache__` behind in the checkout.

## Layout

```
/opt/podman/                         the podman user's home, and the apps root: 0700
├── .config/containers/systemd/      the Quadlet directory: redh installs into it
│   └── nextcloud/                   one subdirectory per installed app
├── .config/systemd/user/            the plain units, from every app's systemd/
└── nextcloud/
    ├── quadlet/                     units, drop-ins, env files: copied on install
    │   ├── nextcloud.container
    │   ├── nextcloud-db.container
    │   └── nextcloud.network
    ├── systemd/                     plain units: a timer, the service it starts
    │   ├── nextcloud-cron.timer
    │   └── nextcloud-cron.service
    ├── caddy/
    │   └── Caddyfile                copied on install to /etc/caddy/Caddyfile.d/nextcloud.caddy
    └── data/                        bind mounts and the like: never copied

/etc/caddy/
├── Caddyfile                        has: import /etc/caddy/Caddyfile.d/*.caddy
└── Caddyfile.d/
    └── nextcloud.caddy              root's, readable by Caddy's group only
```

Apps that still keep their unit files at the top level of `/opt/podman/<app>`
work as they are; then only the unit files and their `*.d/` drop-in
directories are installed. `redh check` points them out and prints the `mv`
that tidies them up.

### Plain units: systemd/

Some jobs are no container: renewing a certificate Caddy cannot get, cleaning
up, a backup. They are a timer and the service it starts, plain units of the
podman user's systemd, and they go in `systemd/`:

```
$ sudo redh install dms
install dms
  + /opt/podman/.config/systemd/user/dms-cert-renew.service
  + /opt/podman/.config/systemd/user/dms-cert-renew.timer
  ok    4 files written
  ok    daemon-reload
  ok    systemd knows all 4 units
  ok    enabled dms-cert-renew.timer
```

* `install` copies them into `/opt/podman/.config/systemd/user/`, the podman
  user's (`0644`), and enables what has an `[Install]` section: the timer. That
  directory has no subdirectory per app, so every copy begins with a comment
  naming its source, as the Caddy snippets do. A file there without one is not
  redh's, and install leaves it alone; one of the same name stops the install.
* A name Quadlet makes is refused: a plain `dms.service` would take the place
  of the container `dms.container` makes, because systemd ranks that directory
  above Quadlet's output.
* `start` starts what has an `[Install]` section — the timer, not the service
  it starts, which would renew right away. `stop` stops both, so `stop --all`
  before a backup also keeps timers quiet. `status` shows the timer's next run.
* A unit taken out of `systemd/` is stopped, disabled and deleted on the next
  install; `uninstall` and `prune` do that to all of them.
* `check` lets systemd judge the units (`systemd-analyze --user verify`, as
  the podman user, so `%h` is its home and `ExecStart=` has to be its to run),
  and reports copies that are out of date or not enabled, and a last run that
  failed, with its journal.
* An app may consist of nothing but `systemd/`.

### Caddy snippets

Caddy runs on the host. `redh install` copies an app's `caddy/Caddyfile` to
`/etc/caddy/Caddyfile.d/<app>.caddy`, the way it copies the units, and the
main Caddyfile imports them all with one line:

```
import /etc/caddy/Caddyfile.d/*.caddy
```

Caddy then reads its configuration from `/etc/caddy` only, and `/opt/podman`
stays the podman user's alone. Each installed file begins with a comment
naming its source; that is how redh tells its files from your own in that
directory. Other files in `caddy/` become `<app>.<name>.caddy`.

On install, Caddy validates the whole configuration as its own user. If it
rejects the new snippet, the old one goes back in place; if not, Caddy reloads.
`uninstall` and `prune` take the snippet away the same way. `redh check` says
when a snippet is not installed or out of date, and when the main Caddyfile
does not import the directory.

Caddy used to import the snippets straight from `/opt/podman`, and
`redh check` still understands that and warns about it. Moving over:

1. `redh install APP` for each app with a snippet (the `CADDY` column of
   `redh list`). The snippets land in `/etc/caddy/Caddyfile.d`; Caddy does
   not load them yet.
2. In `/etc/caddy/Caddyfile`, replace the imports from `/opt/podman` with
   `import /etc/caddy/Caddyfile.d/*.caddy`, then `systemctl reload caddy`.
3. `redh check --fix` closes `/opt/podman` again: Caddy needs no way in any
   more, and the ACL entries that gave it one are removed. If the check still
   names something Caddy opens below `/opt/podman`, move that out first.

## init

`redh init` starts a new app with a skeleton that already follows the layout,
the permission policy and the checks:

```
$ sudo redh init vaultwarden --image docker.io/vaultwarden/server:latest --port 80 --data /data --env
init vaultwarden
  + /opt/podman/vaultwarden/
  + quadlet/
  + quadlet/vaultwarden.container
  + quadlet/vaultwarden.env
  + caddy/
  + caddy/Caddyfile
  + data/
  info  host port 8001 on 127.0.0.1: the first from 8000 up that is free
  info  site vaultwarden.internal, like 7 other sites (--domain chooses another)
  ok    created: podman's alone, directories 0700, files 0600
  info  then: redh check vaultwarden && redh install vaultwarden && redh start vaultwarden
```

* `quadlet/APP.container` runs the image with `AutoUpdate=registry`,
  publishes the port on 127.0.0.1 only, restarts on failure and starts at
  boot. `--data PATH` creates `data/` and mounts it at PATH, `--env` creates
  `quadlet/APP.env` for secrets; without them, both are commented examples.
* `caddy/Caddyfile` is the site, proxying to that port. Its address is APP
  under the domain most of Caddy's sites use, unless `--domain`.
  `--no-caddy` leaves it out, for apps without a web interface.
* The host port is the first one from 8000 up that no app publishes and
  nothing listens on; `--port 8081:80` names it.
* What init cannot know — the image without `--image`, the container's port
  without `--port`, the site's address when there is no other site to go by —
  is on a line marked `TODO(redh init)`. `redh check` lists those as a FAIL,
  and `redh install` refuses until they are filled in and the TODO comments
  are gone.
* Everything is the podman user's alone: directories 0700, files 0600. As
  root, redh creates it all through a descriptor of the new directory, so a
  symlink planted under `/opt/podman` cannot send a file elsewhere.
* An empty directory of that name is filled in. Anything else stops init:
  an existing app, an install left over from a deleted one, another app's
  unit of the same name, a host port that is taken.

## check

`redh check` without arguments checks the host and every app. FAIL means it
does not or will not work; warn means it works but should be fixed. Only FAIL
makes the exit status non-zero. `-v` also lists what is fine. `redh check --fix`
first applies the permission policy and makes env files private (see below),
then checks.

| Topic       | What is checked |
|-------------|-----------------|
| `user`      | linger is on; the user manager is reachable, and not degraded |
| `podman`    | podman runs as the user and is 4.7 or newer; the Quadlet generator is there |
| `update`    | every container sets `AutoUpdate=`; `registry` has a fully-qualified image that is not pinned by digest; locally built images use `local`; `podman-auto-update.timer` is on, and its last run succeeded (else its journal) |
| `layout`    | `quadlet/`, `systemd/` and `caddy/` are used; nothing is lying outside them; no plain units in `quadlet/`, where Quadlet would ignore them; no `SystemD/` that is meant to be `systemd/` |
| `init`      | no line marked `TODO(redh init)` is left in the unit files and snippets |
| `units`     | `systemd-analyze --user verify` accepts the plain units; the type section and `Image=` are there; bind mount sources exist and are absolute; `EnvironmentFile=`s exist and get installed; every `Network=`/`Volume=`/`Pod=`/`Image=` reference to another Quadlet file resolves, in this app or another |
| `generator` | Quadlet's own generator, in dry-run mode, over the app's files laid out as they will be after installing — the same parser systemd runs at boot |
| `image`     | a container whose `Image=localhost/…` was built by hand gets a warning: nothing builds it again once it is gone. If it is gone, FAIL — the next start fails (see below) |
| `boot`      | `[Install] WantedBy=default.target`; `multi-user.target` does not exist for a user manager |
| `caddy`     | the snippet exists, and Caddy loads it from `/etc/caddy/Caddyfile.d`; it adapts *in the real Caddyfile with only this app's snippet loaded*, so a broken snippet is blamed on its own app; every `reverse_proxy` to a local port hits a port the app publishes; a proxied port is not also published on every interface |
| `perms`     | the app's directory, its `caddy/` and what lies in them follow the permission policy — see below |
| `secrets`   | env files and other secrets are only readable by their owner — see below |
| `install`   | the installed units, plain units and Caddy snippet match the source; no old copy elsewhere shadows them; no other app has a file of the same name; no plain unit has a name Quadlet makes |
| `runtime`   | the units are loaded and active; the timers are enabled; no plain unit failed its last run |
| `caddy` (host) | `caddy validate`, run as Caddy's user, passes on the whole configuration; the main Caddyfile imports the snippet directory; no import glob matches nothing; no installed snippet lost its source |
| `apps` (host)  | no unit file, service name or host port is used by two apps; installs and plain units whose app directory is gone; unit files that belong to no app |
| `perms` (host) | `/opt/podman` and everything else in it follow the permission policy |
| `secrets` (host) | env files at the top of `/opt/podman` and of the Quadlet directory, and those the loose unit files there use |

### Permissions

No exceptions: only the podman user gets anywhere below `/opt/podman`.

| Path | Owner, mode |
|------|-------------|
| `/opt/podman` | `podman`, `0700` |
| `/opt/podman/<app>` | `podman`, `0700` |

Caddy reads its snippets from `/etc/caddy`, so it needs no way in either.
Whatever its configuration still points to below `/opt/podman` is a FAIL,
whether that is a snippet imported in place, a `root` directory for
`file_server`, a certificate or a CA root. redh finds those by running
`caddy adapt` on the real Caddyfile and taking every path below
`/opt/podman` from the result. The fix is to move them out. A CA root belongs
in the system trust store.

```
FAIL  perms     /opt/podman (drwxr-xr-x podman:podman): every user can list it; every user can enter it
                chmod 700 /opt/podman
FAIL  perms     /opt/podman (drwx------ podman:podman): an ACL entry still grants Caddy (caddy) x
                chmod 700 /opt/podman
                setfacl -x u:caddy /opt/podman
FAIL  perms     Caddy serves /opt/podman/step-ca/html, but nothing below /opt/podman is Caddy's
                move it out of /opt/podman
```

The rules are tested, not read off the permission bits. As root, redh takes on
the identities of `nobody` and of Caddy's user and asks the kernel, so ACLs
and every directory above count exactly as they do for them.

`redh check --fix` runs exactly the commands the FAILs show, then checks
again. It only ever takes access away: `chown`, `chmod 700`, and
`setfacl -x u:caddy` for an entry that still grants Caddy something. It holds
back, and says why, where taking access away would break something:

* a directory something Caddy's configuration opens still lies below. Move
  that out first; closing it now would take the site down.
* everything, if `caddy` cannot be found: redh cannot ask Caddy what its
  configuration opens.
* a directory a container mounts. A container's non-root user may get in
  through exactly those bits. The list of mounts comes from every unit file
  redh knows and from podman, for what actually runs.

Data directories are left alone: the closed app directory is the wall.

### Env files and other secrets

Env files hold passwords in plain text, and so do password files, so `check`
looks at who besides their owner gets at them:

* every file in the app's directory (three levels deep, not into
  `node_modules`, `.git` or virtualenvs) and in its install directory whose name
  has, as a part of its own, `env` or a word for a secret: `pw`, `password`,
  `passwd`, `secret`, `token`, `key`, `privkey`, `credentials`. So `.env`,
  `db.env`, `.ca.pw`, `api-key` and `mail.key` count, `Rentenversicherung.pdf`
  and `keyboard.conf` do not;
* every file a unit reads with `EnvironmentFile=`, whatever its name and
  wherever it is.

Templates (`.env.example`, `.env.sample`, …), code (`env.js`, `env.py`, …),
public keys (`.pub`), documents and pictures are left out, whatever their name
says.

```
FAIL  secrets   .env (-rw-r--r-- podman:podman): every user of this host can read it
                chmod go-rwx /opt/podman/vaultwarden/.env
FAIL  secrets   quadlet/app.env (-rw-r--r-- podman:podman): it is open to everyone; only /opt/podman (drwx------ podman:podman) keeps them out
                chmod go-rwx /opt/podman/app/quadlet/app.env
                chmod go-rwx /opt/podman/.config/containers/systemd/app/app.env
```

A secret should belong to its owner alone, so each of these is a FAIL:

* every user of the host, or the user Caddy runs as, can get at it — tested
  as `nobody` and `caddy`, like the permissions above;
* it is open, and only a closed directory above it keeps others out: safe by
  luck, one changed directory away from the first;
* its group has members other than its owner and the podman user;
* it belongs to someone other than root, the podman user and its containers'
  subordinate UIDs (from `/etc/subuid`).

The installed copy of a file in `quadlet/` is fixed along with it.

Careful with files inside a directory a container bind-mounts. The app in the
container may read them as a non-root user, and that user is one of the
subordinate UIDs on the host. It may only get in through the very bits
`chmod go-rwx` takes away. `--fix` leaves those alone and says why: give the
file to the container's user first (`podman unshare chown UID:GID FILE`), then
take the bits away.

The podman user writes the unit files and owns the directories, so neither
gets to steer what `--fix` does as root. An env file that is a symlink out of
its app is a FAIL of its own and is not followed, unless a container mounts the
directory. A file a unit names outside `/opt/podman` is reported, not changed.

## install

`redh install nextcloud` mirrors `/opt/podman/nextcloud/quadlet/` into
`/opt/podman/.config/containers/systemd/nextcloud/`:

```
install nextcloud
  /opt/podman/nextcloud/quadlet/ → /opt/podman/.config/containers/systemd/nextcloud/
  ~ nextcloud.container
  + nextcloud-redis.container
  x /opt/podman/.config/containers/systemd/nextcloud.container  (old copy)
  ok    generator Quadlet accepts all 4: nextcloud.service, nextcloud-redis.service, …
  ok    2 files written, 1 removed
  ok    daemon-reload
  ok    systemd knows all 4 units
  info  running on the old definition: nextcloud.service → redh restart nextcloud
```

* The unit checks and the generator dry run come first. A FAIL stops the
  install, unless `--force`.
* Files are written atomically and owned by `podman`, with the source's
  permissions. Files that disappeared from the source are removed.
* Root writes into directories the podman user owns, so it does that only
  through descriptors of directories it reached without leaving the podman
  user's home, and from the Quadlet directory down every directory has to be a
  real one. An install directory, a drop-in directory or the Quadlet directory
  turned into a symlink makes the install fail instead of writing elsewhere; a
  symlink left where a file goes is replaced, not written through. The same
  holds for the plain units and for `uninstall`, which removes a link itself,
  never what it points to.
* Quadlet reads each file *name* only once, and the top level of its directory
  comes before any subdirectory. So an old hand-installed copy of one of the
  app's files there would shadow the new install, and is removed. If it differs
  from the source, the install stops first (`--force` replaces it anyway). A
  file with the same name in *another* app's directory always stops the install.
* Relative paths in unit files (`Volume=./data:/data`) resolve against the
  installed copy, not against `/opt/podman/<app>`. `redh check` fails them;
  write them absolute.
* A symlink in `quadlet/` or `caddy/` that leads out of the app's own directory
  stops the install, `--force` or not, and `redh check` fails it: root would
  read what it points to, and hand a copy to the podman user. A link staying
  inside the app is fine, and is installed as a plain file. Root opens these
  files one path component at a time, so a link swapped in after the check
  gets it no further. Links elsewhere in the app (`data/`) mean something to
  the container, and do not matter.
* `--dry-run` shows the plan. `--restart` restarts the app afterwards.

## list

```
APP            LAYOUT    UNITS  ACTIVE  RUNNING  INSTALLED  CADDY
bookstack      quadlet/  3      3/3     2        in sync    1
vaultwarden    none      -      -       1        no         -

3 containers running.
```

`ACTIVE` counts the units redh installed that systemd reports active.
`RUNNING` comes from podman and counts every running container of the app,
however it was started. A container's app comes from its systemd unit, from the
Quadlet file behind that unit, from the directories it bind-mounts under
`/opt/podman/<app>`, or from its name. Containers that fit no app are listed
underneath.

`--json` writes the same as one JSON document, for programs such as RedhAPI.
Counts are numbers, `0` where the table shows `-`; `null` stands for what the
table cannot tell (`?`), and for a layout or install state it shows as `-`.
Warnings go into the document, so the output stays JSON. With nextcloud running
and a container started by hand:

```
$ sudo redh list --json
{
  "root": "/opt/podman",
  "apps": [
    {
      "name": "nextcloud",
      "layout": "quadlet/",
      "units": 3,
      "services": 2,
      "active": 2,
      "running": 1,
      "installed": "in sync",
      "caddy": 1
    }
  ],
  "containers": {
    "running": 2,
    "in_no_app": [
      {
        "name": "scratch",
        "image": "docker.io/library/busybox:1",
        "unit": null
      }
    ]
  },
  "warnings": []
}
```

`active` and `services` are the two numbers of `ACTIVE`. When podman cannot be
asked, `containers` and every app's `running` are `null`, and `warnings` says why.

## start, stop, restart

An app's units are the services its *installed* files generate. Templates are
skipped unless they set `DefaultInstance=`. They are started or stopped with one
`systemctl --user` call, so systemd orders them, and then each one is reported
on its own:

```
start nextcloud
  starting 2 units …
  FAIL  nextcloud.service            failed, result exit-code, exit status 125
        Oct 05 13:10:01 srv podman[4242]: Error: statfs /opt/podman/nextcloud/data: no such file or directory
  ok    nextcloud-network.service    active (exited) — already running
  ok    nextcloud-db-volume.service  active (exited)
  → 2 ok, 1 failed
```

`.build` and `.volume` units are tasks: Quadlet makes them oneshots without
`RemainAfterExit=`, so they run, exit and are `inactive` again. redh reports them
as `finished` when they succeeded, and only their failure counts. Starting an
app that already runs does not run its tasks again.

After starting, redh waits `settle` seconds (5 by default). Then it flags crash
loops, units that restarted, and containers whose health check says unhealthy.
Every unit that did not end up where it should gets the tail of its journal.

Stopping an app whose network or volume another app uses also stops that app's
containers (Quadlet makes them `Requires=` it). redh says so before it happens.

After stopping, redh asks podman whether any container of the app still runs,
for example one started by a hand-copied Quadlet file or a plain `podman run`.
Each one is a FAIL, with the command that stops it. `stop --all` goes further
and ends with `still running?`: it fails while *any* container of the podman
user runs, whether or not it belongs to an app. That makes it safe to chain:

```sh
redh stop --all && borg create ...; redh start --all
```

`start --all` starts every installed app, including any you had stopped on
purpose. It does not start containers redh does not manage.

## update

When something that should update automatically did not, the first thing to
try is doing it by hand:

```
redh update nextcloud
ok    last automatic update 2026-10-05 00:00: success

update nextcloud
  pulling 1 image …
  ok    docker.io/library/nextcloud:30  new image 52ccd19fdc47 → 4be1f2c3d4e5
  restarting 1 unit …
  ok    nextcloud.service            active (running) — restarted, healthy
  ok    nextcloud-network.service    active (exited) — already running
  → 2 ok, 0 failed
```

1. The app's `.build` units run again, and its images are pulled — those of
   `.image` units too. This happens whether or not a container sets
   `AutoUpdate=`.
2. Only the units whose running container is now behind its image restart.
3. They are checked like `start` checks them. If one fails, redh prints the
   `podman tag` that points the name back at the image it ran before.

`update --dry-run` changes nothing and asks `podman auto-update --dry-run`
instead, which is exactly what the timer sees: for each unit, whether an update
is available, or that auto-update leaves it alone. The first line of both, and
`redh check`, say how the last automatic run went, with its journal if it
failed. Images inside `.kube` YAML are left to podman auto-update.

### Images you build yourself

An image built by hand (`podman build -t localhost/app .`) lives only in the
podman user's image store. `podman image prune -a` deletes it as soon as no
container uses it — and a Quadlet container only exists while its unit runs,
so after `redh stop` nothing does. The next start then makes podman pull it,
`localhost/` counts as a registry host, and the pull goes to
`https://localhost/v2/` — on this host, Caddy, which knows no certificate for
`localhost`:

```
Error: initializing source docker://localhost/app:latest: pinging container registry
localhost: Get "https://localhost/v2/": remote error: tls: internal error
```

A `.build` unit makes the image part of the app. The container names the
`.build` file instead of the image, so Quadlet runs the build first, whenever
it starts; podman's layer cache keeps that quick when nothing changed:

```ini
# quadlet/app.build
[Build]
ImageTag=localhost/app:latest
File=/opt/podman/app/src/Containerfile
SetWorkingDirectory=file

# quadlet/app.container
[Container]
Image=app.build
AutoUpdate=local
```

`redh update app` runs the build again and restarts the container if the
image changed; after a `git pull` of the sources that is the whole deploy.

## uninstall, prune

`uninstall` stops the app, removes its installed Quadlet files and reloads.
`/opt/podman/<app>` stays.

`prune` removes the app completely:

1. Stops it.
2. Removes the installed files.
3. Removes the containers, pods, named volumes and networks its units created.
4. Deletes `/opt/podman/<app>`.

It shows the plan first and asks you to type the app's name; `--yes` skips
that, `--dry-run` stops after the plan, and `--keep-volumes` keeps the named
volumes. It refuses when:

* another app depends on this one, unless `--force`;
* a unit will not stop, or podman still runs a container of the app (also
  one redh did not start) — then nothing is deleted;
* the app directory is a symlink, or resolves to somewhere outside
  `/opt/podman`.

Images stay; `podman image prune` as the podman user removes unused ones.

## export, import

Moves an app to another host, as one file:

```
$ sudo redh export baikal server.internal
export baikal
  + /opt/podman/baikal/
  → alex@server.internal:Downloads/
  stopping 2 units …
  packing baikal …
  ok    baikal-20261105-113012.redh.tar.gz  412 MB, alex's alone (0600): it holds baikal's secrets, unencrypted
  ok    baikal.service  active (running) — started
  sending to alex@server.internal:Downloads/ …
  ok    sent to alex@server.internal:Downloads/
  info  then, on the other host: sudo redh import baikal
```

| `DESTINATION` | Where it goes |
|---------------|---------------|
| (none) | your `~/Downloads` here |
| `server.internal` | `$SUDO_USER@server.internal:Downloads/`, in the home directory there |
| `new_user@server.internal` | `new_user@server.internal:Downloads/` |
| `new_user@server.internal:/some/path` | that path |
| `/some/path` | that directory here |

`sudo redh import` looks in your `~/Downloads`. With archives of several apps
there, `redh import baikal` picks the newest of that app; a path picks exactly
that file. It unpacks to `/opt/podman/baikal` and goes no further:
`redh check`, `install` and `start` come next, because ports and site addresses
may need changing first. `-n` shows what either would do.

* **What goes in:** the app's directory and its named volumes. What an install
  makes again (the Quadlet copies, the Caddy snippet) does not. Export names
  what cannot come along: bind mounts outside the directory, things another app
  provides, images built by hand.
* **A running app is stopped** for packing and started again before anything
  is sent, so the downtime does not depend on the network. A database copied
  mid-write is not a backup. `--live` packs it while it runs, at that risk.
* **Ownership survives.** Files in an app directory belong to its containers'
  subordinate UIDs, and those differ between hosts. tar runs in `podman
  unshare`, so the archive holds the UIDs as the containers see them, and they
  go back through the other host's mapping.
* **The archive holds secrets, unencrypted.** It is `0600` and yours, also at
  the other end. Sending removes the local copy; import leaves the archive
  there and reminds you to delete it.
* **rsync runs as you,** because root has no ssh keys. If your keys are only in
  an agent, note that sudo drops `SSH_AUTH_SOCK` (`Defaults env_keep +=
  "SSH_AUTH_SOCK"` in sudoers keeps it). If sending fails, the archive stays in
  `~/Downloads`, and redh prints the rsync command to run without sudo.
* **An archive is not trusted.** Before anything is unpacked, root reads it to
  the end: gzip's checksum has to match, and no entry may leave `<app>/` and
  `.redh/`, lie behind a symlink, be a device, or be a symlink out of the app
  in `quadlet/` or `caddy/`, where root reads on `redh install`. Unpacking then
  happens as the podman user, in a directory of its own; only when all of it
  worked does the app move into place.
* **Import replaces nothing.** An existing app, a leftover install or an existing
  volume of that name stops it; `redh prune` removes those first.

## Settings

`/etc/redh-manager.conf` is optional (or `-c PATH`, or `$REDH_CONFIG`). The
defaults match the layout above; see `redh-manager.conf.example`.
`ignore = backups, scripts` hides directories under `/opt/podman` that are not
apps (`ohmyzsh` and friends, when the apps root is the podman user's home).
`caddy_dir` is where snippets get installed (`/etc/caddy/Caddyfile.d`);
`caddy_service` names the unit Caddy runs as and reloads, if it is not
`caddy.service`. `systemd_subdir` and `user_unit_dir` are the plain units'
directory in an app, and where they get installed
(`<home>/.config/systemd/user`).

## As the podman user

Every command also works as `podman` itself. `prune` then deletes through
`podman unshare`, which can remove files owned by the containers' subordinate
UIDs.

## Code

```
pyproject.toml     the package, and the redh command
redh/
  cli.py           arguments, permissions, locking
  completion.py    tab completion: argcomplete, and the apps' names for it
  config.py        settings
  host.py          running commands as root / as the podman user
  confine.py       reading and writing the podman user's files as root, never through a link
  app.py           where an app's sources and installed copy are
  units.py         reading Quadlet files: names, keys, drop-ins
  plan.py          what an install would change
  init.py          init: a new app's skeleton
  checks.py        redh check
  caddy.py         imports, isolated adapting, upstreams and ports
  install.py       install, uninstall
  userunits.py     plain units from systemd/: plan, copy, enable
  lifecycle.py     start, stop, restart, status, list
  running.py       what podman runs, and which app each container belongs to
  update.py        update: build, pull, restart what is behind
  envfiles.py      who besides their owner can read env files and other secrets
  perms.py         the permission policy: rules, tests, --fix
  sites.py         installing Caddy snippets into /etc/caddy/Caddyfile.d
  prune.py         prune
  transfer.py      export, import
tests/             unittest; a fake systemd/podman/Caddy/Quadlet in fakes.py
```

```sh
python3 -m unittest discover -s tests
```

## Licence

GPL-3.0, see `LICENSE`.
