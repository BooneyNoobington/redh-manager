//! Where an app's files are: its sources under the apps root, its installed copy.
//!
//!     /opt/podman/<app>/quadlet/   Quadlet units, drop-ins and files they use
//!     /opt/podman/<app>/caddy/     the Caddy snippet(s)
//!
//! Installing copies quadlet/ to <Quadlet dir>/<app>/. Apps that still keep their
//! unit files at the top level of /opt/podman/<app> are understood too; then only
//! the unit files and their drop-in directories are installed.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::rc::Rc;

use crate::config::{Settings, is_app_name};
use crate::host::Host;
use crate::report::Printer;
use crate::units::{PLAIN_UNIT_EXTS, QuadletUnit, extension, is_dropin_dir, quadlet_ext};

/// relative path ('sub/a.container') -> full path
pub type Files = BTreeMap<String, PathBuf>;

/// Dotfiles, editor backups and redh's own temporary files.
pub fn junk(name: &str) -> bool {
    name.starts_with('.') || name.ends_with('~')
}

pub fn name_of(path: &Path) -> String {
    path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Every regular file below base, keyed by its path relative to base. Like os.walk:
/// symlinked directories are not followed, symlinked files are taken.
pub fn walk_files(base: &Path) -> Files {
    fn walk(dir: &Path, rel: &str, files: &mut Files) {
        let Ok(entries) = std::fs::read_dir(dir) else { return };
        let mut entries: Vec<_> = entries.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let name = entry.file_name().to_string_lossy().into_owned();
            if junk(&name) {
                continue;
            }
            let path = entry.path();
            let child = if rel.is_empty() { name.clone() } else { format!("{rel}/{name}") };
            let is_real_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
            if is_real_dir {
                walk(&path, &child, files);
            } else if path.is_file() {
                files.insert(child, path);
            }
        }
    }
    let mut files = Files::new();
    walk(base, "", &mut files);
    files
}

fn last_part(rel: &str) -> &str {
    rel.rsplit('/').next().unwrap_or(rel)
}

fn parent_parts(rel: &str) -> Vec<&str> {
    let mut parts: Vec<&str> = rel.split('/').collect();
    parts.pop();
    parts
}

/// The Quadlet unit files in a file set, leaving out drop-in directories.
pub fn unit_relpaths(files: &Files) -> Vec<String> {
    files
        .keys()
        .filter(|rel| quadlet_ext(last_part(rel)).is_some() && !parent_parts(rel).iter().any(|p| p.ends_with(".d")))
        .cloned()
        .collect()
}

pub fn load_units(base: &Path, files: &Files) -> (Vec<QuadletUnit>, Vec<(String, String)>) {
    let mut units = Vec::new();
    let mut unreadable = Vec::new();
    for rel in unit_relpaths(files) {
        let mut search = vec![base.to_path_buf()];
        let subdir = base.join(&rel).parent().map(Path::to_path_buf).unwrap_or_default();
        if subdir != base {
            search.push(subdir);
        }
        match QuadletUnit::load(&files[&rel], &search) {
            Ok(unit) => units.push(unit),
            Err(e) => unreadable.push((rel, e.to_string())),
        }
    }
    units.sort_by_key(QuadletUnit::sort_key);
    (units, unreadable)
}

pub fn is_snippet_name(name: &str) -> bool {
    let lower = name.to_lowercase();
    lower == "caddyfile" || lower.ends_with(".caddy") || lower.ends_with(".caddyfile")
}

#[derive(Clone, Debug, Default)]
pub struct Sources {
    pub app: String,
    pub dir: PathBuf,
    pub exists: bool,
    /// "quadlet/", "top level" or "none"
    pub layout: String,
    /// The directory that gets installed.
    pub base: Option<PathBuf>,
    pub files: Files,
    pub units: Vec<QuadletUnit>,
    pub unreadable: Vec<(String, String)>,
    pub plain_units: Vec<String>,
    /// Unit files outside quadlet/.
    pub stray: Vec<PathBuf>,
    pub snippets: Vec<PathBuf>,
    pub snippet_layout: String,
    pub stray_snippets: Vec<PathBuf>,
}

#[derive(Clone, Debug, Default)]
pub struct Installed {
    pub app: String,
    pub dir: PathBuf,
    pub exists: bool,
    pub files: Files,
    pub units: Vec<QuadletUnit>,
    pub unreadable: Vec<(String, String)>,
}

impl Installed {
    pub fn services(&self) -> Vec<String> {
        self.units.iter().filter_map(QuadletUnit::startable).collect()
    }
}

pub fn scan_sources(s: &Settings, app: &str) -> Result<Sources, String> {
    let mut src = Sources {
        app: app.to_string(),
        dir: s.app_dir(app),
        layout: "none".into(),
        snippet_layout: "none".into(),
        ..Default::default()
    };
    if !src.dir.is_dir() {
        return Ok(src);
    }
    src.exists = true;
    let mut top: Vec<String> = std::fs::read_dir(&src.dir)
        .map_err(|e| format!("cannot read {}: {e}", src.dir.display()))?
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    top.sort();
    let at_top = |name: &str| src.dir.join(name);

    let top_units: Vec<&String> = top.iter().filter(|n| quadlet_ext(n).is_some() && at_top(n).is_file()).collect();
    let top_dropins: Vec<&String> = top.iter().filter(|n| is_dropin_dir(n) && at_top(n).is_dir()).collect();

    let quadlet_dir = at_top(&s.quadlet_subdir);
    if quadlet_dir.is_dir() {
        src.layout = format!("{}/", s.quadlet_subdir);
        src.files = walk_files(&quadlet_dir);
        src.stray = top_units.iter().chain(top_dropins.iter()).map(|n| at_top(n)).collect();
        src.base = Some(quadlet_dir);
    } else if !top_units.is_empty() {
        src.layout = "top level".into();
        src.base = Some(src.dir.clone());
        for name in &top_units {
            src.files.insert(name.to_string(), at_top(name));
        }
        for name in &top_dropins {
            for (rel, path) in walk_files(&at_top(name)) {
                src.files.insert(format!("{name}/{rel}"), path);
            }
        }
    }

    if let Some(base) = src.base.clone() {
        let (units, unreadable) = load_units(&base, &src.files);
        src.units = units;
        src.unreadable = unreadable;
        src.plain_units = src.files.keys().filter(|rel| PLAIN_UNIT_EXTS.contains(&extension(rel))).cloned().collect();
    }

    let caddy_dir = at_top(&s.caddy_subdir);
    let top_snippets: Vec<PathBuf> =
        top.iter().filter(|n| is_snippet_name(n) && at_top(n).is_file()).map(|n| at_top(n)).collect();
    if caddy_dir.is_dir() {
        src.snippet_layout = format!("{}/", s.caddy_subdir);
        src.snippets = walk_files(&caddy_dir).into_values().collect();
        src.snippets.sort();
        src.stray_snippets = top_snippets;
    } else if !top_snippets.is_empty() {
        src.snippet_layout = "top level".into();
        src.snippets = top_snippets;
    }
    Ok(src)
}

pub fn scan_installed(s: &Settings, app: &str) -> Installed {
    let mut inst = Installed { app: app.to_string(), dir: s.install_dir(app), ..Default::default() };
    if inst.dir.is_dir() {
        inst.exists = true;
        inst.files = walk_files(&inst.dir);
        let (units, unreadable) = load_units(&inst.dir, &inst.files);
        inst.units = units;
        inst.unreadable = unreadable;
    }
    inst
}

/// Settings, host, printer, and a cache of what has been scanned so far.
pub struct Context {
    pub host: Box<dyn Host>,
    pub out: Printer,
    sources: RefCell<HashMap<String, Rc<Sources>>>,
    installed: RefCell<HashMap<String, Rc<Installed>>>,
    tree: RefCell<Option<Rc<Files>>>,
}

impl Context {
    pub fn new(host: Box<dyn Host>, out: Printer) -> Self {
        Context { host, out, sources: RefCell::default(), installed: RefCell::default(), tree: RefCell::default() }
    }

    pub fn settings(&self) -> &Settings {
        self.host.settings()
    }

    /// Drop cached scans after files changed on disk.
    pub fn forget(&self) {
        self.sources.borrow_mut().clear();
        self.installed.borrow_mut().clear();
        *self.tree.borrow_mut() = None;
    }

    /// Directories under the apps root that are apps.
    pub fn apps(&self) -> Result<Vec<String>, String> {
        let s = self.settings();
        let entries = std::fs::read_dir(&s.root).map_err(|e| format!("cannot read {}: {e}", s.root.display()))?;
        let quadlet_top = s
            .quadlet_dir
            .strip_prefix(&s.root)
            .ok()
            .and_then(|p| p.iter().next())
            .map(|p| p.to_string_lossy().into_owned());
        let mut names: Vec<String> = entries
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| {
                !n.starts_with('.')
                    && !s.ignore.contains(n)
                    && Some(n) != quadlet_top.as_ref()
                    && is_app_name(n)
                    && s.root.join(n).is_dir()
            })
            .collect();
        names.sort();
        Ok(names)
    }

    /// Subdirectories of the Quadlet directory: one per installed app.
    pub fn installed_apps(&self) -> Vec<String> {
        let s = self.settings();
        let Ok(entries) = std::fs::read_dir(&s.quadlet_dir) else { return Vec::new() };
        let mut names: Vec<String> = entries
            .flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| !junk(n) && !n.ends_with(".d") && is_app_name(n) && s.quadlet_dir.join(n).is_dir())
            .collect();
        names.sort();
        names
    }

    pub fn known_apps(&self) -> Result<Vec<String>, String> {
        let mut all = self.apps()?;
        all.extend(self.installed_apps());
        all.sort();
        all.dedup();
        Ok(all)
    }

    pub fn sources(&self, app: &str) -> Result<Rc<Sources>, String> {
        if let Some(src) = self.sources.borrow().get(app) {
            return Ok(src.clone());
        }
        let src = Rc::new(scan_sources(self.settings(), app)?);
        self.sources.borrow_mut().insert(app.to_string(), src.clone());
        Ok(src)
    }

    pub fn installed(&self, app: &str) -> Rc<Installed> {
        if let Some(inst) = self.installed.borrow().get(app) {
            return inst.clone();
        }
        let inst = Rc::new(scan_installed(self.settings(), app));
        self.installed.borrow_mut().insert(app.to_string(), inst.clone());
        inst
    }

    /// The app's units: from the sources if it has any, else from the installed copy.
    pub fn units(&self, app: &str) -> Result<Vec<QuadletUnit>, String> {
        let src = self.sources(app)?;
        Ok(if src.exists && !src.units.is_empty() { src.units.clone() } else { self.installed(app).units.clone() })
    }

    /// Every file below the Quadlet directory, keyed by its relative path.
    pub fn tree(&self) -> Rc<Files> {
        if let Some(tree) = self.tree.borrow().as_ref() {
            return tree.clone();
        }
        let qdir = &self.settings().quadlet_dir;
        let tree = Rc::new(if qdir.is_dir() { walk_files(qdir) } else { Files::new() });
        *self.tree.borrow_mut() = Some(tree.clone());
        tree
    }
}

pub fn name_part(rel: &str) -> &str {
    last_part(rel)
}
