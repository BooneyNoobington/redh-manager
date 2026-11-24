"""Terminal output: one status word per line, colour only on a terminal."""

from __future__ import annotations

import os
import sys
from typing import Iterable, Optional, TextIO

# The words are padded to one width so the messages line up.
LEVELS = ("ok", "info", "warn", "FAIL", "skip")
WIDTH = 5
_COLOURS = {"ok": "32", "info": "36", "warn": "33", "FAIL": "1;31", "skip": "2"}


class Printer:
    def __init__(self, colour: Optional[bool] = None, verbose: bool = False,
                 stream: Optional[TextIO] = None) -> None:
        self.stream = stream or sys.stdout
        if colour is None:
            colour = (getattr(self.stream, "isatty", lambda: False)()
                      and not os.environ.get("NO_COLOR")
                      and os.environ.get("TERM") != "dumb")
        self.colour = bool(colour)
        self.verbose = verbose

    def paint(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.colour else text

    def dim(self, text: str) -> str:
        return self.paint("2", text)

    def write(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def heading(self, text: str) -> None:
        self.write(self.paint("1", text))

    def status(self, level: str, text: str, details: Iterable[str] = (),
               indent: int = 2, detail_indent: Optional[int] = None) -> None:
        """'  ok    text' plus optional dimmed detail lines underneath."""
        word = self.paint(_COLOURS.get(level, "0"), level.ljust(WIDTH))
        self.write(f"{' ' * indent}{word} {text}")
        pad = " " * (detail_indent if detail_indent is not None
                     else indent + WIDTH + 1)
        for line in details:
            self.write(pad + self.dim(line))
