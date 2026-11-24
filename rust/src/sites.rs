//! Caddy snippets, installed into one directory below /etc/caddy like the units.
//!
//!     /opt/podman/<app>/caddy/Caddyfile   ->  /etc/caddy/Caddyfile.d/<app>.caddy
//!     /opt/podman/<app>/caddy/<name>      ->  /etc/caddy/Caddyfile.d/<app>.<name>.caddy
//!
//! The main Caddyfile imports them all with one line. Every installed file begins with
//! a comment naming its source: that is how redh tells its files from anyone else's,
//! and to which app each belongs.

use std::collections::BTreeMap;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use crate::app::{Context, name_of};
use crate::config::Settings;

pub fn header(source: &Path, app: &str) -> String {
    format!("# Installed by redh from {} - edit that file, then: redh install {app}\n", source.display())
}

const HEADER_START: &str = "# Installed by redh from ";

fn parse_header(line: &str) -> Option<PathBuf> {
    let rest = line.strip_prefix(HEADER_START)?;
    let source = rest.split(' ').next()?;
    (!source.is_empty()).then(|| PathBuf::from(source))
}

pub fn app_of(s: &Settings, source: &Path) -> Option<String> {
    let rel = source.strip_prefix(&s.root).ok()?;
    rel.iter().next().map(|p| p.to_string_lossy().into_owned())
}

/// <app>.caddy for a Caddyfile (or <app>.caddy), <app>.<name>.caddy for anything else.
pub fn dest_name(app: &str, base: &Path, snippet: &Path) -> String {
    let rel = snippet.strip_prefix(base).unwrap_or(snippet);
    let mut stem = rel.to_string_lossy().replace('/', ".");
    for suffix in [".caddyfile", ".caddy"] {
        if stem.to_lowercase().ends_with(suffix) {
            stem.truncate(stem.len() - suffix.len());
        }
    }
    let lower = stem.to_lowercase();
    if lower == "caddyfile" || lower.is_empty() || lower == app.to_lowercase() {
        format!("{app}.caddy")
    } else {
        format!("{app}.{stem}.caddy")
    }
}

pub fn content_for(app: &str, source: &Path) -> std::io::Result<Vec<u8>> {
    let mut content = header(source, app).into_bytes();
    content.extend(std::fs::read(source)?);
    Ok(content)
}

/// Every *.caddy file in the directory: name -> (path, source; None if not redh's).
pub fn installed(s: &Settings) -> BTreeMap<String, (PathBuf, Option<PathBuf>)> {
    let mut files = BTreeMap::new();
    let Ok(entries) = std::fs::read_dir(&s.caddy_dir) else { return files };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = name_of(&path);
        if name.starts_with('.') || !name.ends_with(".caddy") || !path.is_file() {
            continue;
        }
        let first = crate::units::read_lossy(&path).ok().and_then(|t| t.lines().next().map(String::from));
        files.insert(name, (path, first.as_deref().and_then(parse_header)));
    }
    files
}

#[derive(Clone, Debug, Default)]
pub struct SitePlan {
    pub app: String,
    /// name -> source
    pub wanted: BTreeMap<String, PathBuf>,
    pub add: Vec<String>,
    pub change: Vec<String>,
    pub remove: Vec<String>,
    pub same: Vec<String>,
    /// name, who has it
    pub conflicts: Vec<(String, String)>,
}

impl SitePlan {
    pub fn in_sync(&self) -> bool {
        self.add.is_empty() && self.change.is_empty() && self.remove.is_empty()
    }

    /// Nothing of it installed yet.
    pub fn fresh(&self) -> bool {
        !self.add.is_empty() && self.same.is_empty() && self.change.is_empty()
    }

    pub fn lines(&self, s: &Settings) -> Vec<String> {
        let at = |name: &String| s.caddy_dir.join(name).display().to_string();
        let mut lines: Vec<String> = self.add.iter().map(|n| format!("+ {}", at(n))).collect();
        lines.extend(self.change.iter().map(|n| format!("~ {}", at(n))));
        lines.extend(self.remove.iter().map(|n| format!("- {}", at(n))));
        lines
    }
}

pub fn plan(ctx: &Context, app: &str) -> Result<SitePlan, String> {
    let s = ctx.settings();
    let src = ctx.sources(app)?;
    let mut result = SitePlan { app: app.to_string(), ..Default::default() };
    if src.exists && !src.snippets.is_empty() {
        let base = if src.snippet_layout == "top level" { src.dir.clone() } else { src.dir.join(&s.caddy_subdir) };
        result.wanted = src.snippets.iter().map(|p| (dest_name(app, &base, p), p.clone())).collect();
    }
    let present = installed(s);
    for (name, source) in &result.wanted {
        match present.get(name) {
            Some((path, owner)) => {
                let theirs = owner.as_ref().and_then(|o| app_of(s, o));
                if theirs.as_deref() != Some(app) {
                    result.conflicts.push((name.clone(), theirs.unwrap_or_else(|| "no redh app".into())));
                    continue;
                }
                let same = std::fs::read(path).ok() == content_for(app, source).ok()
                    && std::fs::metadata(path).map(|m| m.mode() & 0o7777 == 0o640).unwrap_or(false);
                if same { result.same.push(name.clone()) } else { result.change.push(name.clone()) }
            }
            None => result.add.push(name.clone()),
        }
    }
    result.remove = present
        .iter()
        .filter(|(name, (_, owner))| {
            owner.as_ref().and_then(|o| app_of(s, o)).as_deref() == Some(app) && !result.wanted.contains_key(*name)
        })
        .map(|(name, _)| name.clone())
        .collect();
    Ok(result)
}

pub fn import_line(s: &Settings) -> String {
    format!("import {}/*.caddy", s.caddy_dir.display())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dest_names() {
        let b = Path::new("/b");
        assert_eq!(dest_name("blog", b, Path::new("/b/Caddyfile")), "blog.caddy");
        assert_eq!(dest_name("blog", b, Path::new("/b/blog.caddy")), "blog.caddy");
        assert_eq!(dest_name("blog", b, Path::new("/b/admin.caddy")), "blog.admin.caddy");
        assert_eq!(dest_name("blog", b, Path::new("/b/sub/x.Caddyfile")), "blog.sub.x.caddy");
    }

    #[test]
    fn headers() {
        let line = header(Path::new("/opt/podman/blog/caddy/Caddyfile"), "blog");
        assert_eq!(parse_header(line.trim_end()).unwrap(), PathBuf::from("/opt/podman/blog/caddy/Caddyfile"));
        assert!(parse_header("# mine, by hand").is_none());
    }
}
