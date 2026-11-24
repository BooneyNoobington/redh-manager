//! What installing an app would change, worked out without changing anything.
//!
//! Quadlet loads each file *name* only once — the first one it finds, and files at the
//! top level are found before any subdirectory — so a copy of one of the app's files
//! elsewhere either shadows the installed one (an old hand-made install: removed by
//! 'redh install') or belongs to another app (a conflict: one has to be renamed).

use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::rc::Rc;

use crate::app::{Context, Files, Installed, Sources, name_part, unit_relpaths};
use crate::host::Host;
use crate::units::{is_global_dropin_dir, quadlet_ext};

pub fn same_content(a: &Path, b: &Path) -> bool {
    match (std::fs::read(a), std::fs::read(b)) {
        (Ok(x), Ok(y)) => x == y,
        _ => false,
    }
}

/// Installed copies keep the source's permissions, and the owner can always read them.
pub fn dest_mode(source_mode: u32) -> u32 {
    (source_mode & 0o777) | 0o600
}

pub fn installed_matches(host: &dyn Host, source: &Path, dest: &Path) -> bool {
    let (Ok(src), Ok(dst)) = (std::fs::metadata(source), std::fs::symlink_metadata(dest)) else {
        return false;
    };
    if !dst.is_file() || dst.mode() & 0o7777 != dest_mode(src.mode()) {
        return false;
    }
    if host.is_root() && !host.is_user() && dst.uid() != host.settings().uid {
        return false;
    }
    same_content(source, dest)
}

#[derive(Clone, Debug)]
pub struct Plan {
    pub app: String,
    pub src: Rc<Sources>,
    pub inst: Rc<Installed>,
    pub add: Vec<String>,
    pub change: Vec<String>,
    pub remove: Vec<String>,
    pub same: Vec<String>,
    /// old copy, its source, identical?
    pub legacy: Vec<(PathBuf, PathBuf, bool)>,
    /// path, owning app
    pub conflicts: Vec<(PathBuf, String)>,
}

impl Plan {
    pub fn in_sync(&self) -> bool {
        self.add.is_empty() && self.change.is_empty() && self.remove.is_empty() && self.legacy.is_empty()
    }

    pub fn lines(&self) -> Vec<String> {
        let mut lines: Vec<String> = self.add.iter().map(|r| format!("+ {r}")).collect();
        lines.extend(self.change.iter().map(|r| format!("~ {r}")));
        lines.extend(self.remove.iter().map(|r| format!("- {r}")));
        lines.extend(self.legacy.iter().map(|(path, _, same)| {
            format!("x {}  (old copy{})", path.display(), if *same { "" } else { ", differs from the source" })
        }));
        lines
    }

    /// Unit files whose definition changes, and whether a change reaches every unit.
    pub fn touched(&self) -> (BTreeSet<String>, bool) {
        let mut names = BTreeSet::new();
        let mut everything = false;
        let legacy_names: Vec<String> = self.legacy.iter().map(|(p, _, _)| crate::app::name_of(p)).collect();
        for rel in self.add.iter().chain(&self.change).chain(&self.remove).chain(&legacy_names) {
            let parts: Vec<&str> = rel.split('/').collect();
            if parts.len() > 1 && parts[parts.len() - 2].ends_with(".d") {
                let dir = parts[parts.len() - 2];
                let target = &dir[..dir.len() - 2];
                let stem = target.rsplit_once('.').map_or(target, |(s, _)| s);
                if is_global_dropin_dir(dir) || stem.ends_with('-') {
                    everything = true;
                } else {
                    names.insert(target.to_string());
                }
            } else if quadlet_ext(parts[parts.len() - 1]).is_some() {
                names.insert(parts[parts.len() - 1].to_string());
            } else {
                everything = true;
            }
        }
        (names, everything)
    }
}

/// Files with the same name as one of the app's, outside its install directory.
pub type Legacy = Vec<(PathBuf, PathBuf, bool)>;
pub type Conflicts = Vec<(PathBuf, String)>;

pub fn copies_elsewhere(ctx: &Context, app: &str, files: &Files) -> (Legacy, Conflicts) {
    let units: BTreeMap<String, PathBuf> =
        unit_relpaths(files).into_iter().map(|rel| (name_part(&rel).to_string(), files[&rel].clone())).collect();
    let mut dropins: BTreeMap<String, PathBuf> = BTreeMap::new();
    for (rel, path) in files {
        let parts: Vec<&str> = rel.split('/').collect();
        if parts.len() >= 2 && parts[parts.len() - 2].ends_with(".d") && !is_global_dropin_dir(parts[parts.len() - 2]) {
            dropins.insert(format!("{}/{}", parts[parts.len() - 2], parts[parts.len() - 1]), path.clone());
        }
    }
    let mut legacy = Legacy::new();
    let mut conflicts = Conflicts::new();
    for (rel, path) in ctx.tree().iter() {
        let parts: Vec<&str> = rel.split('/').collect();
        if parts.len() > 1 && parts[0] == app {
            continue;
        }
        let source = if parts.len() > 1 && parts[parts.len() - 2].ends_with(".d") {
            dropins.get(&format!("{}/{}", parts[parts.len() - 2], parts[parts.len() - 1]))
        } else {
            units.get(parts[parts.len() - 1])
        };
        let Some(source) = source else { continue };
        if parts.len() > 1 && !parts[0].ends_with(".d") {
            conflicts.push((path.clone(), parts[0].to_string()));
        } else {
            legacy.push((path.clone(), source.clone(), same_content(source, path)));
        }
    }
    (legacy, conflicts)
}

pub fn make_plan(ctx: &Context, app: &str) -> Result<Plan, String> {
    let src = ctx.sources(app)?;
    let inst = ctx.installed(app);
    let mut plan = Plan {
        app: app.to_string(),
        src: src.clone(),
        inst: inst.clone(),
        add: Vec::new(),
        change: Vec::new(),
        remove: Vec::new(),
        same: Vec::new(),
        legacy: Vec::new(),
        conflicts: Vec::new(),
    };
    for (rel, source) in &src.files {
        match inst.files.get(rel) {
            None => plan.add.push(rel.clone()),
            Some(dest) if installed_matches(ctx.host.as_ref(), source, dest) => plan.same.push(rel.clone()),
            Some(_) => plan.change.push(rel.clone()),
        }
    }
    plan.remove = inst.files.keys().filter(|rel| !src.files.contains_key(*rel)).cloned().collect();
    let files = if src.files.is_empty() { &inst.files } else { &src.files };
    let (legacy, conflicts) = copies_elsewhere(ctx, app, files);
    plan.legacy = legacy;
    plan.conflicts = conflicts;
    Ok(plan)
}
