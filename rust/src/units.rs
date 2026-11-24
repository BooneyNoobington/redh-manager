//! Reading Quadlet unit files.
//!
//! Only as much of systemd's syntax as redh needs: sections, repeated keys, line
//! continuations, comments and drop-in merging. The naming rules follow Quadlet's own
//! generator (pkg/systemd/quadlet in the podman sources).

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// extension -> (section, suffix of the generated service, key naming the podman object)
const TYPES: [(&str, &str, &str, Option<&str>); 8] = [
    (".container", "Container", "", Some("ContainerName")),
    (".pod", "Pod", "-pod", Some("PodName")),
    (".kube", "Kube", "", None),
    (".network", "Network", "-network", Some("NetworkName")),
    (".volume", "Volume", "-volume", Some("VolumeName")),
    (".image", "Image", "-image", None),
    (".build", "Build", "-build", None),
    (".artifact", "Artifact", "-artifact", None),
];

/// Display order: what actually runs first, the plumbing after it.
pub const KIND_ORDER: [&str; 8] = ["pod", "container", "kube", "network", "volume", "image", "build", "artifact"];

/// Ordinary systemd units. Quadlet ignores them in its directory.
pub const PLAIN_UNIT_EXTS: [&str; 7] = [".service", ".timer", ".socket", ".target", ".path", ".mount", ".slice"];

/// Targets that exist for the system manager but not for a user manager.
pub const SYSTEM_ONLY_TARGETS: [&str; 2] = ["multi-user.target", "graphical.target"];

fn type_of(ext: &str) -> Option<&'static (&'static str, &'static str, &'static str, Option<&'static str>)> {
    TYPES.iter().find(|t| t.0 == ext)
}

/// The extension of a file name: '.container' for 'a.b.container'.
pub fn extension(name: &str) -> &str {
    match name.rfind('.') {
        Some(at) if at > 0 => &name[at..],
        _ => "",
    }
}

pub fn quadlet_ext(name: &str) -> Option<&str> {
    let ext = extension(name);
    type_of(ext).map(|_| ext)
}

/// foo.container.d, foo-.container.d or the global container.d.
pub fn is_dropin_dir(name: &str) -> bool {
    let Some(inner) = name.strip_suffix(".d") else { return false };
    quadlet_ext(inner).is_some() || type_of(&format!(".{inner}")).is_some()
}

/// container.d and friends apply to every unit of that type, in every app.
pub fn is_global_dropin_dir(name: &str) -> bool {
    name.strip_suffix(".d").is_some_and(|inner| type_of(&format!(".{inner}")).is_some())
}

/// Shell-like words: whitespace separated, single and double quotes, backslashes.
pub fn split_words(value: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut in_word = false;
    let mut chars = value.chars();
    while let Some(c) = chars.next() {
        match c {
            '\'' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(c) => word.push(c),
                        None => return value.split_whitespace().map(String::from).collect(),
                    }
                }
            }
            '"' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') => match chars.next() {
                            Some(c) => word.push(c),
                            None => return value.split_whitespace().map(String::from).collect(),
                        },
                        Some(c) => word.push(c),
                        None => return value.split_whitespace().map(String::from).collect(),
                    }
                }
            }
            '\\' => {
                in_word = true;
                if let Some(c) = chars.next() {
                    word.push(c);
                }
            }
            c if c.is_whitespace() => {
                if in_word {
                    words.push(std::mem::take(&mut word));
                    in_word = false;
                }
            }
            c => {
                in_word = true;
                word.push(c);
            }
        }
    }
    if in_word {
        words.push(word);
    }
    words
}

/// systemd-style key/value file: ordered, repeatable keys per section.
#[derive(Clone, Debug, Default)]
pub struct IniFile {
    pub sections: BTreeMap<String, Vec<(String, String)>>,
}

impl IniFile {
    pub fn parse(text: &str) -> Self {
        let mut ini = IniFile::default();
        ini.update(text);
        ini
    }

    /// Merge more text in, the way a drop-in is merged.
    pub fn update(&mut self, text: &str) {
        let mut section: Option<String> = None;
        let mut pending: Option<String> = None;
        for raw in text.lines().chain(std::iter::once("")) {
            let mut line = raw.trim().to_string();
            if let Some(start) = pending.take() {
                if line.starts_with('#') || line.starts_with(';') {
                    pending = Some(start); // comments inside a continuation are dropped
                    continue;
                }
                if let Some(more) = line.strip_suffix('\\') {
                    pending = Some(format!("{start} {}", more.trim()));
                    continue;
                }
                line = format!("{start} {line}").trim().to_string();
            } else if let Some(start) = line.strip_suffix('\\') {
                pending = Some(start.trim().to_string());
                continue;
            }
            if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
                continue;
            }
            if line.starts_with('[') && line.ends_with(']') {
                let name = line[1..line.len() - 1].trim().to_string();
                self.sections.entry(name.clone()).or_default();
                section = Some(name);
            } else if let (Some(name), Some((key, value))) = (&section, line.split_once('=')) {
                self.sections
                    .get_mut(name)
                    .expect("section exists")
                    .push((key.trim().to_string(), value.trim().to_string()));
            }
        }
    }

    pub fn has(&self, section: &str) -> bool {
        self.sections.contains_key(section)
    }

    /// Last value of key, surrounding double quotes removed; None if unset.
    pub fn get(&self, section: &str, key: &str) -> Option<String> {
        let value = self.sections.get(section)?.iter().rev().find(|(k, _)| k == key)?.1.clone();
        if value.len() >= 2 && value.starts_with('"') && value.ends_with('"') {
            return Some(value[1..value.len() - 1].to_string());
        }
        Some(value)
    }

    /// Every value of key in order; an empty assignment resets the list.
    pub fn get_all(&self, section: &str, key: &str) -> Vec<String> {
        let mut result = Vec::new();
        for (k, v) in self.sections.get(section).into_iter().flatten() {
            if k == key {
                if v.is_empty() {
                    result.clear();
                } else {
                    result.push(v.clone());
                }
            }
        }
        result
    }

    pub fn get_words(&self, section: &str, key: &str) -> Vec<String> {
        self.get_all(section, key).iter().flat_map(|v| split_words(v)).collect()
    }
}

/// Drop-in directory names Quadlet consults for a unit, least specific first.
pub fn dropin_dirs(filename: &str) -> Vec<String> {
    let ext = extension(filename);
    let stem = &filename[..filename.len() - ext.len()];
    let mut names = vec![format!("{}.d", &ext[1..])];
    let base = stem.split('@').next().unwrap_or(stem);
    let parts: Vec<&str> = base.split('-').collect();
    for i in 1..parts.len() {
        names.push(format!("{}-{ext}.d", parts[..i].join("-")));
    }
    if stem.contains('@') && !stem.ends_with('@') {
        names.push(format!("{base}@{ext}.d"));
    }
    names.push(format!("{filename}.d"));
    names
}

/// One .container/.network/... file, merged with its drop-ins.
#[derive(Clone, Debug)]
pub struct QuadletUnit {
    pub filename: String,
    pub path: PathBuf,
    pub ini: IniFile,
    pub dropins: Vec<PathBuf>,
    pub ext: String,
    pub kind: String,
    pub stem: String,
    pub section: String,
}

impl QuadletUnit {
    pub fn new(filename: &str, path: &Path, ini: IniFile, dropins: Vec<PathBuf>) -> Self {
        let ext = extension(filename).to_string();
        let section = type_of(&ext).map(|t| t.1).unwrap_or("").to_string();
        QuadletUnit {
            filename: filename.to_string(),
            path: path.to_path_buf(),
            ini,
            dropins,
            kind: ext.trim_start_matches('.').to_string(),
            stem: filename[..filename.len() - ext.len()].to_string(),
            ext,
            section,
        }
    }

    /// Read path and merge the drop-ins found below any of dropin_search.
    pub fn load(path: &Path, dropin_search: &[PathBuf]) -> std::io::Result<Self> {
        let filename = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let mut ini = IniFile::parse(&read_lossy(path)?);
        let mut found: BTreeMap<String, PathBuf> = BTreeMap::new();
        for name in dropin_dirs(&filename) {
            // More specific directories come later and win.
            for base in dropin_search {
                let Ok(entries) = std::fs::read_dir(base.join(&name)) else { continue };
                let mut entries: Vec<_> = entries.flatten().map(|e| e.path()).collect();
                entries.sort();
                for entry in entries {
                    let entry_name = entry.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
                    if entry_name.ends_with(".conf") && entry.is_file() {
                        found.insert(entry_name, entry);
                    }
                }
            }
        }
        let dropins: Vec<PathBuf> = found.into_values().collect();
        for dropin in &dropins {
            ini.update(&read_lossy(dropin)?);
        }
        Ok(QuadletUnit::new(&filename, path, ini, dropins))
    }

    pub fn is_template(&self) -> bool {
        self.stem.ends_with('@')
    }

    pub fn service_base(&self) -> String {
        if let Some(name) = self.get("ServiceName").filter(|n| !n.is_empty()) {
            return name;
        }
        let suffix = type_of(&self.ext).map(|t| t.2).unwrap_or("");
        if self.is_template() {
            return format!("{}{suffix}@", &self.stem[..self.stem.len() - 1]);
        }
        format!("{}{suffix}", self.stem)
    }

    /// The unit the generator writes: foo.service, foo-network.service, foo@.service.
    pub fn service(&self) -> String {
        format!("{}.service", self.service_base())
    }

    /// The service to start and stop; None for a template without DefaultInstance=.
    pub fn startable(&self) -> Option<String> {
        if !self.is_template() {
            return Some(self.service());
        }
        let instance = self.ini.get("Install", "DefaultInstance")?;
        Some(format!("{}{instance}.service", self.service_base()))
    }

    /// Name of the podman container/pod/network/volume; None if it needs systemd specifiers.
    pub fn resource_name(&self) -> Option<String> {
        let key = type_of(&self.ext)?.3?;
        if self.stem.contains('@') {
            return None;
        }
        let name = match self.get(key).filter(|n| !n.is_empty()) {
            Some(name) => name,
            None if self.ext == ".container" => "systemd-%N".to_string(),
            None => format!("systemd-{}", self.stem),
        };
        let name = name.replace("%N", &self.service_base());
        (!name.contains('%')).then_some(name)
    }

    pub fn get(&self, key: &str) -> Option<String> {
        self.ini.get(&self.section, key)
    }

    pub fn get_in(&self, section: &str, key: &str) -> Option<String> {
        self.ini.get(section, key)
    }

    pub fn get_all(&self, key: &str) -> Vec<String> {
        self.ini.get_all(&self.section, key)
    }

    fn own(&self, key: &str, exts: &[&str]) -> Vec<String> {
        if exts.contains(&self.ext.as_str()) { self.get_all(key) } else { Vec::new() }
    }

    pub fn publish_ports(&self) -> Vec<String> {
        self.own("PublishPort", &[".container", ".pod", ".kube"])
    }

    pub fn volume_specs(&self) -> Vec<String> {
        self.own("Volume", &[".container", ".pod", ".build"])
    }

    pub fn mount_specs(&self) -> Vec<String> {
        self.own("Mount", &[".container"])
    }

    pub fn network_specs(&self) -> Vec<String> {
        self.own("Network", &[".container", ".pod", ".kube", ".build"])
    }

    pub fn env_files(&self) -> Vec<String> {
        if self.ext != ".container" {
            return Vec::new();
        }
        self.ini.get_words(&self.section, "EnvironmentFile")
    }

    pub fn install_targets(&self) -> Vec<String> {
        ["WantedBy", "RequiredBy", "UpheldBy"].iter().flat_map(|k| self.ini.get_words("Install", k)).collect()
    }

    pub fn uses_host_network(&self) -> bool {
        self.network_specs().iter().any(|spec| spec.split(':').next() == Some("host"))
    }

    /// (key=value as written, source) for every Volume=/Mount= that names a host path.
    pub fn bind_sources(&self) -> Vec<(String, String)> {
        let mut found = Vec::new();
        for spec in self.volume_specs() {
            let source = spec.split(':').next().unwrap_or("").to_string();
            if source.starts_with('.') || source.starts_with('/') {
                found.push((format!("Volume={spec}"), source));
            }
        }
        for spec in self.mount_specs() {
            let fields: BTreeMap<&str, &str> = spec.split(',').filter_map(|p| p.split_once('=')).collect();
            let source = fields.get("source").or_else(|| fields.get("src")).copied().unwrap_or("");
            if fields.get("type").copied().unwrap_or("volume") == "bind" && !source.is_empty() {
                found.push((format!("Mount={spec}"), source.to_string()));
            }
        }
        found
    }

    /// Other Quadlet files this one names, as (key, filename). Quadlet turns each
    /// into Requires= and After= on the other unit's service, so stopping that
    /// service stops this one too.
    pub fn references(&self) -> Vec<(String, String)> {
        let mut refs = Vec::new();
        let mut add = |key: &str, value: Option<&str>| {
            if let Some(value) = value.filter(|v| quadlet_ext(v).is_some()) {
                refs.push((key.to_string(), value.to_string()));
            }
        };
        for spec in self.network_specs() {
            add("Network", spec.split(':').next());
        }
        for spec in self.volume_specs() {
            add("Volume", spec.split(':').next());
        }
        for spec in self.mount_specs() {
            for part in spec.split(',') {
                if let Some((key, value)) = part.split_once('=')
                    && matches!(key.trim(), "source" | "src")
                {
                    add("Mount", Some(value.trim()));
                }
            }
        }
        if self.ext == ".container" {
            add("Pod", self.get("Pod").as_deref());
        }
        if self.ext == ".container" || self.ext == ".volume" {
            add("Image", self.get("Image").as_deref());
        }
        refs
    }

    /// Units named in [Unit] whose stop propagates to this one.
    pub fn requires(&self) -> Vec<String> {
        ["Requires", "BindsTo", "PartOf", "Requisite"].iter().flat_map(|k| self.ini.get_words("Unit", k)).collect()
    }

    pub fn sort_key(&self) -> (usize, String) {
        (KIND_ORDER.iter().position(|k| *k == self.kind).unwrap_or(KIND_ORDER.len()), self.filename.clone())
    }
}

pub fn read_lossy(path: &Path) -> std::io::Result<String> {
    Ok(String::from_utf8_lossy(&std::fs::read(path)?).into_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unit(filename: &str, text: &str, dropins: &[(&str, &str)]) -> QuadletUnit {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(filename);
        std::fs::write(&path, text).unwrap();
        for (rel, body) in dropins {
            let full = dir.path().join(rel);
            std::fs::create_dir_all(full.parent().unwrap()).unwrap();
            std::fs::write(full, body).unwrap();
        }
        QuadletUnit::load(&path, &[dir.path().to_path_buf()]).unwrap()
    }

    #[test]
    fn sections_repeats_comments_continuations() {
        let ini = IniFile::parse(
            "# leading comment\n[Container]\nImage=docker.io/library/nginx:1\nPublishPort=127.0.0.1:8080:80\n\
             PublishPort=127.0.0.1:8443:443\nExec=one \\\n  # dropped inside a continuation\n  two \\\n  three\n\
             ; another comment\nLabel=\"quoted value\"\n",
        );
        assert_eq!(ini.get("Container", "Image").unwrap(), "docker.io/library/nginx:1");
        assert_eq!(ini.get_all("Container", "PublishPort"), ["127.0.0.1:8080:80", "127.0.0.1:8443:443"]);
        assert_eq!(ini.get("Container", "Exec").unwrap(), "one two three");
        assert_eq!(ini.get("Container", "Label").unwrap(), "quoted value");
        assert!(ini.get("Container", "Missing").is_none());
    }

    #[test]
    fn empty_assignment_resets_a_list() {
        let ini = IniFile::parse("[Install]\nWantedBy=a.target\nWantedBy=\nWantedBy=b.target c.target\n");
        assert_eq!(ini.get_words("Install", "WantedBy"), ["b.target", "c.target"]);
    }

    #[test]
    fn service_names_follow_quadlet() {
        assert_eq!(unit("web.container", "[Container]\n", &[]).service(), "web.service");
        assert_eq!(unit("web.network", "[Network]\n", &[]).service(), "web-network.service");
        assert_eq!(unit("web.volume", "[Volume]\n", &[]).service(), "web-volume.service");
        assert_eq!(unit("web.pod", "[Pod]\n", &[]).service(), "web-pod.service");
        assert_eq!(unit("web.kube", "[Kube]\n", &[]).service(), "web.service");
        assert_eq!(unit("web.build", "[Build]\n", &[]).service(), "web-build.service");
    }

    #[test]
    fn service_name_override() {
        let u = unit("web.container", "[Container]\nServiceName=frontend\n", &[]);
        assert_eq!(u.service(), "frontend.service");
        assert_eq!(u.resource_name().unwrap(), "systemd-frontend");
    }

    #[test]
    fn templates() {
        let template = unit("worker@.container", "[Container]\n", &[]);
        assert_eq!(template.service(), "worker@.service");
        assert!(template.startable().is_none());
        let with_default = unit("worker@.container", "[Container]\n[Install]\nDefaultInstance=one\n", &[]);
        assert_eq!(with_default.startable().unwrap(), "worker@one.service");
        assert_eq!(unit("db@.volume", "[Volume]\n", &[]).service(), "db-volume@.service");
        assert_eq!(unit("worker@two.container", "[Container]\n", &[]).startable().unwrap(), "worker@two.service");
    }

    #[test]
    fn resource_names() {
        assert_eq!(unit("web.container", "[Container]\n", &[]).resource_name().unwrap(), "systemd-web");
        assert_eq!(unit("web.container", "[Container]\nContainerName=web\n", &[]).resource_name().unwrap(), "web");
        assert_eq!(unit("web.network", "[Network]\n", &[]).resource_name().unwrap(), "systemd-web");
        assert_eq!(unit("web.volume", "[Volume]\nVolumeName=web-data\n", &[]).resource_name().unwrap(), "web-data");
        assert!(unit("web.kube", "[Kube]\n", &[]).resource_name().is_none());
    }

    #[test]
    fn references_and_requires() {
        let u = unit(
            "app.container",
            "[Unit]\nRequires=other.service\n[Container]\nImage=app.build\nNetwork=app.network:ip=10.0.0.2\n\
             Network=host-shared.network\nVolume=app-data.volume:/data:Z\nVolume=/srv/x:/x\n\
             Mount=type=volume,source=cache.volume,destination=/cache\nPod=app.pod\n",
            &[],
        );
        let expected = [
            ("Network", "app.network"),
            ("Network", "host-shared.network"),
            ("Volume", "app-data.volume"),
            ("Mount", "cache.volume"),
            ("Pod", "app.pod"),
            ("Image", "app.build"),
        ];
        assert_eq!(u.references(), expected.map(|(k, v)| (k.to_string(), v.to_string())));
        assert_eq!(u.requires(), ["other.service"]);
    }

    #[test]
    fn bind_sources() {
        let u = unit(
            "app.container",
            "[Container]\nVolume=./data:/data\nVolume=/srv/app:/srv\nVolume=named:/named\n\
             Mount=type=bind,source=/etc/app,destination=/etc/app\n",
            &[],
        );
        let sources: Vec<String> = u.bind_sources().into_iter().map(|(_, s)| s).collect();
        assert_eq!(sources, ["./data", "/srv/app", "/etc/app"]);
    }

    #[test]
    fn dropins_merge_in_order() {
        let u = unit(
            "web-app.container",
            "[Container]\nImage=a\nPublishPort=1:1\n",
            &[
                ("container.d/10-image.conf", "[Container]\nImage=b\n"),
                ("web-.container.d/10-image.conf", "[Container]\nImage=c\n"),
                ("web-app.container.d/20-port.conf", "[Container]\nPublishPort=2:2\n"),
            ],
        );
        assert_eq!(u.get("Image").unwrap(), "c");
        assert_eq!(u.publish_ports(), ["1:1", "2:2"]);
        assert_eq!(u.dropins.len(), 2);
    }

    #[test]
    fn dropin_dir_names() {
        assert_eq!(
            dropin_dirs("a-b-c.container"),
            ["container.d", "a-.container.d", "a-b-.container.d", "a-b-c.container.d"]
        );
        assert_eq!(dropin_dirs("w@x.container"), ["container.d", "w@.container.d", "w@x.container.d"]);
    }

    #[test]
    fn words() {
        assert_eq!(split_words("a 'b c' \"d \\\"e\" f\\ g"), ["a", "b c", "d \"e", "f g"]);
        assert_eq!(split_words("unclosed 'quote"), ["unclosed", "'quote"]);
    }
}
