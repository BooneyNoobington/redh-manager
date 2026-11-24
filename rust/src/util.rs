//! Shared vocabulary: errors and small helpers.

use std::fmt;

#[derive(Debug)]
pub enum Error {
    /// Ends the command with 'error: ...' (exit status 1).
    Die(String),
    /// Something on the host is unreachable or broken (exit status 2).
    Host(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Die(m) | Error::Host(m) => f.write_str(m),
        }
    }
}

impl From<String> for Error {
    fn from(message: String) -> Self {
        Error::Die(message)
    }
}

pub type Res<T> = Result<T, Error>;

pub fn plural(count: usize, word: &str) -> String {
    plural_with(count, word, "s")
}

pub fn plural_with(count: usize, word: &str, suffix: &str) -> String {
    format!("{count} {word}{}", if count == 1 { "" } else { suffix })
}

/// 'a, b, c' — or 'a, b, c and 4 more' for long lists.
pub fn join_names<S: AsRef<str>>(names: &[S], limit: usize) -> String {
    let names: Vec<&str> = names.iter().map(AsRef::as_ref).collect();
    if names.len() <= limit {
        return names.join(", ");
    }
    format!("{} and {} more", names[..limit].join(", "), names.len() - limit)
}

/// difflib's get_close_matches, near enough: the most similar name, if similar enough.
pub fn close_match<'a>(word: &str, choices: &'a [String]) -> Option<&'a String> {
    fn distance(a: &str, b: &str) -> usize {
        let b: Vec<char> = b.chars().collect();
        let mut row: Vec<usize> = (0..=b.len()).collect();
        for (i, ca) in a.chars().enumerate() {
            let mut previous = row[0];
            row[0] = i + 1;
            for (j, cb) in b.iter().enumerate() {
                let current = row[j + 1];
                row[j + 1] = if ca == *cb { previous } else { 1 + previous.min(row[j]).min(row[j + 1]) };
                previous = current;
            }
        }
        row[b.len()]
    }
    choices
        .iter()
        .map(|c| {
            let longest = word.chars().count().max(c.chars().count()).max(1);
            (1.0 - distance(word, c) as f64 / longest as f64, c)
        })
        .filter(|(score, _)| *score >= 0.6)
        .max_by(|a, b| a.0.total_cmp(&b.0))
        .map(|(_, c)| c)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn helpers() {
        assert_eq!(plural(1, "unit"), "1 unit");
        assert_eq!(plural(3, "unit"), "3 units");
        assert_eq!(plural_with(2, "match", "es"), "2 matches");
        assert_eq!(join_names(&["a", "b", "c"], 2), "a, b and 1 more");
        let apps = vec!["nextcloud".to_string(), "gitea".to_string()];
        assert_eq!(close_match("nextclud", &apps).unwrap(), "nextcloud");
        assert!(close_match("zzz", &apps).is_none());
    }
}
