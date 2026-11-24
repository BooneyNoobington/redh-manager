//! redh — manage rootless Podman Quadlet apps that live under /opt/podman/<app>.
//!
//! The Rust port of redh.py: same commands, same output, one static binary.

// Parts of the foundation are only used by commands not ported yet.
#![allow(dead_code)]

mod app;
mod config;
mod host;
mod lifecycle;
mod plan;
mod report;
mod running;
mod sites;
mod sys;
mod units;
mod util;

#[cfg(test)]
mod fake;

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use crate::app::Context;
use crate::host::{Host, RealHost};
use crate::report::Printer;
use crate::util::{Error, Res, close_match, join_names};

#[derive(Parser)]
#[command(
    name = "redh",
    version,
    about = "Manage rootless Podman Quadlet apps that live under /opt/podman/<app>.",
    after_help = "Run as root (or as the podman user). 'redh COMMAND -h' explains a command."
)]
struct Cli {
    /// settings file (default: /etc/redh-manager.conf if it exists).
    #[arg(short = 'c', long = "config", value_name = "PATH", global = true)]
    config: Option<PathBuf>,
    /// also show what is fine, not only what is not.
    #[arg(short, long, global = true)]
    verbose: bool,
    /// plain output, even on a terminal.
    #[arg(long = "no-color", global = true)]
    no_color: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// one line per app: layout, units, how many run, install state.
    List,
    /// check apps: layout, unit files, generator dry run, auto-update, boot, Caddy snippet,
    /// permissions, env files, install state. Without APP: every app.
    Check {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
        /// first apply the permission policy and make env files private (as root), then check.
        #[arg(long)]
        fix: bool,
    },
    /// what systemd says about each unit. Without APP: every app.
    Status {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
    },
    /// (re)install the apps' Quadlet files and reload systemd.
    Install {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
        /// every app under the apps root.
        #[arg(long)]
        all: bool,
        /// show what would change, change nothing.
        #[arg(short = 'n', long)]
        dry_run: bool,
        /// install even though the checks found problems.
        #[arg(long)]
        force: bool,
        /// restart the app afterwards.
        #[arg(long)]
        restart: bool,
    },
    /// stop the apps and remove their installed Quadlet files; /opt/podman/<app> stays.
    Uninstall {
        #[arg(value_name = "APP", required = true)]
        apps: Vec<String>,
        /// show what would go, change nothing.
        #[arg(short = 'n', long)]
        dry_run: bool,
        /// remove the files even if units keep running.
        #[arg(long)]
        force: bool,
    },
    /// start every unit of the apps.
    Start {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
        /// every installed app.
        #[arg(long)]
        all: bool,
    },
    /// stop every unit of the apps.
    Stop {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
        /// every installed app.
        #[arg(long)]
        all: bool,
    },
    /// stop, then start every unit of the apps.
    Restart {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
        /// every installed app.
        #[arg(long)]
        all: bool,
    },
    /// by hand, what podman auto-update does: build and pull the apps' images, restart what
    /// runs an older one.
    Update {
        #[arg(value_name = "APP")]
        apps: Vec<String>,
        /// every installed app.
        #[arg(long)]
        all: bool,
        /// ask podman auto-update what it would do; change nothing.
        #[arg(short = 'n', long)]
        dry_run: bool,
    },
    /// stop the app, remove its units, networks and volumes, and delete /opt/podman/<app>.
    Prune {
        #[arg(value_name = "APP")]
        app: String,
        /// show what would go, change nothing.
        #[arg(short = 'n', long)]
        dry_run: bool,
        /// do not ask for confirmation.
        #[arg(short = 'y', long)]
        yes: bool,
        /// leave the named volumes alone.
        #[arg(long)]
        keep_volumes: bool,
        /// carry on when other apps depend on it or units do not stop.
        #[arg(long)]
        force: bool,
    },
}

/// App names as given — also as paths, like 'nextcloud/' from tab completion.
fn resolve(ctx: &Context, names: &[String], pool: &[String], what: &str) -> Res<Vec<String>> {
    let mut apps: Vec<String> = Vec::new();
    for name in names {
        let app = PathBuf::from(name.trim_end_matches('/'))
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| name.clone());
        if !pool.contains(&app) && ctx.apps()?.contains(&app) {
            return Err(Error::Die(format!("{app} is not installed → redh install {app}")));
        }
        if !pool.contains(&app) {
            let hint = match close_match(&app, pool) {
                Some(close) => format!(" — did you mean {close}?"),
                None if pool.is_empty() => format!(" (there are no {what})"),
                None => format!(" ({what}: {})", join_names(pool, 8)),
            };
            return Err(Error::Die(format!("unknown app '{app}'{hint}")));
        }
        if !apps.contains(&app) {
            apps.push(app);
        }
    }
    Ok(apps)
}

fn dispatch(ctx: &Context, command: &Command) -> Res<i32> {
    match command {
        Command::List => lifecycle::list_apps(ctx),
        Command::Status { apps } => {
            let known = ctx.known_apps()?;
            let apps = if apps.is_empty() { known.clone() } else { resolve(ctx, apps, &known, "apps")? };
            if apps.is_empty() {
                ctx.out.write(&format!("no apps under {}", ctx.settings().root.display()));
            }
            let containers = match running::running(ctx) {
                Ok(c) => Some(c),
                Err(e) => {
                    ctx.out.status_at("warn", &format!("cannot tell which containers run: {e}"), &[], 0, None);
                    None
                }
            };
            let mut ok = true;
            for (index, app) in apps.iter().enumerate() {
                if index > 0 {
                    ctx.out.blank();
                }
                ok = lifecycle::status_app(ctx, app, containers.as_deref())? && ok;
            }
            Ok(if ok { 0 } else { 1 })
        }
        _ => Err(Error::Die("not ported to Rust yet; use redh.py for now".into())),
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let settings = match config::load(cli.config.as_deref()) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("error: {e}");
            return ExitCode::from(1);
        }
    };
    let host = RealHost::new(settings);
    if !(host.is_root() || host.is_user()) {
        eprintln!("error: run redh as root (or as {})", host.settings().user);
        return ExitCode::from(1);
    }
    let user = host.settings().user.clone();
    let out = Printer::stdout(if cli.no_color { Some(false) } else { None }, cli.verbose);
    let ctx = Context::new(Box::new(host), out);
    match dispatch(&ctx, &cli.command) {
        Ok(code) => ExitCode::from(code as u8),
        Err(Error::Die(message)) => {
            eprintln!("error: {message}");
            ExitCode::from(1)
        }
        Err(Error::Host(message)) => {
            ctx.out.status_at("FAIL", &message, &[format!("is linger on? loginctl enable-linger {user}")], 0, None);
            ExitCode::from(2)
        }
    }
}
