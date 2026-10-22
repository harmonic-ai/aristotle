"""Commands to paste, quoted for the shell that will read them."""

import os
import re
import shlex
from collections.abc import Sequence

_PLAIN_ON_WINDOWS = re.compile(r"[A-Za-z0-9_.:=+/-]+")
"""Characters cmd.exe and PowerShell both take literally outside quotes."""


_FLAG_LIKE = re.compile(r"-[A-Za-z0-9][A-Za-z0-9-]*")
"""A word PowerShell passes on whole although it starts with one dash, as ``-C``. It splits ``-x.y`` at the dot, and
after a ``--`` splits ``-x:y`` at the colon."""

_SPLIT = re.compile('[$`"\u201c\u201d\u201e%!\r\n]|\\\\$')
"""What PowerShell (``$``, a backtick, a straight or curly double quote) or cmd.exe (``_POWERSHELL_ONLY``) reads
specially in double quotes."""

_POWERSHELL_ONLY = re.compile(r'[%!"\r\n]|\\$')
"""What cmd.exe can't be given literally in double quotes: it expands ``%`` and ``!``, ends the command at a line feed
and drops a carriage return, and its C runtime reads a ``"`` or a backslash before the closing quote."""


def shell_commands(*commands: Sequence[str]) -> list[tuple[str | None, list[str]]]:
    """``commands``, each a list of words, as lines to paste, labelled with the shell they are quoted for, or None
    when one form serves every shell.

    POSIX quoting outside Windows, where a word that starts with ``=`` is quoted too: zsh replaces it with a program's
    path. Under pwsh there, which sets PSModulePath for what it runs, PowerShell's form follows when it differs, each
    labelled, since a POSIX shell started from pwsh inherits PSModulePath as well, and GitHub's Ubuntu runners set it
    for every step. On Windows a plain word goes bare and any other in double quotes, which cmd.exe and PowerShell both
    take literally, unless a word holds something one of them reads specially there. Then the commands go double-quoted
    for cmd.exe and single-quoted for PowerShell, with its single quotes doubled, the curly ones included, since
    PowerShell ends a single-quoted string at those too. What cmd.exe can't take literally in double quotes leaves the
    commands for PowerShell alone.
    """

    def posix(word: str) -> str:
        quoted = shlex.quote(word)
        return f"'{word}'" if quoted.startswith("=") else quoted

    def plain(word: str) -> bool:
        whole = not word.startswith("-") or word.startswith("--") or _FLAG_LIKE.fullmatch(word)
        return bool(_PLAIN_ON_WINDOWS.fullmatch(word) and whole)

    def double(word: str) -> str:
        return word if plain(word) else f'"{word}"'

    def single(word: str) -> str:
        if plain(word):
            return word
        if word == "--%":
            # PowerShell's stop-parsing token, which it drops even in quotes; an array hands it on.
            return "@('--%')"
        return "'" + re.sub("['\u2018\u2019\u201a\u201b]", r"\g<0>\g<0>", word) + "'"

    powershell = ("PowerShell", [" ".join(map(single, command)) for command in commands])
    if os.name != "nt":
        lines = [" ".join(map(posix, command)) for command in commands]
        if not os.environ.get("PSModulePath") or powershell[1] == lines:
            return [(None, lines)]
        return [("a POSIX shell", lines), powershell]

    words = [word for command in commands for word in command]
    cmd = [" ".join(map(double, command)) for command in commands]
    if not any(_SPLIT.search(word) for word in words):
        return [(None, cmd)]
    if any(_POWERSHELL_ONLY.search(word) for word in words):
        return [powershell]
    return [("cmd.exe", cmd), powershell]


def shell_hint(lead: str, *commands: Sequence[str]) -> str:
    """``lead``, then ``commands`` to paste: once, or once for each shell that would read them differently."""
    lines: list[str] = []
    for shell, quoted in shell_commands(*commands):
        lines.append(f"{'or' if lines else lead}{f', in {shell}' if shell else ''}:")
        lines.extend(f"  {line}" for line in quoted)
    return "\n".join(lines)
