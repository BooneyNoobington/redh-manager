//! Running commands: as root, as the podman user, and against its user manager.
//!
//! redh normally runs as root. Everything that has to happen as the podman user —
//! systemctl --user, podman, the Quadlet generator — goes through runuser with that
//! user's XDG_RUNTIME_DIR, so it reaches the user manager that lingering started.
//! Everything goes through `Host::exec`; the tests replace just that.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::Read;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::Duration;

use wait_timeout::ChildExt;

use crate::config::Settings;
use crate::sys;

/// The generator binary moved around between podman versions and distros.
pub const GENERATOR_CANDIDATES: [&str; 4] = [
    "/usr/libexec/podman/quadlet",
    "/usr/lib/podman/quadlet",
    "/usr/lib/systemd/user-generators/podman-user-generator",
    "/usr/lib/systemd/system-generators/podman-system-generator",
];

/// Properties redh reads for a unit's state.
pub const STATE_PROPS: [&str; 10] = [
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "NRestarts",
    "ExecMainStatus",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "Type",
    "RemainAfterExit",
];

pub type State = BTreeMap<String, String>;

pub fn prop<'a>(state: &'a State, key: &str) -> &'a str {
    state.get(key).map(String::as_str).unwrap_or("")
}

/// A oneshot that runs to completion and is inactive again afterwards. Quadlet makes
/// .build and .volume units like that: 'inactive (dead)' after a run is their success.
pub fn is_task(state: &State) -> bool {
    prop(state, "Type") == "oneshot" && prop(state, "RemainAfterExit") == "no"
}

/// A task that has run, and succeeded.
pub fn finished(state: &State) -> bool {
    is_task(state)
        && prop(state, "ActiveState") == "inactive"
        && matches!(state.get("Result").map(String::as_str), None | Some("success"))
        && !prop(state, "InactiveEnterTimestamp").is_empty()
}

#[derive(Clone, Debug)]
pub struct Output {
    pub argv: Vec<String>,
    pub rc: i32,
    pub out: String,
    pub err: String,
}

impl Output {
    pub fn ok(&self) -> bool {
        self.rc == 0
    }

    /// The most telling line of output, for a one-line error report.
    pub fn message(&self) -> String {
        for text in [&self.err, &self.out] {
            if let Some(line) = text.lines().map(str::trim).rfind(|l| !l.is_empty()) {
                return line.to_string();
            }
        }
        format!("exit status {}", self.rc)
    }

    fn failed(argv: &[String], rc: i32, err: String) -> Self {
        Output { argv: argv.to_vec(), rc, out: String::new(), err }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Access {
    Read,
    Write,
    Exec,
}

impl Access {
    fn mode(self) -> libc::c_int {
        match self {
            Access::Read => libc::R_OK,
            Access::Write => libc::W_OK,
            Access::Exec => libc::X_OK,
        }
    }
}

pub fn sv(args: &[&str]) -> Vec<String> {
    args.iter().map(|s| s.to_string()).collect()
}

pub fn secs(seconds: u64) -> Option<Duration> {
    Some(Duration::from_secs(seconds))
}

pub trait Host {
    fn settings(&self) -> &Settings;
    fn euid(&self) -> u32;

    /// The process boundary: every command runs through here.
    fn exec(&self, argv: &[String], env: &[(String, String)], timeout: Option<Duration>, cwd: Option<&Path>) -> Output;

    fn generator(&self) -> Option<PathBuf>;

    /// The paths `user` may open with `access`, as the kernel decides; None when that
    /// cannot be tested (only root can take on another identity).
    fn accessible(&self, user: &str, paths: &[PathBuf], access: Access, groups: &[String]) -> Option<HashSet<PathBuf>>;

    fn linger(&self) -> bool {
        Path::new("/var/lib/systemd/linger").join(&self.settings().user).exists()
    }

    /// Everyone in a group: listed members, and users whose primary group it is.
    fn group_members(&self, gid: u32) -> HashSet<String> {
        let Some((_, members)) = sys::group_by_gid(gid) else { return HashSet::new() };
        let mut all: HashSet<String> = members.into_iter().collect();
        all.extend(sys::all_users().into_iter().filter(|u| u.gid == gid).map(|u| u.name));
        all
    }

    /// (first, count) ranges the podman user's containers map their users to.
    fn subordinate_uids(&self) -> Vec<(u32, u32)> {
        let s = self.settings();
        let Ok(text) = std::fs::read_to_string("/etc/subuid") else { return Vec::new() };
        text.lines()
            .filter_map(|line| {
                let parts: Vec<&str> = line.trim().split(':').collect();
                if parts.len() == 3 && (parts[0] == s.user || parts[0] == s.uid.to_string()) {
                    Some((parts[1].parse().ok()?, parts[2].parse().ok()?))
                } else {
                    None
                }
            })
            .collect()
    }

    /// Hand a file redh created to the podman user (only root has to).
    fn chown(&self, path: &Path) {
        if self.is_root() && !self.is_user() {
            let s = self.settings();
            let _ = std::os::unix::fs::lchown(path, Some(s.uid), Some(s.gid));
        }
    }

    /// Give a file to someone else (only root can).
    fn chown_to(&self, path: &Path, uid: u32, gid: u32) {
        if self.is_root() {
            let _ = std::os::unix::fs::lchown(path, Some(uid), Some(gid));
        }
    }

    // -- built on exec -----------------------------------------------------------

    fn is_root(&self) -> bool {
        self.euid() == 0
    }

    fn is_user(&self) -> bool {
        self.euid() == self.settings().uid
    }

    /// How a person reaches the same user manager from a root shell.
    fn manager_hint(&self) -> String {
        format!("systemctl --user -M {}@", self.settings().user)
    }

    fn user_env(&self) -> Vec<(String, String)> {
        let runtime = self.settings().runtime_dir().display().to_string();
        vec![
            ("XDG_RUNTIME_DIR".into(), runtime.clone()),
            ("DBUS_SESSION_BUS_ADDRESS".into(), format!("unix:path={runtime}/bus")),
        ]
    }

    /// Run a command as ourselves.
    fn run(&self, argv: &[String], timeout: Option<Duration>) -> Output {
        self.exec(argv, &[], timeout, None)
    }

    /// Run a command as the podman user, with its runtime directory.
    fn run_user(&self, argv: &[String], env: &[(String, String)], timeout: Option<Duration>) -> Output {
        let s = self.settings();
        let mut extra = self.user_env();
        extra.extend(env.iter().cloned());
        if self.is_user() {
            return self.exec(argv, &extra, timeout, Some(&s.home));
        }
        let mut full = sv(&["runuser", "-u", &s.user, "--", "env"]);
        full.push(format!("HOME={}", s.home.display()));
        full.extend(extra.iter().map(|(k, v)| format!("{k}={v}")));
        full.extend(argv.iter().cloned());
        self.exec(&full, &[], timeout, Some(&s.home))
    }

    /// Run a command as some other system user (only root can; else as ourselves).
    fn run_as(&self, user: &str, argv: &[String], timeout: Option<Duration>) -> Output {
        let mut full = Vec::new();
        if self.is_root() && user != "root" {
            // runuser keeps root's environment; root's XDG directories would send the
            // program (Caddy: its storage) somewhere it cannot write.
            full = sv(&["runuser", "-u", user, "--", "env"]);
            for var in ["XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"] {
                full.extend(sv(&["-u", var]));
            }
        }
        full.extend(argv.iter().cloned());
        self.exec(&full, &[], timeout, Some(Path::new("/")))
    }

    fn systemctl(&self, args: &[&str], timeout: Option<Duration>) -> Output {
        let mut argv = sv(&["systemctl", "--user"]);
        argv.extend(sv(args));
        self.run_user(&argv, &[], timeout)
    }

    /// systemctl show for several units at once, keyed by the name asked for.
    fn show(&self, units: &[String], props: &[&str]) -> Result<HashMap<String, State>, String> {
        let mut seen = HashSet::new();
        let units: Vec<String> = units.iter().filter(|u| seen.insert(u.as_str())).cloned().collect();
        if units.is_empty() {
            return Ok(HashMap::new());
        }
        let mut wanted = vec!["Id"];
        wanted.extend(props);
        let property = format!("--property={}", wanted.join(","));
        let mut args = vec!["show", property.as_str(), "--"];
        args.extend(units.iter().map(String::as_str));
        let result = self.systemctl(&args, secs(60));
        if !result.ok() && result.out.trim().is_empty() {
            return Err(format!("cannot reach {}'s user manager: {}", self.settings().user, result.message()));
        }
        let blocks: Vec<State> = result
            .out
            .trim_matches('\n')
            .split("\n\n")
            .map(|block| {
                block.lines().filter_map(|l| l.split_once('=')).map(|(k, v)| (k.to_string(), v.to_string())).collect()
            })
            .collect();
        if blocks.len() == units.len() {
            // One block per unit, in order — also right for aliases, whose Id differs.
            return Ok(units.into_iter().zip(blocks).collect());
        }
        // systemctl skipped a name it rejected outright; match by Id instead.
        let by_id: HashMap<String, State> =
            blocks.into_iter().map(|b| (b.get("Id").cloned().unwrap_or_default(), b)).collect();
        Ok(units
            .into_iter()
            .map(|u| {
                let state =
                    by_id.get(&u).cloned().unwrap_or_else(|| State::from([("LoadState".into(), "unknown".into())]));
                (u, state)
            })
            .collect())
    }

    fn state_of(&self, units: &[String]) -> Result<HashMap<String, State>, String> {
        self.show(units, &STATE_PROPS)
    }

    fn daemon_reload(&self) -> Output {
        self.systemctl(&["daemon-reload"], secs(120))
    }

    /// The newest journal lines of a user unit since a point in time.
    fn journal(&self, unit: &str, since: f64, lines: usize) -> Vec<String> {
        let mut argv = sv(&["journalctl", "--no-pager", "--quiet", "--output=short"]);
        argv.push(format!("--lines={lines}"));
        argv.push(format!("--since=@{}", since as i64));
        let result = if self.is_root() && !self.is_user() {
            // 'journalctl --user-unit' would match root's own UID; spell it out.
            let uid = self.settings().uid;
            argv.extend([
                format!("_UID={uid}"),
                format!("_SYSTEMD_USER_UNIT={unit}"),
                "+".into(),
                format!("_UID={uid}"),
                format!("USER_UNIT={unit}"),
            ]);
            self.run(&argv, secs(30))
        } else {
            argv.extend(["--user".into(), format!("--unit={unit}")]);
            self.run_user(&argv, &[], secs(30))
        };
        result.out.lines().filter(|l| !l.trim().is_empty()).map(String::from).collect()
    }

    fn podman(&self, args: &[&str], timeout: Option<Duration>) -> Output {
        let mut argv = sv(&["podman"]);
        argv.extend(sv(args));
        self.run_user(&argv, &[], timeout)
    }

    fn podman_version(&self) -> Option<(u32, u32, u32)> {
        let result = self.podman(&["--version"], secs(30));
        if !result.ok() {
            return None;
        }
        let word = result.out.split_whitespace().find(|w| w.chars().next().is_some_and(|c| c.is_ascii_digit()))?;
        let mut parts = word.split(|c: char| !c.is_ascii_digit()).map(|p| p.parse::<u32>().unwrap_or(0));
        Some((parts.next()?, parts.next()?, parts.next().unwrap_or(0)))
    }

    /// Run the Quadlet generator over exactly these directories, writing nothing.
    fn generator_dryrun(&self, unit_dirs: &[PathBuf]) -> Output {
        let Some(generator) = self.generator() else {
            return Output::failed(&[], 127, "Quadlet generator not found".into());
        };
        let dirs: Vec<String> = unit_dirs.iter().map(|d| d.display().to_string()).collect();
        let argv = vec![generator.display().to_string(), "-user".into(), "-dryrun".into()];
        self.run_user(&argv, &[("QUADLET_UNIT_DIRS".into(), dirs.join(":"))], secs(120))
    }

    /// (user, extra groups) a system service runs as; None if there is no such service.
    fn service_identity(&self, service: &str) -> Option<(String, Vec<String>)> {
        let result =
            self.run(&sv(&["systemctl", "show", service, "--property=LoadState,User,SupplementaryGroups"]), secs(30));
        let props: HashMap<&str, &str> = result.out.lines().filter_map(|l| l.split_once('=')).collect();
        if props.get("LoadState") != Some(&"loaded") {
            return None;
        }
        let user = props.get("User").filter(|u| !u.is_empty()).unwrap_or(&"root").to_string();
        let groups = props
            .get("SupplementaryGroups")
            .map(|g| g.split_whitespace().map(String::from).collect())
            .unwrap_or_default();
        Some((user, groups))
    }

    /// Named user entries of a path's ACL: user -> 'r-x' and the like.
    fn acl_entries(&self, path: &Path) -> HashMap<String, String> {
        let result =
            self.run(&sv(&["getfacl", "--absolute-names", "--omit-header", &path.display().to_string()]), secs(30));
        result
            .out
            .lines()
            .filter_map(|line| {
                let line = line.split('#').next()?.trim();
                let parts: Vec<&str> = line.split(':').collect();
                (parts.len() == 3 && parts[0] == "user" && !parts[1].is_empty())
                    .then(|| (parts[1].to_string(), parts[2].to_string()))
            })
            .collect()
    }
}

/// The real thing.
pub struct RealHost {
    settings: Settings,
    euid: u32,
    generator: OnceLock<Option<PathBuf>>,
}

impl RealHost {
    pub fn new(settings: Settings) -> Self {
        RealHost { settings, euid: sys::geteuid(), generator: OnceLock::new() }
    }
}

fn is_executable(path: &Path) -> bool {
    path.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

impl Host for RealHost {
    fn settings(&self) -> &Settings {
        &self.settings
    }

    fn euid(&self) -> u32 {
        self.euid
    }

    fn exec(&self, argv: &[String], env: &[(String, String)], timeout: Option<Duration>, cwd: Option<&Path>) -> Output {
        let Some((program, args)) = argv.split_first() else {
            return Output::failed(argv, 127, "nothing to run".into());
        };
        let mut command = Command::new(program);
        command.args(args).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
        command.envs(env.iter().map(|(k, v)| (k, v)));
        if let Some(dir) = cwd.filter(|d| d.is_dir()) {
            command.current_dir(dir);
        }
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                return Output::failed(argv, 127, format!("{program}: command not found"));
            }
            Err(e) => return Output::failed(argv, 126, format!("{program}: {e}")),
        };
        // Read both pipes while waiting, so a chatty command cannot fill one and stall.
        let mut out_pipe = child.stdout.take().expect("piped");
        let mut err_pipe = child.stderr.take().expect("piped");
        let out = std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = out_pipe.read_to_end(&mut buf);
            buf
        });
        let err = std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = err_pipe.read_to_end(&mut buf);
            buf
        });
        let status = match timeout {
            Some(limit) => match child.wait_timeout(limit) {
                Ok(Some(status)) => Some(status),
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    None
                }
            },
            None => child.wait().ok(),
        };
        let out = String::from_utf8_lossy(&out.join().unwrap_or_default()).into_owned();
        let err = String::from_utf8_lossy(&err.join().unwrap_or_default()).into_owned();
        match status {
            Some(status) => Output { argv: argv.to_vec(), rc: status.code().unwrap_or(-1), out, err },
            None => Output {
                argv: argv.to_vec(),
                rc: 124,
                out,
                err: format!("{program}: timed out after {}s", timeout.map_or(0, |t| t.as_secs())),
            },
        }
    }

    fn generator(&self) -> Option<PathBuf> {
        self.generator.get_or_init(|| GENERATOR_CANDIDATES.iter().map(PathBuf::from).find(|p| is_executable(p))).clone()
    }

    fn accessible(&self, user: &str, paths: &[PathBuf], access: Access, groups: &[String]) -> Option<HashSet<PathBuf>> {
        if !self.is_root() || user == "root" {
            return None;
        }
        let entry = sys::user_by_name(user)?;
        let mut gids = sys::group_list(user, entry.gid);
        gids.extend(groups.iter().filter_map(|g| sys::gid_of_group(g)));
        gids.sort_unstable();
        gids.dedup();
        sys::as_identity(entry.uid, entry.gid, &gids, || {
            paths.iter().filter(|p| sys::effective_access(p, access.mode())).cloned().collect()
        })
    }
}
