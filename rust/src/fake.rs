//! A pretend host for the tests: enough of systemd --user, podman and the journal.
//!
//! FakeHost is the real Host with only the process boundary replaced, so the command
//! lines redh builds are exercised; FakeSystem answers them from state kept in memory
//! and from the files a test laid out in a temporary directory.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashSet};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::time::Duration;

use serde_json::json;

use crate::app::Context;
use crate::config::Settings;
use crate::host::{Access, Host, Output, State, prop};
use crate::report::Printer;
use crate::sites;
use crate::units::{QuadletUnit, quadlet_ext};

pub const GENERATOR: &str = "/fake/libexec/podman/quadlet";

/// What Quadlet makes of each type: (Type=, RemainAfterExit=) of the generated service.
fn service_type(ext: &str) -> (&'static str, &'static str) {
    match ext {
        ".container" | ".kube" => ("notify", "no"),
        ".pod" => ("forking", "no"),
        ".network" | ".image" | ".artifact" => ("oneshot", "yes"),
        _ => ("oneshot", "no"), // .volume, .build
    }
}

#[derive(Clone, Debug, Default)]
pub struct ContainerInfo {
    pub unit: String,
    pub image: String,
    pub image_id: String,
    pub sources: Vec<String>,
}

#[derive(Default)]
pub struct FakeSystem {
    pub quadlet_dir: PathBuf,
    pub calls: RefCell<Vec<Vec<String>>>,
    pub units: RefCell<BTreeMap<String, State>>,
    pub live: RefCell<BTreeMap<String, QuadletUnit>>,
    /// name -> what 'podman ps' shows
    pub containers: RefCell<BTreeMap<String, ContainerInfo>>,
    pub fail_start: RefCell<HashSet<String>>,
    pub crash_loop: RefCell<HashSet<String>>,
    pub stuck: RefCell<HashSet<String>>,
    pub journal_lines: Vec<String>,
}

pub fn blank() -> State {
    [
        ("LoadState", "loaded"),
        ("ActiveState", "inactive"),
        ("SubState", "dead"),
        ("Result", "success"),
        ("NRestarts", "0"),
        ("ExecMainStatus", "0"),
        ("ActiveEnterTimestamp", ""),
        ("InactiveEnterTimestamp", ""),
        ("SourcePath", ""),
        ("Type", "simple"),
        ("RemainAfterExit", "no"),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v.to_string()))
    .collect()
}

fn set(state: &mut State, pairs: &[(&str, &str)]) {
    for (k, v) in pairs {
        state.insert(k.to_string(), v.to_string());
    }
}

fn result(argv: &[String], rc: i32, out: impl Into<String>, err: impl Into<String>) -> Output {
    Output { argv: argv.to_vec(), rc, out: out.into(), err: err.into() }
}

impl FakeSystem {
    pub fn new(settings: &Settings) -> Self {
        FakeSystem {
            quadlet_dir: settings.quadlet_dir.clone(),
            journal_lines: vec!["Oct 05 13:10:01 host podman[4242]: Error: something went wrong".into()],
            ..Default::default()
        }
    }

    pub fn run(&self, argv: &[String], _env: &[(String, String)]) -> Output {
        self.calls.borrow_mut().push(argv.to_vec());
        let tool = Path::new(&argv[0]).file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        match tool.as_str() {
            "systemctl" if argv.get(1).map(String::as_str) == Some("--user") => self.systemctl(&argv[2..]),
            "systemctl" => result(argv, 0, "LoadState=not-found\nUser=\nSupplementaryGroups=\n", ""),
            "podman" => self.podman(&argv[1..]),
            "journalctl" => result(argv, 0, self.journal_lines.join("\n") + "\n", ""),
            _ => result(argv, 127, "", format!("{tool}: command not found")),
        }
    }

    /// A container started some other way than through redh.
    pub fn run_container(&self, name: &str, unit: &str, image: &str, sources: &[&str]) {
        self.containers.borrow_mut().insert(
            name.into(),
            ContainerInfo {
                unit: unit.into(),
                image: image.into(),
                image_id: format!("sha256:{:012x}", name.len() * 7919),
                sources: sources.iter().map(|s| s.to_string()).collect(),
            },
        );
    }

    // -- systemd --------------------------------------------------------------------

    /// What the generator would produce from the Quadlet directory right now.
    fn generated(&self) -> BTreeMap<String, QuadletUnit> {
        let mut seen: BTreeMap<String, QuadletUnit> = BTreeMap::new();
        let Ok(entries) = std::fs::read_dir(&self.quadlet_dir) else { return BTreeMap::new() };
        let mut dirs = vec![self.quadlet_dir.clone()];
        let mut subdirs: Vec<PathBuf> = entries
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_dir() && !p.to_string_lossy().ends_with(".d"))
            .collect();
        subdirs.sort();
        dirs.extend(subdirs); // the top level first, then subdirectories — like Quadlet
        for dir in dirs {
            let Ok(entries) = std::fs::read_dir(&dir) else { continue };
            let mut paths: Vec<PathBuf> = entries.flatten().map(|e| e.path()).collect();
            paths.sort();
            for path in paths {
                let name = path.file_name().unwrap().to_string_lossy().into_owned();
                if quadlet_ext(&name).is_some() && path.is_file() && !seen.contains_key(&name) {
                    seen.insert(name, QuadletUnit::load(&path, std::slice::from_ref(&dir)).unwrap());
                }
            }
        }
        seen.into_values().map(|u| (u.service(), u)).collect()
    }

    pub fn daemon_reload(&self) {
        let live = self.generated();
        let mut units = self.units.borrow_mut();
        units.retain(|name, state| live.contains_key(name) || prop(state, "ActiveState") != "inactive");
        for (name, state) in units.iter_mut() {
            if !live.contains_key(name) {
                set(state, &[("LoadState", "not-found")]);
            }
        }
        for (name, unit) in &live {
            let state = units.entry(name.clone()).or_insert_with(blank);
            let (kind, remain) = service_type(&unit.ext);
            set(
                state,
                &[
                    ("LoadState", "loaded"),
                    ("SourcePath", &unit.path.display().to_string()),
                    ("Type", kind),
                    ("RemainAfterExit", remain),
                ],
            );
        }
        *self.live.borrow_mut() = live;
    }

    pub fn start_all(&self) {
        self.daemon_reload();
        let names: Vec<String> = self.units.borrow().keys().cloned().collect();
        for name in names {
            self.start(&name);
        }
    }

    fn start(&self, name: &str) -> bool {
        let unit = self.live.borrow().get(name).cloned();
        let mut units = self.units.borrow_mut();
        let Some(state) = units.get_mut(name) else { return false };
        if prop(state, "ActiveState") == "active" {
            return true;
        }
        if self.fail_start.borrow().contains(name) {
            set(
                state,
                &[
                    ("ActiveState", "failed"),
                    ("SubState", "failed"),
                    ("Result", "exit-code"),
                    ("ExecMainStatus", "125"),
                ],
            );
            return false;
        }
        if self.crash_loop.borrow().contains(name) {
            let restarts = prop(state, "NRestarts").parse::<u32>().unwrap_or(0) + 3;
            set(
                state,
                &[("ActiveState", "activating"), ("SubState", "auto-restart"), ("NRestarts", &restarts.to_string())],
            );
            return true;
        }
        if prop(state, "Type") == "oneshot" {
            if prop(state, "RemainAfterExit") == "no" {
                set(
                    state,
                    &[
                        ("ActiveState", "inactive"),
                        ("SubState", "dead"),
                        ("Result", "success"),
                        ("InactiveEnterTimestamp", "Mon 2026-10-05 13:10:00 CEST"),
                    ],
                );
            } else {
                set(
                    state,
                    &[
                        ("ActiveState", "active"),
                        ("SubState", "exited"),
                        ("Result", "success"),
                        ("ActiveEnterTimestamp", "Mon 2026-10-05 13:10:01 CEST"),
                    ],
                );
            }
            return true;
        }
        set(
            state,
            &[
                ("ActiveState", "active"),
                ("SubState", "running"),
                ("Result", "success"),
                ("ActiveEnterTimestamp", "Mon 2026-10-05 13:10:01 CEST"),
            ],
        );
        drop(units);
        if let Some(unit) = unit.filter(|u| u.ext == ".container") {
            let image = unit.get("Image").unwrap_or_default();
            self.containers.borrow_mut().insert(
                unit.resource_name().unwrap_or_default(),
                ContainerInfo {
                    unit: name.into(),
                    image_id: format!("sha256:{:012x}", image.len() * 104729),
                    image,
                    sources: unit.bind_sources().into_iter().map(|(_, s)| s).filter(|s| s.starts_with('/')).collect(),
                },
            );
        }
        true
    }

    fn stop(&self, names: &[String]) {
        for name in names {
            let mut units = self.units.borrow_mut();
            let Some(state) = units.get_mut(name) else { continue };
            if self.stuck.borrow().contains(name) || prop(state, "ActiveState") == "failed" {
                continue;
            }
            if prop(state, "ActiveState") == "active" {
                set(
                    state,
                    &[
                        ("ActiveState", "inactive"),
                        ("SubState", "dead"),
                        ("InactiveEnterTimestamp", "Mon 2026-10-05 14:00:00 CEST"),
                    ],
                );
            }
            self.containers.borrow_mut().retain(|_, info| &info.unit != name);
        }
    }

    fn systemctl(&self, args: &[String]) -> Output {
        let verb = args[0].as_str();
        let names: Vec<String> = match args.iter().position(|a| a == "--") {
            Some(at) => args[at + 1..].to_vec(),
            None => args[1..].to_vec(),
        };
        match verb {
            "daemon-reload" => {
                self.daemon_reload();
                result(args, 0, "", "")
            }
            "show" => {
                let props: Vec<&str> = args[1].split_once('=').map(|(_, p)| p.split(',').collect()).unwrap_or_default();
                let units = self.units.borrow();
                let blocks: Vec<String> = names
                    .iter()
                    .map(|name| {
                        let mut state = units.get(name).cloned().unwrap_or_else(|| {
                            let mut s = blank();
                            set(&mut s, &[("LoadState", "not-found")]);
                            s
                        });
                        state.insert("Id".into(), name.clone());
                        props.iter().map(|p| format!("{p}={}", prop(&state, p))).collect::<Vec<_>>().join("\n")
                    })
                    .collect();
                result(args, 0, blocks.join("\n\n") + "\n", "")
            }
            "start" | "restart" => {
                if verb == "restart" {
                    self.stop(&names);
                }
                // Every unit gets started, like systemd does; one failing does not stop the rest.
                let mut ok = true;
                for name in &names {
                    ok &= self.start(name);
                }
                result(args, if ok { 0 } else { 1 }, "", if ok { "" } else { "Job for x.service failed." })
            }
            "stop" => {
                self.stop(&names);
                result(args, 0, "", "")
            }
            "reset-failed" => {
                for name in &names {
                    if let Some(state) = self.units.borrow_mut().get_mut(name)
                        && prop(state, "ActiveState") == "failed"
                    {
                        set(state, &[("ActiveState", "inactive"), ("SubState", "dead"), ("Result", "success")]);
                    }
                }
                result(args, 0, "", "")
            }
            "is-system-running" => result(args, 0, "running\n", ""),
            "is-enabled" => result(args, 0, "enabled\n", ""),
            "is-active" => result(args, 0, "active\n", ""),
            "list-units" => result(args, 0, "", ""),
            _ => result(args, 1, "", format!("unknown verb {verb}")),
        }
    }

    // -- podman ---------------------------------------------------------------------

    fn podman(&self, args: &[String]) -> Output {
        let first = args.first().map(String::as_str).unwrap_or("");
        match first {
            "--version" => result(args, 0, "podman version 5.4.2\n", ""),
            "ps" => {
                let names: String = self.containers.borrow().keys().map(|n| format!("{n}\n")).collect();
                result(args, 0, names, "")
            }
            "container" if args.get(1).map(String::as_str) == Some("inspect") => {
                let containers = self.containers.borrow();
                let records: Vec<serde_json::Value> = args[2..]
                    .iter()
                    .filter_map(|name| containers.get(name).map(|info| (name, info)))
                    .map(|(name, info)| {
                        let labels = if info.unit.is_empty() { json!({}) } else { json!({"PODMAN_SYSTEMD_UNIT": info.unit}) };
                        json!({
                            "Id": name, "Name": name, "ImageName": info.image, "Image": info.image_id,
                            "IsInfra": false, "State": {"Status": "running"}, "Config": {"Labels": labels},
                            "Mounts": info.sources.iter().map(|s| json!({"Type": "bind", "Source": s, "Destination": "/data"})).collect::<Vec<_>>(),
                        })
                    })
                    .collect();
                result(args, 0, serde_json::to_string(&records).unwrap(), "")
            }
            "inspect" => result(args, 0, "healthy\n", ""),
            _ => result(args, 1, "", format!("fake podman: {}", args.join(" "))),
        }
    }
}

pub struct FakeHost {
    settings: Settings,
    system: Rc<FakeSystem>,
}

impl Host for FakeHost {
    fn settings(&self) -> &Settings {
        &self.settings
    }

    fn euid(&self) -> u32 {
        crate::sys::geteuid()
    }

    fn exec(
        &self,
        argv: &[String],
        env: &[(String, String)],
        _timeout: Option<Duration>,
        _cwd: Option<&Path>,
    ) -> Output {
        self.system.run(argv, env)
    }

    fn generator(&self) -> Option<PathBuf> {
        Some(PathBuf::from(GENERATOR))
    }

    fn accessible(
        &self,
        _user: &str,
        _paths: &[PathBuf],
        _access: Access,
        _groups: &[String],
    ) -> Option<HashSet<PathBuf>> {
        None
    }

    fn linger(&self) -> bool {
        true
    }

    fn group_members(&self, _gid: u32) -> HashSet<String> {
        HashSet::new()
    }

    fn subordinate_uids(&self) -> Vec<(u32, u32)> {
        Vec::new()
    }
}

/// textwrap.dedent, then without leading blank lines.
pub fn dedent(text: &str) -> String {
    let margin =
        text.lines().filter(|l| !l.trim().is_empty()).map(|l| l.len() - l.trim_start().len()).min().unwrap_or(0);
    let lines: Vec<&str> = text.lines().map(|l| if l.trim().is_empty() { "" } else { &l[margin..] }).collect();
    let mut joined = lines.join("\n");
    if text.ends_with('\n') {
        joined.push('\n');
    }
    joined.trim_start().to_string()
}

/// A temporary /opt/podman, /etc/caddy and a FakeHost wired together.
pub struct Lab {
    pub tmp: tempfile::TempDir,
    pub settings: Settings,
    pub system: Rc<FakeSystem>,
    buf: Rc<RefCell<String>>,
}

impl Lab {
    pub fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("opt/podman");
        std::fs::create_dir_all(&home).unwrap();
        std::fs::set_permissions(&home, std::fs::Permissions::from_mode(0o700)).unwrap();
        let settings = Settings {
            user: "podman".into(),
            uid: unsafe { libc::getuid() },
            gid: unsafe { libc::getgid() },
            root: home.clone(),
            quadlet_dir: home.join(".config/containers/systemd"),
            home,
            quadlet_subdir: "quadlet".into(),
            caddy_subdir: "caddy".into(),
            caddyfile: tmp.path().join("etc/caddy/Caddyfile"),
            caddy_dir: tmp.path().join("etc/caddy/Caddyfile.d"),
            caddy: "caddy".into(),
            caddy_service: "caddy.service".into(),
            settle: 0.0,
            ignore: Default::default(),
            config_path: None,
        };
        let system = Rc::new(FakeSystem::new(&settings));
        Lab { tmp, settings, system, buf: Rc::default() }
    }

    /// A context with empty caches, as the next redh run would see it.
    pub fn ctx(&self) -> Context {
        let host = FakeHost { settings: self.settings.clone(), system: self.system.clone() };
        Context::new(Box::new(host), Printer::buffer(self.buf.clone(), true))
    }

    pub fn path(&self, rel: &str) -> PathBuf {
        if rel.starts_with('/') { PathBuf::from(rel) } else { self.settings.home.join(rel) }
    }

    /// Write a file relative to the podman home (or absolute).
    pub fn write(&self, rel: &str, text: &str) -> PathBuf {
        self.write_mode(rel, text, 0o644)
    }

    pub fn write_mode(&self, rel: &str, text: &str, mode: u32) -> PathBuf {
        let path = self.path(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, dedent(text)).unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(mode)).unwrap();
        path
    }

    pub fn mkdir(&self, rel: &str) -> PathBuf {
        let path = self.path(rel);
        std::fs::create_dir_all(&path).unwrap();
        path
    }

    pub fn output(&self) -> String {
        std::mem::take(&mut *self.buf.borrow_mut()).replace(&self.tmp.path().display().to_string(), "")
    }

    /// What 'redh install' will do, done by hand until it is ported: copy quadlet/ and the
    /// snippet, then let systemd reload.
    pub fn install_by_hand(&self, app: &str) {
        let src = self.settings.app_dir(app);
        let dest = self.settings.install_dir(app);
        for (rel, path) in crate::app::walk_files(&src.join("quadlet")) {
            let target = dest.join(&rel);
            std::fs::create_dir_all(target.parent().unwrap()).unwrap();
            std::fs::copy(&path, &target).unwrap();
        }
        let caddy = src.join("caddy");
        for snippet in crate::app::walk_files(&caddy).into_values() {
            std::fs::create_dir_all(&self.settings.caddy_dir).unwrap();
            let target = self.settings.caddy_dir.join(sites::dest_name(app, &caddy, &snippet));
            std::fs::write(&target, sites::content_for(app, &snippet).unwrap()).unwrap();
            std::fs::set_permissions(&target, std::fs::Permissions::from_mode(0o640)).unwrap();
        }
        self.system.daemon_reload();
    }
}

/// A tidy app: quadlet/ and caddy/, everything as it should be.
pub fn nextcloud(lab: &Lab) {
    let data = lab.mkdir("nextcloud/data");
    lab.write(
        "nextcloud/quadlet/nextcloud.container",
        &format!(
            "
            [Unit]
            Description=Nextcloud

            [Container]
            Image=docker.io/library/nextcloud:30
            AutoUpdate=registry
            Network=nextcloud.network
            Volume={}:/var/www/html:Z
            Volume=nextcloud-db.volume:/db
            PublishPort=127.0.0.1:8080:80
            HealthCmd=curl -f http://localhost/

            [Install]
            WantedBy=default.target
            ",
            data.display()
        ),
    );
    lab.write("nextcloud/quadlet/nextcloud.network", "[Network]\n");
    lab.write("nextcloud/quadlet/nextcloud-db.volume", "[Volume]\n");
    lab.write("nextcloud/caddy/nextcloud.caddy", "cloud.example.org {\n    reverse_proxy 127.0.0.1:8080\n}\n");
    lab.write(
        lab.settings.caddyfile.to_str().unwrap(),
        &format!("{{\n    email admin@example.org\n}}\nimport {}/*.caddy\n", lab.settings.caddy_dir.display()),
    );
}
