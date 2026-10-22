"""Wildcard patterns, matched in time linear in the text for each step of the pattern."""

import itertools
import re
from enum import Enum


class Run(Enum):
    """A run of characters in a pattern, by the regex that matches it."""

    ANY = ".*"
    """Any run, including none."""

    FOLDERS = "(?:.*/)?"
    """None, or any run that ends in ``/``."""


def steps(fragments: list[Run | str]) -> tuple[Run | re.Pattern[str], ...]:
    """``fragments``, each a Run or a regex that matches exactly one character, as matches() takes them: the regexes
    between two Runs compiled as one, and Runs in a row as the one they amount to."""
    compiled: list[Run | re.Pattern[str]] = []
    for is_run, group in itertools.groupby(fragments, key=lambda fragment: isinstance(fragment, Run)):
        if is_run:
            compiled.append(Run.ANY if Run.ANY in group else Run.FOLDERS)
        else:
            compiled.append(re.compile("".join(map(str, group)), re.DOTALL))
    return tuple(compiled)


def matches(pattern: tuple[Run | re.Pattern[str], ...], text: str) -> bool:
    """Whether all of ``text`` matches ``pattern``, the steps steps() gives, in time linear in ``text`` for each step."""
    ends = {0}
    after_any = False
    for step in pattern:
        if step is Run.ANY:
            after_any = True
            continue
        start = min(ends)
        if step is Run.FOLDERS:
            ends |= {i + 1 for i in range(start, len(text)) if text[i] == "/"}
        elif after_any:
            ends = set()
            found = step.search(text, start)
            while found:
                ends.add(found.end())
                found = step.search(text, found.start() + 1)
        else:
            ends = {found.end() for i in ends if (found := step.match(text, i))}
        after_any = False
        if not ends:
            return False
    return after_any or len(text) in ends
