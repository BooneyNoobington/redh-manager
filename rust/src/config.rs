//! Settings: an optional INI file plus defaults that match the usual layout.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use crate::sys;

pub const DEFAULT_CONFIG: &str = "/etc/redh-manager.conf";
const SECTION: &str = "redh";
const KEYS: [&str; 11] = [
    "user",
    "root",
    "quadlet_dir",
    "quadlet_subdir",
    "caddy_subdir",
    "caddyfile",
    "caddy_dir",
    "caddy",
    "caddy_service",
    "settle",
    "ignore",
];

#[derive(Clone, Debug)]
pub struct Settings {
    pub user: String,
    pub uid: u32,
    pub gid: u32,
    pub home: PathBuf,
    /// Where the apps live: <root>/<app>.
    pub root: PathBuf,
    /// The podman user's Quadlet directory.
    pub quadlet_dir: PathBuf,
    pub quadlet_subdir: String,
    pub caddy_subdir: String,
    pub caddyfile: PathBuf,
    /// Where the apps' snippets get installed.
    pub caddy_dir: PathBuf,
    pub caddy: String,
    pub caddy_service: String,
    /// Seconds to watch units after starting them.
    pub settle: f64,
    pub ignore: BTreeSet<String>,
    pub config_path: Option<PathBuf>,
}

impl Settings {
    pub fn runtime_dir(&self) -> PathBuf {
        PathBuf::from(format!("/run/user/{}", self.uid))
    }

    pub fn app_dir(&self, app: &str) -> PathBuf {
        self.root.join(app)
    }

    pub fn install_dir(&self, app: &str) -> PathBuf {
        self.quadlet_dir.join(app)
    }
}

/// An app is a directory name; nothing that could climb out of the apps root.
pub fn is_app_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
}

type Ini = BTreeMap<String, BTreeMap<String, String>>;

/// The INI dialect of Python's configparser, as far as a settings file needs it:
/// [sections], `key = value` or `key: value`, lower-cased keys, comment lines and
/// inline comments after whitespace, indented continuation lines.
fn parse_ini(text: &str) -> Result<Ini, String> {
    let mut ini = Ini::new();
    let mut section: Option<String> = None;
    let mut last_key: Option<String> = None;
    for (number, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if raw.starts_with(char::is_whitespace)
            && let (Some(sec), Some(key)) = (&section, &last_key)
        {
            let value = ini.get_mut(sec).and_then(|s| s.get_mut(key)).expect("key exists");
            value.push('\n');
            value.push_str(line);
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            let name = line[1..line.len() - 1].trim().to_string();
            if ini.contains_key(&name) {
                return Err(format!("section {name:?} appears twice (line {})", number + 1));
            }
            ini.insert(name.clone(), BTreeMap::new());
            section = Some(name);
            last_key = None;
            continue;
        }
        let Some(sec) = &section else {
            return Err(format!("line {}: a key before any [section]", number + 1));
        };
        let Some(split) = line.find(['=', ':']) else {
            return Err(format!("line {}: not 'key = value': {line}", number + 1));
        };
        let key = line[..split].trim().to_lowercase();
        let mut value = line[split + 1..].trim().to_string();
        // Inline comments only count after whitespace, like configparser's.
        for marker in [" #", " ;", "\t#", "\t;"] {
            if let Some(at) = value.find(marker) {
                value.truncate(at);
            }
        }
        let value = value.trim().to_string();
        let entries = ini.get_mut(sec).expect("section exists");
        if entries.contains_key(&key) {
            return Err(format!("key {key:?} appears twice in [{sec}]"));
        }
        entries.insert(key.clone(), value);
        last_key = Some(key);
    }
    Ok(ini)
}

pub fn load(path: Option<&Path>) -> Result<Settings, String> {
    let explicit = path.is_some();
    let path: PathBuf = match path {
        Some(p) => p.to_path_buf(),
        None => std::env::var_os("REDH_CONFIG").map(PathBuf::from).unwrap_or_else(|| DEFAULT_CONFIG.into()),
    };
    let shown = path.display().to_string();
    let (ini, config_path) = if path.exists() {
        let text = std::fs::read_to_string(&path).map_err(|e| format!("{shown}: {e}"))?;
        (parse_ini(&text).map_err(|e| format!("{shown}: {e}"))?, Some(path.clone()))
    } else if explicit {
        return Err(format!("config file not found: {shown}"));
    } else {
        (Ini::new(), None)
    };

    let extra: Vec<&String> = ini.keys().filter(|s| s.as_str() != SECTION).collect();
    if !extra.is_empty() {
        let names: Vec<&str> = extra.iter().map(|s| s.as_str()).collect();
        return Err(format!("{shown}: unknown section(s) {}; everything goes under [{SECTION}].", names.join(", ")));
    }
    let empty = BTreeMap::new();
    let section = ini.get(SECTION).unwrap_or(&empty);
    let unknown: Vec<&str> = section.keys().map(String::as_str).filter(|k| !KEYS.contains(k)).collect();
    if !unknown.is_empty() {
        return Err(format!("{shown}: unknown key(s) {}; known keys: {}.", unknown.join(", "), KEYS.join(", ")));
    }
    let get = |key: &str, default: &str| section.get(key).cloned().unwrap_or_else(|| default.to_string());

    let user = get("user", "podman");
    let Some(entry) = sys::user_by_name(&user) else {
        return Err(match &config_path {
            Some(_) => format!("user {user:?} does not exist (user= in {shown})"),
            None => format!("user {user:?} does not exist; set user= in {DEFAULT_CONFIG}"),
        });
    };
    let home = entry.dir.trim_end_matches('/').to_string();
    let home = if home.is_empty() { "/".to_string() } else { home };

    let settle: f64 =
        get("settle", "5").parse().map_err(|_| format!("{shown}: settle= must be a number of seconds."))?;
    let ignore = get("ignore", "")
        .split(|c: char| c == ',' || c.is_whitespace())
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect();

    let absolute = |key: &str, default: &str| -> Result<PathBuf, String> {
        let mut value = get(key, default);
        if let Some(rest) = value.strip_prefix("~/") {
            value = format!("{}/{rest}", std::env::var("HOME").unwrap_or_default());
        }
        let value = value.trim_end_matches('/').to_string();
        let value = if value.is_empty() { "/".to_string() } else { value };
        if !value.starts_with('/') {
            return Err(format!("{shown}: {key}= must be an absolute path, not {value:?}."));
        }
        Ok(PathBuf::from(value))
    };
    let plain = |key: &str, default: &str| -> Result<String, String> {
        let value = get(key, default).trim_matches('/').to_string();
        if value.is_empty() || value.contains('/') || value.starts_with('.') {
            return Err(format!("{shown}: {key}= must be a single directory name."));
        }
        Ok(value)
    };

    Ok(Settings {
        uid: entry.uid,
        gid: entry.gid,
        root: absolute("root", &home)?,
        quadlet_dir: absolute("quadlet_dir", &format!("{home}/.config/containers/systemd"))?,
        quadlet_subdir: plain("quadlet_subdir", "quadlet")?,
        caddy_subdir: plain("caddy_subdir", "caddy")?,
        caddyfile: absolute("caddyfile", "/etc/caddy/Caddyfile")?,
        caddy_dir: absolute("caddy_dir", "/etc/caddy/Caddyfile.d")?,
        caddy: get("caddy", "caddy"),
        caddy_service: get("caddy_service", "caddy.service"),
        settle: settle.max(0.0),
        ignore,
        config_path,
        home: PathBuf::from(home),
        user,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ini_like_configparser() {
        let ini =
            parse_ini("# comment\n[redh]\nUser = podman  # who\nroot: /opt/podman\nignore = a,\n  b c\n").unwrap();
        let redh = &ini["redh"];
        assert_eq!(redh["user"], "podman");
        assert_eq!(redh["root"], "/opt/podman");
        assert_eq!(redh["ignore"], "a,\nb c");
        assert!(parse_ini("[redh]\nuser = a\nuser = b\n").is_err());
        assert!(parse_ini("user = a\n").is_err());
    }

    #[test]
    fn app_names() {
        assert!(is_app_name("paperless-ngx"));
        assert!(is_app_name("RedhDB2"));
        assert!(!is_app_name(".config"));
        assert!(!is_app_name("../etc"));
        assert!(!is_app_name(""));
    }
}
