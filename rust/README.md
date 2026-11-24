# redh in Rust

The Rust port of `redh.py`: same commands, same output, one static binary.
Just for fun; the Python version in the directory above stays the reference.

```sh
make                  # target/x86_64-unknown-linux-musl/release/redh, static
make test             # cargo test
make check            # rustfmt and clippy
sudo make install     # /usr/local/sbin/redh; builds nothing as root
```

The musl target needs `rustup target add x86_64-unknown-linux-musl` once.

## Ported so far

| Command | State |
|---------|-------|
| `list` | ported, output identical to redh.py; `--json` not yet, clap refuses the option |
| `status` | ported, output identical to redh.py |
| `check`, `install`, `uninstall`, `start`, `stop`, `restart`, `update`, `prune` | not yet; they say so |

## Layout

Module for module like the Python package:

```
src/
  main.rs        arguments and dispatch
  config.rs      settings
  sys.rs         libc: users, groups, identities, access checks, flock — all the unsafe
  host.rs        the Host trait: every command runs through Host::exec
  report.rs      output
  units.rs       reading Quadlet files
  app.rs         an app's sources and installed copy
  plan.rs        what an install would change
  sites.rs       Caddy snippets in /etc/caddy/Caddyfile.d
  running.rs     what podman runs, and to which app it belongs
  lifecycle.rs   list, status
  fake.rs        the tests' pretend systemd and podman
```
