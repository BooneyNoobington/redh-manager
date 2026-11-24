//! What is actually running: the podman user's containers, and their apps.
//!
//! systemd only knows the units redh installed; podman knows every container, however
//! it was started. Each container's app comes from, in order:
//!
//!     unit    its systemd unit is generated from one of the app's Quadlet files
//!     file    the Quadlet file behind its unit lives in the app's directory, or has
//!             the name of one of the app's files
//!     mount   it bind-mounts the app's directory under /opt/podman
//!     name    its name is the app's name (systemd-<app>, <app>-db, <app>_web)

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::app::{Context, name_of};
use crate::host::secs;
use crate::units::{QuadletUnit, quadlet_ext};

#[derive(Clone, Debug, Default)]
pub struct Container {
    pub id: String,
    pub name: String,
    pub image: String,
    /// The image it actually runs.
    pub image_id: String,
    /// The systemd unit running it, if any.
    pub unit: String,
    /// Bind-mounted host paths.
    pub sources: Vec<String>,
    /// The Quadlet file behind its unit.
    pub source_path: String,
    pub app: Option<String>,
    /// unit, file, mount or name
    pub why: String,
}

fn text(value: &Value, key: &str) -> String {
    value.get(key).and_then(Value::as_str).unwrap_or("").to_string()
}

/// Every running container of the podman user, each with its app if it has one.
pub fn running(ctx: &Context) -> Result<Vec<Container>, String> {
    let host = ctx.host.as_ref();
    let listing = host.podman(&["ps", "--quiet", "--no-trunc"], secs(60));
    if !listing.ok() {
        return Err(format!("podman ps failed: {}", listing.message()));
    }
    let ids: Vec<&str> = listing.out.split_whitespace().collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    // A container can stop between the two calls; inspect then reports the others
    // and fails, so the output counts, not the exit status.
    let mut args = vec!["container", "inspect"];
    args.extend(&ids);
    let inspected = host.podman(&args, secs(120));
    let records: Vec<Value> = if inspected.out.trim().is_empty() {
        Vec::new()
    } else {
        serde_json::from_str(&inspected.out).map_err(|_| format!("podman inspect: {}", inspected.message()))?
    };
    let mut containers = Vec::new();
    for record in &records {
        let running = record.get("State").map(|s| text(s, "Status")) == Some("running".into());
        if record.get("IsInfra").and_then(Value::as_bool).unwrap_or(false) || !running {
            continue;
        }
        let config = record.get("Config").cloned().unwrap_or(Value::Null);
        let labels = config.get("Labels").cloned().unwrap_or(Value::Null);
        let mounts = record.get("Mounts").and_then(Value::as_array).cloned().unwrap_or_default();
        let image = Some(text(record, "ImageName")).filter(|i| !i.is_empty()).unwrap_or_else(|| text(&config, "Image"));
        containers.push(Container {
            id: text(record, "Id").chars().take(12).collect(),
            name: text(record, "Name"),
            image,
            image_id: text(record, "Image"),
            unit: text(&labels, "PODMAN_SYSTEMD_UNIT"),
            sources: mounts
                .iter()
                .filter(|m| text(m, "Type") == "bind" && !text(m, "Source").is_empty())
                .map(|m| text(m, "Source"))
                .collect(),
            ..Default::default()
        });
    }
    assign(ctx, &mut containers)?;
    containers
        .sort_by(|a, b| (a.app.as_deref().unwrap_or("~"), &a.name).cmp(&(b.app.as_deref().unwrap_or("~"), &b.name)));
    Ok(containers)
}

/// The app whose directory (or install directory) holds path.
pub fn app_of_path(ctx: &Context, apps: &BTreeSet<String>, path: &Path) -> Option<String> {
    let s = ctx.settings();
    for base in [&s.quadlet_dir, &s.root] {
        if let Ok(rel) = path.strip_prefix(base) {
            let first = rel.iter().next()?.to_string_lossy().into_owned();
            if apps.contains(&first) {
                return Some(first);
            }
        }
    }
    None
}

fn realpath(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

fn assign(ctx: &Context, containers: &mut [Container]) -> Result<(), String> {
    let apps: BTreeSet<String> = ctx.known_apps()?.into_iter().collect();
    let mut by_service: HashMap<String, String> = HashMap::new();
    let mut by_file: HashMap<String, String> = HashMap::new();
    for app in &apps {
        let units: Vec<QuadletUnit> =
            ctx.installed(app).units.iter().chain(ctx.sources(app)?.units.iter()).cloned().collect();
        for unit in units {
            for name in [Some(unit.service()), unit.startable()].into_iter().flatten() {
                by_service.entry(name).or_insert_with(|| app.clone());
            }
            by_file.entry(unit.filename.clone()).or_insert_with(|| app.clone());
        }
    }
    let units: Vec<String> = containers
        .iter()
        .filter(|c| !c.unit.is_empty())
        .map(|c| c.unit.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let paths =
        if units.is_empty() { HashMap::new() } else { ctx.host.show(&units, &["SourcePath"]).unwrap_or_default() };

    for c in containers.iter_mut() {
        c.source_path = paths.get(&c.unit).and_then(|s| s.get("SourcePath")).cloned().unwrap_or_default();
        let mut app = by_service.get(&c.unit).cloned();
        let mut why = "unit";
        if app.is_none() && !c.source_path.is_empty() {
            let source = Path::new(&c.source_path);
            app = app_of_path(ctx, &apps, &realpath(source))
                .or_else(|| app_of_path(ctx, &apps, source))
                .or_else(|| by_file.get(&name_of(source)).cloned());
            why = "file";
        }
        if app.is_none() {
            let mut hits: BTreeMap<String, usize> = BTreeMap::new();
            for source in &c.sources {
                if let Some(a) = app_of_path(ctx, &apps, Path::new(source)) {
                    *hits.entry(a).or_default() += 1;
                }
            }
            if let Some((a, _)) = hits.into_iter().max_by(|x, y| x.1.cmp(&y.1).then(y.0.cmp(&x.0))) {
                app = Some(a);
                why = "mount";
            }
        }
        if app.is_none() {
            let bare = c.name.strip_prefix("systemd-").unwrap_or(&c.name);
            let mut candidates: Vec<&String> = apps.iter().collect();
            candidates.sort_by_key(|a| std::cmp::Reverse(a.len()));
            if let Some(found) = candidates.into_iter().find(|a| {
                bare == a.as_str() || bare.starts_with(&format!("{a}-")) || bare.starts_with(&format!("{a}_"))
            }) {
                app = Some(found.clone());
                why = "name";
            }
        }
        c.why = if app.is_some() { why.to_string() } else { String::new() };
        c.app = app;
    }
    Ok(())
}

/// Every host path a container mounts, or will: from every app's units, from the
/// loose unit files in the Quadlet directory, and from what actually runs.
pub fn mount_sources(ctx: &Context) -> Result<BTreeSet<PathBuf>, String> {
    let mut units: Vec<QuadletUnit> = Vec::new();
    for app in ctx.known_apps()? {
        units.extend(ctx.sources(&app)?.units.iter().cloned());
        units.extend(ctx.installed(&app).units.iter().cloned());
    }
    for (rel, path) in ctx.tree().iter() {
        if !rel.contains('/')
            && quadlet_ext(rel).is_some()
            && let Ok(unit) = QuadletUnit::load(path, &[])
        {
            units.push(unit);
        }
    }
    let mut paths: BTreeSet<PathBuf> = units
        .iter()
        .flat_map(QuadletUnit::bind_sources)
        .filter(|(_, s)| s.starts_with('/') && !s.contains('%'))
        .map(|(_, s)| realpath(Path::new(&s)))
        .collect();
    if let Ok(containers) = running(ctx) {
        paths.extend(containers.iter().flat_map(|c| c.sources.iter().map(|s| realpath(Path::new(s)))));
    }
    Ok(paths)
}

/// The command that stops this one container, as root.
pub fn stop_hint(ctx: &Context, c: &Container) -> String {
    if !c.unit.is_empty() {
        return format!("{} stop {}", ctx.host.manager_hint(), c.unit);
    }
    format!("runuser -u {} -- podman stop {}", ctx.settings().user, c.name)
}

/// 'vaultwarden (docker.io/vaultwarden/server:1.32), unit vaultwarden.service'.
pub fn describe(c: &Container) -> String {
    let unit = if c.unit.is_empty() { "no systemd unit".to_string() } else { format!("unit {}", c.unit) };
    format!("{} ({}), {unit}", c.name, c.image)
}
