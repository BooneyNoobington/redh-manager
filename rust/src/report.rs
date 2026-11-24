//! Terminal output: one status word per line, colour only on a terminal.

use std::cell::RefCell;
use std::io::{IsTerminal, Write};
use std::rc::Rc;

/// The status words are padded to one width so the messages line up.
pub const WIDTH: usize = 5;

fn colour_of(level: &str) -> &'static str {
    match level {
        "ok" => "32",
        "info" => "36",
        "warn" => "33",
        "FAIL" => "1;31",
        "skip" => "2",
        _ => "0",
    }
}

#[derive(Clone)]
enum Sink {
    Stdout,
    Buffer(Rc<RefCell<String>>),
}

#[derive(Clone)]
pub struct Printer {
    sink: Sink,
    pub colour: bool,
    pub verbose: bool,
}

impl Printer {
    pub fn stdout(colour: Option<bool>, verbose: bool) -> Self {
        let colour = colour.unwrap_or_else(|| {
            std::io::stdout().is_terminal()
                && std::env::var_os("NO_COLOR").is_none_or(|v| v.is_empty())
                && std::env::var("TERM").map_or(true, |t| t != "dumb")
        });
        Printer { sink: Sink::Stdout, colour, verbose }
    }

    /// A printer that collects its output into buf, for the tests.
    #[cfg(test)]
    pub fn buffer(buf: Rc<RefCell<String>>, verbose: bool) -> Self {
        Printer { sink: Sink::Buffer(buf), colour: false, verbose }
    }

    pub fn paint(&self, code: &str, text: &str) -> String {
        if self.colour { format!("\x1b[{code}m{text}\x1b[0m") } else { text.to_string() }
    }

    pub fn dim(&self, text: &str) -> String {
        self.paint("2", text)
    }

    pub fn write(&self, text: &str) {
        match &self.sink {
            Sink::Stdout => {
                // A reader that went away (| head) is no reason to fail.
                let mut out = std::io::stdout().lock();
                let _ = writeln!(out, "{text}");
                let _ = out.flush();
            }
            Sink::Buffer(buf) => {
                let mut buf = buf.borrow_mut();
                buf.push_str(text);
                buf.push('\n');
            }
        }
    }

    pub fn blank(&self) {
        self.write("");
    }

    pub fn heading(&self, text: &str) {
        self.write(&self.paint("1", text));
    }

    /// '  ok    text' plus optional dimmed detail lines underneath.
    pub fn status(&self, level: &str, text: &str, details: &[String]) {
        self.status_at(level, text, details, 2, None);
    }

    pub fn status_at(&self, level: &str, text: &str, details: &[String], indent: usize, detail_indent: Option<usize>) {
        let word = self.paint(colour_of(level), &format!("{level:<WIDTH$}"));
        self.write(&format!("{}{} {}", " ".repeat(indent), word, text));
        let pad = " ".repeat(detail_indent.unwrap_or(indent + WIDTH + 1));
        for line in details {
            self.write(&format!("{pad}{}", self.dim(line)));
        }
    }
}
