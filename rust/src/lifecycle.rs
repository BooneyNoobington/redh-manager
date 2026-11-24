//! Starting, stopping and looking at an app's units.
//!
//! An app's units are the services its *installed* Quadlet files generate — that is
//! what systemd knows about. systemd is not the last word on what runs, though: podman
//! is asked too (see running.rs), however a container was started.

use std::collections::{BTreeMap, HashMap};

use crate::app::Context;
use crate::host::{State, finished, is_task, prop, secs};
use crate::plan::make_plan;
use crate::running::{self, Container};
use crate::sites;
use crate::units::QuadletUnit;
use crate::util::{Error, Res, plural};

const HEALTH_FORMAT: &str = "{{.State.Health.Status}}";

/// (service, unit) for every unit of the installed copy that can be started.
pub fn services_of(ctx: &Context, app: &str) -> Vec<(String, QuadletUnit)> {
    ctx.installed(app).units.iter().filter_map(|u| u.startable().map(|s| (s, u.clone()))).collect()
}

/// 'active (running)', 'failed (exit-code), exit status 125', 'finished', ...
pub fn describe(state: &State) -> String {
    if finished(state) {
        return "finished".into();
    }
    let active =
        [prop(state, "ActiveState"), prop(state, "LoadState")].into_iter().find(|s| !s.is_empty()).unwrap_or("?");
    let sub = prop(state, "SubState");
    let mut text = if !sub.is_empty() && sub != active { format!("{active} ({sub})") } else { active.to_string() };
    let result = prop(state, "Result");
    if !result.is_empty() && result != "success" {
        text += &format!(", result {result}");
    }
    let status = prop(state, "ExecMainStatus");
    if !status.is_empty() && status != "0" && active != "active" {
        text += &format!(", exit status {status}");
    }
    text
}

pub fn since(state: &State) -> String {
    let key = if prop(state, "ActiveState") == "active" { "ActiveEnterTimestamp" } else { "InactiveEnterTimestamp" };
    let parts: Vec<&str> = prop(state, key).split_whitespace().collect();
    if parts.len() >= 3 { format!("since {} {}", parts[1], &parts[2][..parts[2].len().min(5)]) } else { String::new() }
}

/// healthy/unhealthy/starting for active containers that define a HealthCmd=.
pub fn health(
    ctx: &Context,
    pairs: &[(String, QuadletUnit)],
    states: &HashMap<String, State>,
) -> HashMap<String, String> {
    let mut found = HashMap::new();
    for (name, unit) in pairs {
        let active = states.get(name).map(|s| prop(s, "ActiveState")) == Some("active");
        let (Some(container), true, true, true) =
            (unit.resource_name(), unit.ext == ".container", unit.get("HealthCmd").is_some(), active)
        else {
            continue;
        };
        let result = ctx.host.podman(&["inspect", "--format", HEALTH_FORMAT, &container], secs(30));
        if result.ok() && !result.out.trim().is_empty() {
            found.insert(name.clone(), result.out.trim().to_string());
        }
    }
    found
}

/// The units whose being active matters: tasks only count when they failed.
pub fn counted(states: &HashMap<String, State>, names: &[String]) -> Vec<String> {
    names
        .iter()
        .filter(|n| {
            let state = states.get(*n).cloned().unwrap_or_default();
            !(is_task(&state) && prop(&state, "ActiveState") != "failed")
        })
        .cloned()
        .collect()
}

fn from_lines(c: &Container) -> Vec<String> {
    if c.source_path.is_empty() { Vec::new() } else { vec![format!("from {}", c.source_path)] }
}

/// The app's units as systemd sees them, plus whatever else of it podman runs.
pub fn status_app(ctx: &Context, app: &str, containers: Option<&[Container]>) -> Res<bool> {
    let out = &ctx.out;
    out.heading(app);
    let src = ctx.sources(app)?;
    let inst = ctx.installed(app);
    let mine: Vec<&Container> = containers.unwrap_or(&[]).iter().filter(|c| c.app.as_deref() == Some(app)).collect();
    if !inst.exists {
        let hint = if src.units.is_empty() { String::new() } else { format!(" — redh install {app}") };
        out.status("info", &format!("not installed by redh{hint}"), &[]);
        for c in mine {
            out.status("ok", &format!("running: {}", running::describe(c)), &from_lines(c));
        }
        return Ok(true);
    }
    let pairs = services_of(ctx, app);
    let names: Vec<String> = pairs.iter().map(|(n, _)| n.clone()).collect();
    let states = ctx.host.state_of(&names).map_err(Error::Host)?;
    let healthy = health(ctx, &pairs, &states);
    let width = names.iter().map(String::len).max().unwrap_or(0);
    let mut ok = true;
    for (name, _) in &pairs {
        let state = states.get(name).cloned().unwrap_or_default();
        let active = prop(&state, "ActiveState");
        let (mut level, text) = if prop(&state, "LoadState") != "loaded" && matches!(active, "inactive" | "") {
            ("FAIL", format!("not loaded ({})", prop(&state, "LoadState")))
        } else {
            let level = match active {
                "active" => "ok",
                "inactive" => "info",
                "failed" => "FAIL",
                _ => "warn",
            };
            (if finished(&state) { "ok" } else { level }, describe(&state))
        };
        let mut extras: Vec<String> = [since(&state), healthy.get(name).cloned().unwrap_or_default()]
            .into_iter()
            .filter(|x| !x.is_empty())
            .collect();
        if healthy.get(name).map(String::as_str) == Some("unhealthy") {
            level = "warn";
        }
        let restarts: usize = prop(&state, "NRestarts").parse().unwrap_or(0);
        if restarts > 0 {
            extras.push(plural(restarts, "restart"));
        }
        ok = ok && level != "FAIL";
        let extras = if extras.is_empty() { String::new() } else { format!(", {}", extras.join(", ")) };
        out.status(level, &format!("{name:<width$}  {text}{extras}"), &[]);
    }
    for unit in inst.units.iter().filter(|u| u.startable().is_none()) {
        out.status("skip", &format!("{} is a template; instances are started by name", unit.service()), &[]);
    }
    for c in mine {
        if !names.contains(&c.unit) {
            out.status("warn", &format!("also running, outside redh: {}", running::describe(c)), &from_lines(c));
        }
    }
    if src.exists && ((!src.units.is_empty() && !make_plan(ctx, app)?.in_sync()) || !sites::plan(ctx, app)?.in_sync()) {
        out.status("info", &format!("the installed copy is out of date → redh install {app}"), &[]);
    }
    Ok(ok)
}

pub fn list_apps(ctx: &Context) -> Res<i32> {
    let out = &ctx.out;
    let apps = ctx.known_apps()?;
    let names: Vec<String> = apps.iter().flat_map(|a| ctx.installed(a).services()).collect();
    let states = if names.is_empty() {
        HashMap::new()
    } else {
        ctx.host.state_of(&names).unwrap_or_else(|e| {
            out.status_at("warn", &e, &[], 0, None);
            HashMap::new()
        })
    };
    let containers = match running::running(ctx) {
        Ok(c) => Some(c),
        Err(e) => {
            out.status_at("warn", &format!("cannot tell which containers run: {e}"), &[], 0, None);
            None
        }
    };
    let mut per_app: BTreeMap<String, usize> = BTreeMap::new();
    for c in containers.iter().flatten() {
        if let Some(app) = &c.app {
            *per_app.entry(app.clone()).or_default() += 1;
        }
    }

    let mut rows: Vec<[String; 7]> =
        vec![["APP", "LAYOUT", "UNITS", "ACTIVE", "RUNNING", "INSTALLED", "CADDY"].map(String::from)];
    for app in &apps {
        let src = ctx.sources(app)?;
        let inst = ctx.installed(app);
        let services = inst.services();
        let site = sites::plan(ctx, app)?;
        let installed = if !src.exists {
            "orphaned".to_string()
        } else if src.units.is_empty() && site.wanted.is_empty() {
            "-".to_string()
        } else if (!src.units.is_empty() && !inst.exists) || (src.units.is_empty() && site.fresh()) {
            "no".to_string()
        } else {
            let in_sync = site.in_sync() && (src.units.is_empty() || make_plan(ctx, app)?.in_sync());
            (if in_sync { "in sync" } else { "outdated" }).to_string()
        };
        let relevant = if states.is_empty() { services.clone() } else { counted(&states, &services) };
        let unit_state = if !relevant.is_empty() && !states.is_empty() {
            let active =
                relevant.iter().filter(|n| states.get(*n).map(|s| prop(s, "ActiveState")) == Some("active")).count();
            format!("{active}/{}", relevant.len())
        } else if relevant.is_empty() {
            "-".to_string()
        } else {
            "?".to_string()
        };
        let up = match &containers {
            None => "?".to_string(),
            Some(_) => per_app.get(app).map_or("-".to_string(), usize::to_string),
        };
        let units = if src.exists && !src.units.is_empty() { src.units.len() } else { inst.units.len() };
        rows.push([
            app.clone(),
            if src.exists { src.layout.clone() } else { "-".into() },
            if units > 0 { units.to_string() } else { "-".into() },
            unit_state,
            up,
            installed,
            if src.snippets.is_empty() { "-".into() } else { src.snippets.len().to_string() },
        ]);
    }
    if apps.is_empty() {
        out.write(&format!("no apps under {}", ctx.settings().root.display()));
    } else {
        let widths: Vec<usize> = (0..7).map(|i| rows.iter().map(|r| r[i].chars().count()).max().unwrap_or(0)).collect();
        for (index, row) in rows.iter().enumerate() {
            let line: Vec<String> = row.iter().zip(&widths).map(|(cell, w)| format!("{cell:<w$}")).collect();
            let line = line.join("  ").trim_end().to_string();
            out.write(&if index == 0 { out.paint("1", &line) } else { line });
        }
    }
    if let Some(containers) = &containers {
        let stray: Vec<&Container> = containers.iter().filter(|c| c.app.is_none()).collect();
        out.blank();
        let tail = if stray.is_empty() { ".".to_string() } else { format!(", {} of them in no app:", stray.len()) };
        out.write(&format!("{} running{tail}", plural(containers.len(), "container")));
        for c in stray {
            out.write(&format!("  {}", running::describe(c)));
        }
    }
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fake::{Lab, blank, nextcloud};

    /// Containers redh did not start: a hand-installed Quadlet file, 'podman run'.
    fn hand_started() -> Lab {
        let lab = Lab::new();
        for app in ["vaultwarden", "redhdb", "yattee-server"] {
            lab.mkdir(&format!("{app}/data"));
        }
        let flat = lab.write(
            lab.settings.quadlet_dir.join("vw.container").to_str().unwrap(),
            "[Container]\nImage=docker.io/vaultwarden/server:1\n",
        );
        let mut state = blank();
        state.insert("ActiveState".into(), "active".into());
        state.insert("SourcePath".into(), flat.display().to_string());
        lab.system.units.borrow_mut().insert("vw.service".into(), state);
        let data = lab.path("vaultwarden/data");
        lab.system.run_container(
            "vaultwarden",
            "vw.service",
            "docker.io/vaultwarden/server:1",
            &[data.to_str().unwrap()],
        );
        lab.system.run_container("systemd-redhdb", "", "docker.io/library/mariadb:11", &[]);
        lab.system.run_container("portainer_agent", "", "docker.io/portainer/agent:2", &[]);
        lab
    }

    #[test]
    fn containers_are_matched_to_apps() {
        let lab = hand_started();
        let found: Vec<(String, Option<String>, String)> =
            running::running(&lab.ctx()).unwrap().into_iter().map(|c| (c.name, c.app, c.why)).collect();
        assert_eq!(
            found,
            [
                ("systemd-redhdb".to_string(), Some("redhdb".to_string()), "name".to_string()),
                ("vaultwarden".into(), Some("vaultwarden".into()), "mount".into()),
                ("portainer_agent".into(), None, "".into()),
            ]
        );
    }

    #[test]
    fn list_shows_what_runs_and_what_belongs_nowhere() {
        let lab = hand_started();
        list_apps(&lab.ctx()).unwrap();
        let out = lab.output();
        let rows: BTreeMap<&str, Vec<&str>> = out
            .lines()
            .skip(1)
            .take(3)
            .map(|l| (l.split_whitespace().next().unwrap(), l.split_whitespace().collect()))
            .collect();
        assert_eq!(rows["redhdb"][4], "1");
        assert_eq!(rows["vaultwarden"][4], "1");
        assert_eq!(rows["yattee-server"][4], "-");
        assert!(out.contains("3 containers running, 1 of them in no app:\n"), "{out}");
        assert!(out.contains("  portainer_agent (docker.io/portainer/agent:2), no systemd unit\n"), "{out}");
    }

    #[test]
    fn status_of_an_app_redh_did_not_install() {
        let lab = hand_started();
        let ctx = lab.ctx();
        let containers = running::running(&ctx).unwrap();
        status_app(&ctx, "vaultwarden", Some(&containers)).unwrap();
        let out = lab.output();
        assert!(out.contains("not installed by redh"), "{out}");
        assert!(out.contains("running: vaultwarden (docker.io/vaultwarden/server:1), unit vw.service"), "{out}");
        assert!(out.contains("from /opt/podman/.config/containers/systemd/vw.container"), "{out}");
    }

    #[test]
    fn status_and_list_of_an_installed_app() {
        let lab = Lab::new();
        nextcloud(&lab);
        lab.install_by_hand("nextcloud");
        lab.system.start_all();
        let ctx = lab.ctx();
        assert!(status_app(&ctx, "nextcloud", None).unwrap());
        let out = lab.output();
        assert!(
            out.contains("ok    nextcloud.service            active (running), since 2026-10-05 13:10, healthy"),
            "{out}"
        );
        assert!(out.contains("ok    nextcloud-db-volume.service  finished, since 2026-10-05 13:10"), "{out}");
        assert!(!out.contains("out of date"), "{out}");
        list_apps(&ctx).unwrap();
        let out = lab.output();
        let lines: Vec<&str> = out.lines().collect();
        assert_eq!(
            lines[0].split_whitespace().collect::<Vec<_>>(),
            ["APP", "LAYOUT", "UNITS", "ACTIVE", "RUNNING", "INSTALLED", "CADDY"]
        );
        assert_eq!(
            lines[1].split_whitespace().collect::<Vec<_>>(),
            ["nextcloud", "quadlet/", "3", "2/2", "1", "in", "sync", "1"]
        );
        assert_eq!(*lines.last().unwrap(), "1 container running.");
    }

    #[test]
    fn describing_states() {
        let mut state = blank();
        state.insert("ActiveState".into(), "failed".into());
        state.insert("SubState".into(), "failed".into());
        state.insert("Result".into(), "exit-code".into());
        state.insert("ExecMainStatus".into(), "125".into());
        assert_eq!(describe(&state), "failed, result exit-code, exit status 125");
        let mut task = blank();
        task.insert("Type".into(), "oneshot".into());
        task.insert("InactiveEnterTimestamp".into(), "Mon 2026-10-05 13:10:00 CEST".into());
        assert_eq!(describe(&task), "finished");
        assert_eq!(since(&task), "since 2026-10-05 13:10");
    }
}
