"""A block's upload, built from its filelist: the files it needs, where each goes, and the ``aristotle.f`` sent."""

import bisect
import dataclasses
import functools
import io
import os
import posixpath
import re
import stat
import tarfile
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath

from aristotlelib import local_file_utils
from aristotlelib.verilog import filelist
from aristotlelib.verilog.filelist import Entry, Kind

FILELIST_NAME = "aristotle.f"

EXTERNAL_FOLDER = "_ext"
"""Where files from outside the upload's folder go, each under a folder named for where it came from."""

#: The options the server reads from ``aristotle.f``, each with its argument, by every name it takes.
SERVER_OPTIONS = frozenset(
    {
        "--top",
        "-I",
        "--include-directory",
        "-D",
        "--define-macro",
        "-U",
        "--undefine-macro",
        "-y",
        "--libdir",
        "-Y",
        "--libext",
        "-G",
        "--timescale",
    }
)

#: Options whose argument slang splits at commas, which the server may not read as slang does.
_COMMA_LIST_OPTIONS = frozenset(
    {"--top", "-I", "--include-directory", "-U", "--undefine-macro", "-y", "--libdir", "-Y"}
)

# The name is on the directive's line, as slang requires.
_INCLUDE = re.compile(r'`include[ \t]*(?:"([^"\n]*)"|<([^>\n]*)>|(`\w+))')
#: A system include, up to the ``<`` that starts its name.
_SYSTEM_INCLUDE = re.compile(r"`include[ \t]*<")
_DEFINE = re.compile(r"`define\b")

#: What the design-unit scan blanks before it looks for a keyword: comments, strings and escaped identifiers. A line
#: comment ends at a lone carriage return too, and before a backslash that ends its line, which continues a define.
_UNIT_NOISE = re.compile(
    r"//(?:[^\r\n\\]|\\(?![\r\n]))*|/\*.*?(?:\*/|\Z)|\"(?:\\(?:\r\n|.)|[^\"\\\r\n])*\"?|\\\S+", re.ASCII | re.DOTALL
)
#: What Coq's lexer reads past: a comment's start or end, which nest, and a string, in which "" stands for a quote.
_COQ_NOISE = re.compile(r'\(\*|\*\)|"(?:[^"]|"")*"?')
#: Coq's vernacular outside its comments and strings: a library loaded, or a proof's start or end, set off by
#: whitespace. Each starts with its word, which re then finds as fast as str.find() does.
_COQ = (
    re.compile(r"Require(?<!\SRequire)\s+(?:Import|Export)\s", re.ASCII),
    re.compile(r"Proof(?<!\SProof)\.(?!\S)", re.ASCII),
    re.compile(r"Qed(?<!\SQed)\.(?!\S)", re.ASCII),
)
#: A macro's use or a compiler directive: a backtick, then a name, simple or escaped.
_MACRO = re.compile(r"`[A-Za-z_\\]")
#: A `` `define`` and its body, to the first line break no backslash escapes, once comments are blanked.
_DEFINE_LINE = re.compile(r"`define\b(?:[^\\\r\n]|\\(?:\r\n|[\s\S]))*")
#: A backslash that ends a line, which continues a define's body onto the next.
_CONTINUATION = re.compile(r"\\(?=\r\n|\n|\r)")
#: A conditional-compilation directive, with the name it tests.
_CONDITIONAL = re.compile(r"`(?:(?:ifdef|ifndef|elsif)[ \t]+[A-Za-z_][\w$]*|else|endif)(?![\w$])", re.ASCII)
#: A design unit's keyword.
_UNIT = re.compile(
    r"(?<![\w$`])(module|macromodule|primitive|interface|program|package|checker|config)(?![\w$])", re.ASCII
)
_LIFETIME = re.compile(r"(?:static|automatic)(?![\w$])", re.ASCII)
_CLASS = re.compile(r"class(?![\w$])", re.ASCII)
#: An escaped name, then a simple one or a macro standing for one.
_UNIT_NAME = re.compile(r"\\(\S+)|(`?[A-Za-z_][\w$]*)", re.ASCII)
_SPACES = " \t\r\n\f\v"
_BLANK = frozenset(_SPACES)
_WORD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")

#: A simple identifier.
_SIMPLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: A module's name as --blackbox takes it: a simple identifier, or an escaped one.
_MODULE_NAME = re.compile(rf"{_SIMPLE_NAME.pattern}|\\[!-~]+")
#: A byte that continues a simple identifier.
_IDENTIFIER_BYTE = rb"[A-Za-z0-9_$]"

#: How many of a file's other design units a message names before saying how many more there are.
_UNITS_NAMED = 3

#: The suffixes, in any case, of Verilog sources and headers: the files beside the block's that leave_out_unnamed() reads.
VERILOG_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh"})


class BundleError(Exception):
    """A block that can't be uploaded as its filelist describes it."""


@dataclasses.dataclass(frozen=True)
class Include:
    """An `` `include`` directive in a file the block reaches."""

    file: Path
    """The including file, as named."""
    line: int
    start: int
    """Offset of the included name, quotes excluded, in the including file's bytes."""
    end: int
    name: str
    """As written, without its quotes or brackets."""
    form: str
    """``quoted``, ``system`` for ``<name>``, which slang searches for only in folders this CLI never names, or
    ``macro``, such as `` `HEADER``, which isn't followed."""
    target: Path | None
    """The file it resolves to, as named; None if it doesn't resolve."""
    in_define: bool
    """Inside a `` `define`` body, so slang searches for it from wherever the macro is used."""


@dataclasses.dataclass(frozen=True)
class Rewrite:
    """An include whose name changes in the uploaded copy, on the same line, so that it reaches the same file there."""

    include: Include
    name: str
    """The new name: the included file's path in the upload, relative to the including file's folder there."""


@dataclasses.dataclass(frozen=True)
class Group:
    """An ``_ext`` folder: the folder outside the upload whose files it holds, and what gave it its name."""

    folder: Path
    named_by: str
    """``filelist`` for a variable the filelists expand, or ``folder`` for the folder's own name."""


@dataclasses.dataclass(frozen=True)
class Blackbox:
    """A module ``--blackbox`` names, and the block's files left out for it."""

    name: str
    """As named, an escaped name's backslash included."""
    origin: filelist.Origin
    files: tuple[Path, ...]
    """Each file left out for it, as named, that declares it and no design unit but black boxes: those the filelists
    name, then those Bundle.leave_out_unnamed() finds."""


@dataclasses.dataclass(frozen=True)
class Member:
    """A file in the upload."""

    archive: str
    """Its path in the upload."""
    path: Path
    """Where it is read from, as named."""
    reason: str
    """Why it goes up: the filelist line naming it, or the file including it."""


@dataclasses.dataclass
class Bundle:
    """The files a block's upload holds for it, from the folder submitted and outside it, and the ``aristotle.f`` it
    carries."""

    root: Path
    members: list[Member]
    """Every file the block reaches, in the order reached."""
    includes: list[Include]
    """Every `` `include`` in those files, in the order found."""
    filelist: filelist.Filelist
    filelist_text: str | None
    """The ``aristotle.f`` generated for the server; None when the committed one goes up unchanged."""
    groups: dict[str, Group] = dataclasses.field(default_factory=dict)
    """Each ``_ext`` folder, by name."""
    whole_folder: bool = True
    """Whether every file in ``root`` goes up, not just the block's."""
    committed: Path | None = None
    """The ``aristotle.f`` the block was read from, if any."""
    arguments: bool = False
    """Whether command-line options added to it."""
    rewrites: list[Rewrite] = dataclasses.field(default_factory=list)
    as_written: list[Include] = dataclasses.field(default_factory=list)
    """Includes inside a `` `define`` body that wouldn't reach their file in the upload, left as written."""
    blackboxes: list[Blackbox] = dataclasses.field(default_factory=list)
    """Each module ``--blackbox`` names, once, in the order first named."""
    read_from: dict[Path, str] = dataclasses.field(default_factory=dict)
    """Where each member's file, and the committed ``aristotle.f``, was read, links resolved, by its path as named."""

    @property
    def unresolved(self) -> list[Include]:
        return [i for i in self.includes if i.target is None]

    def leaves_out(self, path: Path) -> bool:
        """Whether the file at ``path``, under any name but a link's, is one of the blackboxes' files, which never go
        up; a link to one goes up only with its target, as any link does."""
        identities, reals = self._left_out
        if not identities and not reals:
            return False
        try:
            file_stat = os.lstat(path)
        except OSError:
            return False
        if stat.S_ISLNK(file_stat.st_mode):
            return False
        if (identity := local_file_utils.file_identity(file_stat)) is not None and identity in identities:
            return True
        return bool(reals) and os.path.normcase(os.path.realpath(path)) in reals

    @functools.cached_property
    def _left_out(self) -> tuple[set[tuple[int, int]], set[str]]:
        """The blackboxes' files by identity, and by resolved path those without one."""
        identities: set[tuple[int, int]] = set()
        reals: set[str] = set()
        for path in {f for box in self.blackboxes for f in box.files}:
            try:
                identity = local_file_utils.file_identity(os.stat(path))
            except OSError:
                identity = None
            if identity is None:
                reals.add(os.path.normcase(os.path.realpath(path)))
            else:
                identities.add(identity)
        return identities, reals

    def leave_out_unnamed(self, paths: Iterable[Path], reason: str, *, ignorable: bool = True) -> None:
        """Leave out each of ``paths``, files as named that go up beside the block's for ``reason``, that is a regular
        .v, .sv, .vh or .svh file declaring a black box and no other design unit, listing it under each it declares.

        Raises:
            BundleError: one declares a black box beside another design unit; the message names a rule in the root's
                .gitignore as a way to keep it out if ``ignorable``.
        """
        boxed = {box.name.removeprefix("\\"): filelist.Named(box.name, box.origin) for box in self.blackboxes}
        found: dict[str, list[Path]] = {}
        for path in paths if boxed else ():
            if path.suffix.lower() not in VERILOG_SUFFIXES or self.leaves_out(path):
                continue
            try:
                if not stat.S_ISREG(os.lstat(path).st_mode):
                    continue
                data = path.read_bytes()
            except OSError:
                # The upload's archiving reports it, as for any file it can't read.
                continue
            for name in _declared(path, reason, data, boxed, gitignore=self.root / ".gitignore" if ignorable else None):
                found.setdefault(name, []).append(path)
        if found:
            self.blackboxes = [
                dataclasses.replace(box, files=(*box.files, *found.get(box.name.removeprefix("\\"), ())))
                for box in self.blackboxes
            ]
            self.__dict__.pop("_left_out", None)

    def generate(self, uploaded: set[str]) -> str:
        """The ``aristotle.f`` for the server, naming the ``-I`` folders that hold a file in ``uploaded``, the upload's
        paths.

        Raises:
            BundleError: as build() does.
        """
        return _generate(self, uploaded)

    @property
    def used_groups(self) -> dict[str, Group]:
        """The ``_ext`` folders that hold a file of the upload: not one only an ``-I`` folder above ``root`` named."""
        used = {PurePosixPath(m.archive).parts[1] for m in self.members if self.is_outside(m)}
        return {name: group for name, group in self.groups.items() if name in used}

    @property
    def filelist_name(self) -> str:
        """The path in the upload of the ``aristotle.f`` that goes up: the committed one's, as spelled on disk, when
        it is at the root."""
        if self.committed is not None and Path(os.path.abspath(self.committed)).parent == self.root:
            return self.committed.name
        return FILELIST_NAME

    def is_outside(self, member: Member) -> bool:
        """Whether ``member`` comes from outside ``root``, and so goes up under ``_ext``."""
        return not _is_under(Path(os.path.normpath(os.path.abspath(member.path))), self.root)

    def is_linked_out(self, member: Member) -> bool:
        """Whether ``member``, named in ``root``, is reached through a link that leads outside it: it goes up where the
        block names it, as the build reads it, and is listed with where it is."""
        real, real_root = Path(os.path.realpath(member.path)), Path(os.path.realpath(self.root))
        return not self.is_outside(member) and not _is_under(real, real_root)

    def content(self, member: Member) -> bytes | None:
        """``member``'s bytes with its includes rewritten, or None when it goes up unchanged.

        Raises:
            BundleError: the file changed since its includes were read.
        """
        edits = self._edits.get(member.path)
        if not edits:
            return None
        data = Path(os.path.realpath(member.path)).read_bytes()
        for edit in edits:
            include = edit.include
            if data[include.start : include.end] != include.name.encode("utf-8", "surrogateescape"):
                raise BundleError(f"{filelist.shown(member.path)} changed while it was being uploaded. Try again.")
            data = data[: include.start] + edit.name.encode("utf-8", "surrogateescape") + data[include.end :]
        return data

    @functools.cached_property
    def _edits(self) -> dict[Path, list[Rewrite]]:
        """Each including file's rewrites, last in the file first."""
        edits: dict[Path, list[Rewrite]] = {}
        for rewrite in sorted(self.rewrites, key=lambda r: -r.include.start):
            edits.setdefault(rewrite.include.file, []).append(rewrite)
        return edits


@dataclasses.dataclass(frozen=True)
class _File:
    path: str
    real: str
    reason: str


def scan_includes(data: bytes) -> list[tuple[int, int, int, str, str, bool]]:
    """Each `` `include`` in ``data`` outside comments and strings: line, start and end of the name in bytes, the name
    as the file system decodes a path, its form, ``quoted``, ``system`` for ``<name>``, or ``macro``, and whether it is
    inside a `` `define`` body."""
    text = data.decode("latin-1")
    found = []
    i, n, line = 0, len(text), 1
    in_define = False
    # A system include before this index has no ">" after it on its line.
    unclosed = 0
    while i < n:
        c = text[i]
        if c == "\n":
            if in_define:
                # A line break ends the body unless a backslash escapes it; one inside a block comment never does.
                last = i - 1 if i > 0 and text[i - 1] == "\r" else i
                in_define = last > 0 and text[last - 1] == "\\"
            line += 1
            i += 1
        elif c == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
        elif c == '"':
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += 3 if text.startswith("\\\r\n", j) else 2 if text[j] == "\\" else 1
            # An escaped line break inside the string is a line too; an unescaped one ends it, for the loop to count.
            line += text.count("\n", i, j)
            i = j + 1 if j < n and text[j] == '"' else j
        elif c == "`" and _DEFINE.match(text, i):
            in_define = True
            i += len("`define")
        elif c == "`" and text.startswith("`include", i):
            system = _SYSTEM_INCLUDE.match(text, i) is not None
            if system and i < unclosed:
                # No match, as an earlier one on the line found no ">" up to its end.
                i += len("`include")
                continue
            match = _INCLUDE.match(text, i)
            if match is None:
                if system:
                    end = text.find("\n", i)
                    unclosed = n if end < 0 else end
                i += len("`include")
                continue
            group = next(g for g in (1, 2, 3) if match.group(g) is not None)
            form = ("quoted", "system", "macro")[group - 1]
            start, end = match.start(group), match.end(group)
            found.append((line, start, end, data[start:end].decode("utf-8", "surrogateescape"), form, in_define))
            i = match.end()
        else:
            i += 1
    return found


def scan_units(data: bytes) -> list[str]:
    """The name of each design unit ``data`` declares outside comments and strings, in order and at any depth: each
    ``module``, ``macromodule``, ``primitive``, ``interface``, ``program``, ``package``, ``checker`` and ``config``, an
    escaped name without its backslash, and a macro standing for one as written."""
    text = data.decode("latin-1")
    code = _verilog_code(text)
    defines = [m.span() for m in _DEFINE_LINE.finditer(code)]
    # A backslash that ends a line, once it has marked where a define goes on, as a space.
    flat = _CONTINUATION.sub(" ", code)
    # What comes before an interface outside a define: without the defines and conditional directives.
    outside = _CONTINUATION.sub(" ", _CONDITIONAL.sub(_blank, _DEFINE_LINE.sub(_blank, code)))
    names = []
    for match in _UNIT.finditer(flat):
        if match.group(1) == "interface" and _uses_interface(flat, outside, defines, match):
            continue
        i = _skip_space(flat, match.end())
        if lifetime := _LIFETIME.match(flat, i):
            i = _skip_space(flat, lifetime.end())
        if name := _UNIT_NAME.match(text, i):
            names.append(name.group(1) or name.group(2))
    return names


def _verilog_code(text: str) -> str:
    """``text`` at the same offsets with Verilog's comments and strings as spaces, and an escaped identifier as its
    backslash and underscores."""
    return _UNIT_NOISE.sub(lambda m: f"\\{'_' * (len(m.group()) - 1)}" if m.group()[0] == "\\" else _blank(m), text)


def _is_coq(data: bytes) -> bool:
    """Whether ``data``, a .v file's bytes, is Coq's: outside Coq's comments and strings, it loads a library, or starts
    or ends a proof, declares no design unit, and ends with a sentence's period, which Verilog reads as code too; and it
    uses no macro and no directive. No Verilog file passes: without a macro or a directive, all of its code counts, and
    a period there is followed by a name or a ``*``, never by a ``(*``, a string or the end, which is all that can
    follow that last one."""
    # A Coq file ends with a sentence or a comment, and most Verilog files, which end otherwise, are skipped unread.
    if not data.rstrip(_SPACES.encode()).endswith((b".", b"*)")):
        return False
    text = data.decode("latin-1")
    coq = _coq_code(text)
    end = len(coq.rstrip(_SPACES)) - 1
    if end < 0 or coq[end] != "." or not any(word.search(coq) for word in _COQ):
        return False
    code = _verilog_code(text)
    return code[end] == "." and not _MACRO.search(code) and not scan_units(coq.encode("latin-1"))


def _coq_code(text: str) -> str:
    """``text`` at the same offsets with Coq's comments, which nest, and its strings, inside a comment too, as spaces;
    one that doesn't end runs to the end."""
    kept: list[str] = []
    depth = done = start = 0
    for match in _COQ_NOISE.finditer(text):
        token = match.group()
        if token == "(*":
            if not depth:
                start = match.start()
            depth += 1
        elif token == "*)":
            if depth:
                depth -= 1
                if not depth:
                    kept += [text[done:start], " " * (match.end() - start)]
                    done = match.end()
        elif not depth:
            kept += [text[done : match.start()], " " * len(token)]
            done = match.end()
    if depth:
        kept += [text[done:start], " " * (len(text) - start)]
        done = len(text)
    return "".join(kept) + text[done:]


def _uses_interface(flat: str, outside: str, defines: list[tuple[int, int]], match: re.Match[str]) -> bool:
    """Whether the ``interface`` ``match`` found in ``flat``, scan_units' blanked text, declares none: a virtual
    interface or a generic interface port, by what comes before it, in ``flat`` when it is in one of the ``defines``,
    else in ``outside``; or an interface class."""
    i = bisect.bisect_right(defines, (match.start(), match.start())) - 1
    before = flat if i >= 0 and match.start() < defines[i][1] else outside
    end = match.start()
    while end and before[end - 1] in _BLANK:
        end -= 1
    start = end
    while start and before[start - 1] in _WORD:
        start -= 1
    if before[start:end] == "virtual" or before[end - 1 : end] in ("(", ","):
        return True
    return _CLASS.match(flat, _skip_space(flat, match.end())) is not None


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group())


def _skip_space(code: str, i: int) -> int:
    """The index of the first character at or after ``i`` that isn't whitespace."""
    while i < len(code) and code[i] in _BLANK:
        i += 1
    return i


def in_server_subset(path: Path, root: Path) -> bool:
    """Whether the server reads the filelist at ``path`` as slang does: one option and its argument, or one file,
    per line, in the options it takes, with every path relative and inside ``root``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if text.startswith("\ufeff"):
        # The reader skips a byte order mark, but the server's slang would read it as part of the first argument.
        return False
    for line in text.splitlines():
        if not line.strip() or line.startswith(("#", "//")):
            continue
        if line != line.strip() or any(c in line for c in "$'#*?") or "/*" in line or "..." in line or "\t" in line:
            return False
        words = _server_words(line)
        if words is None:
            return False
        if words == ["--single-unit"]:
            continue
        if len(words) == 2 and words[0] in SERVER_OPTIONS:
            option, value = words
            if option in _COMMA_LIST_OPTIONS and "," in value:
                return False
            if option in ("-I", "--include-directory", "-y", "--libdir") and not _inside(path.parent, value, root):
                return False
            continue
        if (
            len(words) == 1
            and not words[0].startswith(("-", "+"))
            and not words[0].endswith("/")
            and _inside(path.parent, words[0], root)
        ):
            continue
        return False
    return True


def _server_words(line: str) -> list[str] | None:
    """``line`` split into words as the server splits it, or None where slang would split it differently."""
    words: list[str] = []
    current: list[str] | None = None
    i, n, quoted = 0, len(line), False
    while i < n:
        c = line[i]
        if c == "\\":
            if i + 1 >= n or line[i + 1] not in '"\\ ' or (quoted and line[i + 1] == " "):
                return None
            current = (current or []) + [line[i + 1]]
            i += 2
            continue
        if c == '"':
            quoted = not quoted
            current = current or []
        elif c == " " and not quoted:
            if current is not None:
                words.append("".join(current))
                current = None
        else:
            current = (current or []) + [c]
        i += 1
    if quoted:
        return None
    if current is not None:
        words.append("".join(current))
    return words


def _inside(folder: Path, value: str, root: Path) -> bool:
    if os.path.isabs(value) or "\\" in value:
        return False
    joined = os.path.normpath(os.path.join(os.path.abspath(folder), value))
    top = os.path.normpath(os.path.abspath(root))
    return joined == top or joined.startswith(top.rstrip(os.sep) + os.sep)


def build(
    block: filelist.Filelist,
    root: Path,
    *,
    committed: Path | None = None,
    whole_folder: bool = True,
    arguments: bool = False,
    check: Callable[[Path, str], None] | None = None,
) -> Bundle:
    """The upload for ``block`` from the folder ``root``, all of which goes up when ``whole_folder``; ``committed``
    is the ``aristotle.f`` it was read from, and ``arguments`` whether command-line options added to it. ``check`` is
    called with each file the block reaches, as named, and why, before anything of it is read, and raises BundleError
    to refuse it. A file the filelists name that declares a ``--blackbox`` module and no other design unit is left out.

    Raises:
        BundleError: the block names more than one top, a ``--blackbox`` that isn't a module's name or is the top, a
            ``-y`` folder the server can't be given, ``-v`` files without a top, a value that isn't UTF-8, or a path a
            generated ``aristotle.f`` can't name; a ``--blackbox`` module shares a file with another design unit, or is
            in an included file, or leaves out every file named; or a file it reaches can't be read, has a name the
            upload can't hold, includes a path filelist.never_read() refuses, or ``check`` refuses it.
    """
    root = Path(os.path.normpath(os.path.abspath(root)))
    boxed = _boxed(block)
    distinct_tops = list(dict.fromkeys(t.value for t in block.tops))
    if len(distinct_tops) > 1:
        named = ", ".join(f"{t.value} ({t.origin})" for t in block.tops)
        raise BundleError(f"The filelist names more than one top module: {named}. Aristotle takes one.")
    for top in block.tops:
        if (box := boxed.get(top.value.removeprefix("\\"))) is not None:
            raise BundleError(
                f"--blackbox {box.value} ({box.origin}) names the top module, which Aristotle verifies, so it can't be "
                "a black box."
            )

    include_dirs = block.of_kind(Kind.INCLUDE_DIR)
    reached: dict[str, _File] = {}
    includes: list[Include] = []
    queue: list[_File] = []
    for entry in block.entries:
        if entry.kind in (Kind.SOURCE, Kind.LIBRARY_FILE):
            file = _File(str(entry.path), str(entry.real), f"named at {entry.origin}")
            if file.path not in reached:
                if check is not None:
                    check(Path(file.path), file.reason)
                reached[file.path] = file
                queue.append(file)
    left_out = _leave_out(queue, boxed) if boxed else {}
    if left_out:
        for path in left_out:
            del reached[path]
        queue = [f for f in queue if f.path not in left_out]
        if not queue:
            raise BundleError(
                "--blackbox leaves out every file the Verilog block names, so none of the design would go up."
            )
    listed = set(reached)
    read: set[str] = set()
    while queue:
        file = queue.pop(0)
        if _loops(file.path):
            if file.real in read:
                # A second pass through a link back up the tree: uploaded, not followed, or names would grow forever.
                continue
            read.add(file.real)
        data = _read(file)
        if boxed and file.path not in listed:
            _refuse_included(file, data, boxed)
        for line, start, end, name, form, in_define in scan_includes(data):
            if form == "quoted" and (why := filelist.never_read(name)):
                raise BundleError(f"{filelist.shown(Path(file.path))}:{line} includes {why}.")
            target = _resolve_include(name, file, include_dirs) if form == "quoted" else None
            includes.append(
                Include(
                    Path(file.path),
                    line,
                    start,
                    end,
                    name,
                    form,
                    None if target is None else Path(target.path),
                    in_define,
                )
            )
            if target is not None and target.path not in reached:
                reason = f"included at {filelist.shown(Path(file.path))}:{line}"
                if check is not None:
                    check(Path(target.path), reason)
                reached[target.path] = _File(target.path, target.real, reason)
                queue.append(reached[target.path])

    files = list(reached.values())
    paths = [Path(f.path) for f in files]
    used_dirs = [d.path for d in include_dirs if any(_is_under(p, d.path) for p in paths)]
    groups = _groups(paths + used_dirs, root, block.variables)
    members = [Member(_archive_path(Path(f.path), root, groups), Path(f.path), f.reason) for f in files]
    read_from = {Path(f.path): f.real for f in files}
    _check_names(members, root, read_from)

    blackboxes = [
        Blackbox(named.value, named.origin, tuple(Path(p) for p, declared in left_out.items() if name in declared))
        for name, named in boxed.items()
    ]
    bundle = Bundle(
        root, members, includes, block, None, groups, whole_folder, committed, arguments, blackboxes=blackboxes
    )
    if committed is not None:
        read_from[committed] = block.read_at if block.read_at is not None else os.path.realpath(committed)
    bundle.read_from = read_from
    if (
        arguments
        or committed is None
        or block.ignored
        or not in_server_subset(committed, root)
        or _named_outside(block, root)
        # The server reads its names from the upload's root; slang read them from the folder a link leads to.
        or os.path.dirname(os.path.realpath(committed)) != os.path.realpath(root)
    ):
        bundle.filelist_text = _generate(bundle)
        upload_dirs = [_archive_path(d.path, root, groups) for d in _include_dirs_kept(bundle)]
    else:
        upload_dirs = [_archive_path(d.path, root, groups) for d in include_dirs]
    bundle.rewrites, bundle.as_written = _rewrites(bundle, upload_dirs)
    return bundle


def _loops(path: str) -> bool:
    """Whether the folders above ``path`` pass through one real folder twice, as a link back up the tree makes them."""
    seen: set[str] = set()
    folder = os.path.dirname(path)
    while True:
        real = os.path.realpath(folder)
        if real in seen:
            return True
        seen.add(real)
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def _read(file: _File) -> bytes:
    try:
        return Path(file.real).read_bytes()
    except OSError as e:
        raise BundleError(f"Can't read {filelist.shown(Path(file.path))}: {e.strerror or e}") from e


def _boxed(block: filelist.Filelist) -> dict[str, filelist.Named]:
    """Each module ``--blackbox`` names, by its name without an escape's backslash, where it was first named.

    Raises:
        BundleError: a value isn't a module's name.
    """
    boxed: dict[str, filelist.Named] = {}
    for named in block.blackboxes:
        if not _MODULE_NAME.fullmatch(named.value):
            shown = named.value if named.value.isascii() and named.value.isprintable() else repr(named.value)[1:-1]
            # What an escaped name reads as once a filelist or a shell drops its backslash.
            hint = (
                " To name an escaped one, double its backslash in a filelist, or quote it in a shell."
                if _MODULE_NAME.fullmatch(f"\\{named.value}")
                else ""
            )
            raise BundleError(
                f"--blackbox {shown} ({named.origin}) isn't a module's name. Name one module per --blackbox, as its "
                f"declaration spells it.{hint}"
            )
        boxed.setdefault(named.value.removeprefix("\\"), named)
    return boxed


def _leave_out(files: list[_File], boxed: Mapping[str, filelist.Named]) -> dict[str, list[str]]:
    """The ``files`` that declare a black box and no other design unit, by path, with the black boxes each declares.

    Raises:
        BundleError: a file declares a black box beside another design unit, or can't be read.
    """
    left_out = {}
    for file in files:
        if declared := _declared(Path(file.path), file.reason, _read(file), boxed, gitignore=None):
            left_out[file.path] = declared
    return left_out


def _declared(
    path: Path, reason: str, data: bytes, boxed: Mapping[str, filelist.Named], *, gitignore: Path | None
) -> list[str]:
    """The black boxes that ``data``, the bytes of the file at ``path``, which goes up for ``reason``, declares, by the
    design-unit scan, which runs only when _name_in() finds one of their names in ``data``, and not on a .v file
    _is_coq() finds is Coq's; ``gitignore``, if any, is the .gitignore whose rule would keep it out of the upload.

    Raises:
        BundleError: it declares a black box beside another design unit.
    """
    if not any(_name_in(name).search(data) for name in boxed) or (path.suffix == ".v" and _is_coq(data)):
        return []
    units = list(dict.fromkeys(scan_units(data)))
    declared = [u for u in units if u in boxed]
    if declared and (others := [u for u in units if u not in boxed]):
        box = boxed[declared[0]]
        fix = (
            "or black-box those too"
            if gitignore is None
            else f"black-box those too, or keep the file out of the upload with a rule in {filelist.shown(gitignore)}"
        )
        raise BundleError(
            f"{filelist.shown(path)} ({reason}) can't be left out for --blackbox {box.value} ({box.origin}): it also "
            f"declares {_some(others)}. Move {box.value} to a file of its own, {fix}."
        )
    return declared


@functools.cache
def _name_in(name: str) -> re.Pattern[bytes]:
    """What finds ``name``, a black box's without an escape's backslash, in a file's bytes wherever scan_units() could
    read it as declared: a simple name as a whole identifier, and any other anywhere."""
    literal = re.escape(name.encode())
    if not _SIMPLE_NAME.fullmatch(name):
        return re.compile(literal)
    # The name first, which re finds as fast as bytes.find() does; the lookbehind then checks the byte before it.
    return re.compile(literal + rb"(?<!" + _IDENTIFIER_BYTE + literal + rb")(?!" + _IDENTIFIER_BYTE + rb")")


def _refuse_included(file: _File, data: bytes, boxed: Mapping[str, filelist.Named]) -> None:
    """Stops on ``file``, an included one, declaring a black box: the file including it goes up, and it with that.

    Raises:
        BundleError: naming the black box, and what would let the file be left out.
    """
    units = list(dict.fromkeys(scan_units(data)))
    if not (declared := [u for u in units if u in boxed]):
        return
    box = boxed[declared[0]]
    if others := [u for u in units if u not in boxed]:
        fix = (
            f"It also declares {_some(others)}, so move {box.value} to a file of its own that nothing includes, and "
            "name that file in the filelists."
        )
    else:
        fix = "Drop that `include: the file is then left out when the filelists name it."
    raise BundleError(
        f"{filelist.shown(Path(file.path))} ({file.reason}) can't be left out for --blackbox {box.value} "
        f"({box.origin}): the file including it goes up. {fix}"
    )


def _some(names: list[str]) -> str:
    """``names`` in a sentence, as ``a``, ``a and b`` or ``a, b and c``, naming at most _UNITS_NAMED and how many
    more."""
    if len(names) > _UNITS_NAMED:
        return f"{', '.join(names[:_UNITS_NAMED])} and {len(names) - _UNITS_NAMED} more"
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _rewrites(bundle: Bundle, upload_dirs: list[str]) -> tuple[list[Rewrite], list[Include]]:
    """The includes that wouldn't reach their file in the upload, as slang searches it, with the name that does; and
    those inside a `` `define`` body, which keep their name, as slang searches for them from wherever the macro is used.

    Raises:
        BundleError: such an include names its file by an absolute path, which the upload doesn't have, or the name
            that reaches the file can't go between an include's quotes.
    """
    by_path = {m.path: m.archive for m in bundle.members}
    uploaded = set(by_path.values())
    rewrites = []
    as_written = []
    for include in bundle.includes:
        if include.target is None:
            continue
        includer, target = by_path[include.file], by_path[include.target]
        if not os.path.isabs(include.name):
            found = None
            for folder in [posixpath.dirname(includer), *upload_dirs]:
                candidate = posixpath.normpath(posixpath.join(folder, include.name))
                escapes = folder not in ("", ".") and not candidate.startswith(f"{folder}/")
                if escapes and not any(name.startswith(f"{folder}/") for name in uploaded):
                    # A name that climbs out of a folder works only if the folder exists, and nothing puts it there.
                    continue
                if candidate in uploaded or (bundle.whole_folder and _in_folder(bundle.root, candidate)):
                    found = candidate
                    break
            if found == target:
                continue
        if include.in_define:
            # A leading slash too: on Windows, from Python 3.13, isabs() wants a drive.
            if os.path.isabs(include.name) or include.name.startswith(("/", "\\")):
                raise BundleError(
                    f"{filelist.shown(include.file)}:{include.line} includes {include.name} by its absolute path inside "
                    "a `define, which can't be rewritten for the upload: slang looks for it from wherever the macro is "
                    "used. Name it relative to an -I folder instead."
                )
            as_written.append(include)
            continue
        name = posixpath.relpath(target, posixpath.dirname(includer) or ".")
        if any(c in name for c in '"\n\r') or name.endswith("\\"):
            raise BundleError(
                f"{filelist.shown(include.file)}:{include.line} would have to include {name}, which an `include can't "
                "name. Rename the folders that path runs through."
            )
        rewrites.append(Rewrite(include, name))
    return rewrites, as_written


def _in_folder(root: Path, archive: str) -> bool:
    """Whether ``archive`` names a file the upload holds from ``root`` itself, its own ``_ext`` folder included, as an
    earlier upload unpacked there has."""
    parts = PurePosixPath(archive).parts
    return bool(parts) and parts[0] != ".." and os.path.isfile(root / archive)


def _resolve_include(name: str, includer: _File, include_dirs: list[Entry]) -> _File | None:
    """The file an `` `include "name"`` in ``includer`` reaches, as slang searches: an absolute path as it is, else
    the includer's folder, then each ``-I`` folder in order."""
    if not name:
        return None
    if os.path.isabs(name):
        if not os.path.isfile(name):
            return None
        path, real = os.path.normpath(name), os.path.realpath(name)
        return _File(path if os.path.realpath(path) == real else real, real, "")
    folders = [(os.path.dirname(includer.path), os.path.dirname(includer.real))]
    folders += [(str(d.path), str(d.real)) for d in include_dirs]
    for named, real in folders:
        target = os.path.join(real, name)
        if os.path.isfile(target):
            path, real_target = os.path.normpath(os.path.join(named, name)), os.path.realpath(target)
            return _File(path if os.path.realpath(path) == real_target else real_target, real_target, "")
    return None


def _groups(paths: list[Path], root: Path, variables: Mapping[str, str]) -> dict[str, Group]:
    """An ``_ext`` folder for each place ``paths`` outside ``root`` come from: the deepest folder a variable the
    filelists expand names above them, else their deepest common folder."""
    outside = [p for p in paths if not _is_under(p, root)]
    if not outside:
        return {}
    folders = _folders(variables)
    groups: dict[str, Group] = {}
    unplaced = []
    for path in outside:
        found = [(name, folder) for name, folder in folders if _is_under(path, folder)]
        if found:
            name, folder = max(found, key=lambda f: (len(f[1].parts), f[0]))
            groups.setdefault(name, Group(folder, "filelist"))
        else:
            unplaced.append(path)
    unplaced = [p for p in unplaced if not any(_is_under(p, g.folder) for g in groups.values())]
    for drive in dict.fromkeys(p.drive for p in unplaced):
        on_drive = [str(p if p.is_dir() else p.parent) for p in unplaced if p.drive == drive]
        common = Path(os.path.commonpath(on_drive))
        name = common.name or "root"
        while name in groups:
            name += "_"
        groups[name] = Group(common, "folder")
    return groups


def _folders(variables: Mapping[str, str]) -> list[tuple[str, Path]]:
    found = []
    for name, value in sorted(variables.items()):
        if value and os.path.isabs(value) and os.path.isdir(value) and Path(value).parent != Path(value):
            found.append((name, Path(os.path.normpath(value))))
    return found


def _is_under(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _archive_path(path: Path, root: Path, groups: Mapping[str, Group]) -> str:
    """Where ``path``, a file or folder as named, goes in the upload, ``/``-separated."""
    if _is_under(path, root):
        return PurePosixPath(*path.relative_to(root).parts).as_posix() if path != root else "."
    name, folder = max(
        ((n, g.folder) for n, g in groups.items() if _is_under(path, g.folder)), key=lambda g: len(g[1].parts)
    )
    return PurePosixPath(EXTERNAL_FOLDER, name, *path.relative_to(folder).parts).as_posix()


def _in_folder_upload(folder: Path, root: Path) -> bool:
    """Whether ``folder``, inside ``root``, goes up with it: not reached through a link to elsewhere, and holding a
    file."""
    if not _is_under(folder, root) or not _is_under(Path(os.path.realpath(folder)), Path(os.path.realpath(root))):
        return False
    return any(files for _, _, files in os.walk(folder))


def _credential(archived: Path, named: Path, leads: Path, root: Path, real_root: Path) -> str | None:
    """Why a reached file never goes up, as a credential by its path in the upload, as named, or where a link at it
    leads, each below ``root`` (``real_root`` as resolved), whose own name counts, or outside it, in full; None if it
    isn't one. A variable or a link can name such a file under another name."""
    for path, below, file_why, folder_why in (
        (archived, None, "is named like a credential", "is in a folder that holds credentials"),
        (named, root, "is named like a credential", "is in a folder that holds credentials"),
        (leads, real_root, "leads to a file named like a credential", "leads into a folder that holds credentials"),
    ):
        if local_file_utils.is_secret_file(path):
            return f"{file_why}, and such files are never uploaded. Leave it out of the block."
        if local_file_utils.is_in_secret_folder(path, below):
            return f"{folder_why}, and such files are never uploaded. Leave it out of the block."
    return None


def _check_names(members: list[Member], root: Path, read_from: Mapping[Path, str]) -> None:
    """Stops on a reached file whose name the upload can't hold, or that two files would share; where a link leads is
    where ``read_from`` says it was read."""
    seen: dict[str, Path] = {}
    real_root = Path(os.path.realpath(root))
    for member in members:
        # A .git folder counts in the upload path, as named and where a link leads, each below root or, outside it,
        # anywhere: an outside file's upload path depends on the files beside it. So does a git directory under any
        # name, where the file resolves. Then a credential; the name's own check comes last, as no rename makes either
        # upload.
        real = Path(read_from[member.path])
        leads = real.relative_to(real_root) if _is_under(real, real_root) else real
        named = member.path.relative_to(root) if _is_under(member.path, root) else member.path
        if any(local_file_utils.in_git_folder(p) for p in (PurePosixPath(member.archive), named, leads)) or (
            local_file_utils.in_git_dir(real)
        ):
            raise BundleError(
                f"{filelist.shown(member.path)} ({member.reason}) is inside a .git folder, which is never uploaded."
            )
        if credential := _credential(Path(member.archive), named, leads, root, real_root):
            raise BundleError(f"{filelist.shown(member.path)} ({member.reason}) {credential}")
        if not local_file_utils.is_utf8_name(Path(member.archive)):
            raise BundleError(
                f"{local_file_utils.archive_name(member.path)} ({member.reason}) has a name that isn't UTF-8, which "
                "Aristotle can't take. Rename it."
            )
        if member.archive in seen:
            raise BundleError(
                f"{filelist.shown(member.path)} and {filelist.shown(seen[member.archive])} would both go up as "
                f"{member.archive}."
            )
        seen[member.archive] = member.path


def _named_outside(block: filelist.Filelist, root: Path) -> bool:
    return any(not _is_under(e.path, root) for e in block.entries)


#: Characters that would end or split a line of the generated ``aristotle.f``: every control character but tab, which
#: quotes keep in a word, and the Unicode line breaks.
_LINE_BREAKING = re.compile("[\x00-\x08\x0a-\x1f\x7f\x85\u2028\u2029]")

#: What the server reads as a wildcard in a path of ``aristotle.f``, as slang does: ``*`` or ``?`` anywhere, and ``...``
#: at the start of a folder's or file's name.
_WILDCARD = re.compile(r"[*?]|(?<![^/])\.\.\.")


def _quote(value: str) -> str:
    """``value`` as one word of the server's ``aristotle.f``."""
    if value and not any(c in value for c in " \t\"\\#'") and not value.startswith(("//", "/*")):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _generate(bundle: Bundle, uploaded: set[str] | None = None) -> str:
    """The ``aristotle.f`` for the server: the nine options it reads, with paths inside the upload. An ``-I`` folder in
    ``root`` goes in when a file in ``uploaded`` is under it; without ``uploaded``, when it holds a file the folder walk
    would take.

    Raises:
        BundleError: the block needs a -y folder searched, has -v files in the upload but no --top, or a value or path
            that isn't UTF-8 or holds a character that would break its line; or a path holds what the server reads as a
            wildcard or, for an -I folder, a comma.
    """
    block = bundle.filelist
    if block.of_kind(Kind.LIBRARY_DIR):
        entry = block.of_kind(Kind.LIBRARY_DIR)[0]
        raise BundleError(
            f"-y {filelist.shown(entry.path)} ({entry.origin}) needs Aristotle to find the modules a library folder holds, which this "
            "version of the CLI can't do yet. Name the files the block needs, with -v or on lines of their own."
        )
    archived = {m.path: m.archive for m in bundle.members}
    if (library_files := [e for e in block.of_kind(Kind.LIBRARY_FILE) if e.path in archived]) and not block.tops:
        entry = library_files[0]
        raise BundleError(
            f"-v {filelist.shown(entry.path)} ({entry.origin}) names a library file, and without --top every module "
            "in it that nothing instantiates would be a top. Add --top <module>."
        )
    timescale = [block.timescale] if block.timescale is not None else []
    for option, values in [
        ("--top", block.tops),
        ("--timescale", timescale),
        ("-D", block.defines),
        ("-U", block.undefines),
        ("-G", block.parameters),
        ("-Y", block.library_extensions),
    ]:
        for named in values:
            if not local_file_utils.is_utf8_text(named.value):
                raise BundleError(
                    f"{option} {local_file_utils.upload_name(named.value)} ({named.origin}) isn't UTF-8, which the "
                    "aristotle.f Aristotle reads has to be."
                )
            if _LINE_BREAKING.search(named.value):
                raise BundleError(
                    f"{option} {repr(named.value)[1:-1]} ({named.origin}) holds a control character, which would break "
                    "its line of the aristotle.f Aristotle reads."
                )
    sources = [bundle.committed.name] if bundle.committed is not None else []
    source = " and ".join(sources + ["the command line"] * bundle.arguments) or "the command line"
    lines = [f"# Generated by the aristotle CLI from {source}, in the options Aristotle reads."]
    lines += [f"--top {_quote(t)}" for t in dict.fromkeys(t.value for t in block.tops)]
    lines.append("--single-unit")
    if block.timescale is not None:
        lines.append(f"--timescale {_quote(block.timescale.value)}")
    lines += [f"-D {_quote(d.value)}" for d in block.defines]
    lines += [f"-U {_quote(u.value)}" for u in block.undefines]
    lines += [f"-G {_quote(p.value)}" for p in block.parameters]
    for folder in _include_dirs_kept(bundle, uploaded):
        where = _archive_path(folder.path, bundle.root, bundle.groups)
        _check_line(where, folder)
        lines.append(f"-I {_quote(where)}")
    lines += [f"-Y {_quote(e.value)}" for e in block.library_extensions]
    listed: set[Path] = set()
    for entry in block.entries:
        if entry.kind in (Kind.SOURCE, Kind.LIBRARY_FILE) and entry.path in archived and entry.real not in listed:
            listed.add(entry.real)
            path = archived[entry.path]
            _check_line(path, entry)
            lines.append(_quote(f"./{path}" if path.startswith(("-", "+")) else path))
    return "\n".join(lines) + "\n"


def _check_line(path: str, entry: Entry) -> None:
    """Stops on a ``path`` in the upload that would break its line of the generated ``aristotle.f``, or that the
    server would read there as a pattern, or, for an ``-I`` folder, as a list."""
    if _LINE_BREAKING.search(path):
        raise BundleError(
            f"{repr(path)[1:-1]} ({entry.origin}) has a control character in its name, which would break its line "
            "of the aristotle.f Aristotle reads. Rename it."
        )
    if wildcard := _WILDCARD.search(path):
        raise BundleError(
            f'{path} ({entry.origin}) has "{wildcard.group()}" in its path, a wildcard in the aristotle.f Aristotle '
            "reads, so its line could match other paths too, or instead. Rename the file or folder whose name holds it."
        )
    if entry.kind is Kind.INCLUDE_DIR and "," in path:
        raise BundleError(
            f'{path} ({entry.origin}) has "," in its path, which separates -I folders in the aristotle.f Aristotle '
            "reads, so its line would name other folders. Rename the folder whose name holds it."
        )


def _include_dirs_kept(bundle: Bundle, uploaded: set[str] | None = None) -> list[Entry]:
    """The ``-I`` folders a generated ``aristotle.f`` names: those holding a reached file at their path in the upload,
    and those in a folder that goes up whole that hold a file in ``uploaded``, the upload's paths, or without it, a
    file the walk would take; never one whose path in the upload isn't UTF-8, which holds no uploaded file, as the walk
    leaves such files out and a reached one stops."""
    archives = [m.archive for m in bundle.members]
    kept = []
    for folder in bundle.filelist.of_kind(Kind.INCLUDE_DIR):
        if any(_is_under(m.path, folder.path) for m in bundle.members):
            # By its place in the upload: a folder above the one submitted holds the block's files on disk only.
            where = _archive_path(folder.path, bundle.root, bundle.groups)
            keep = where == "." or any(name.startswith(f"{where}/") for name in archives)
        elif not bundle.whole_folder or not _is_under(folder.path, bundle.root):
            continue
        elif uploaded is not None:
            where = _archive_path(folder.path, bundle.root, bundle.groups)
            keep = any(where == "." or name.startswith(f"{where}/") for name in uploaded)
        else:
            where = _archive_path(folder.path, bundle.root, bundle.groups)
            keep = _in_folder_upload(folder.path, bundle.root)
        if keep and local_file_utils.is_utf8_text(where):
            kept.append(folder)
    return kept


def folder_filelist(folder: Path) -> Path | None:
    """``folder``'s ``aristotle.f`` as spelled on disk, any case, the exact spelling first, also one it lacks the
    permission to examine, which reading it then names; None without one."""
    try:
        names = [n for n in os.listdir(folder) if n.lower() == FILELIST_NAME]
    except OSError:
        return None
    for name in sorted(names, key=lambda n: n != FILELIST_NAME):
        try:
            if stat.S_ISREG(os.stat(folder / name).st_mode):
                return folder / name
        except PermissionError:
            return folder / name
        except OSError:
            continue
    return None


def read(path: Path, root: Path, environ: Mapping[str, str] | None = None) -> Bundle:
    """The upload for the block the ``aristotle.f`` at ``path`` describes, from the folder ``root``.

    Raises:
        filelist.FilelistError: the filelist, or one it names, can't be read or is a credential.
        BundleError: as build() does.
    """
    return build(
        filelist.read(path, environ, check=lambda named, real, origin: check_filelist(named, real, origin, root)),
        root,
        committed=path,
    )


def check_filelist(named: Path, real: Path, origin: filelist.Origin, root: Path) -> None:
    """Refuses, before it is read, a filelist in a .git folder or that is a credential, as named or where a link at it
    leads, each below ``root`` or, outside it, in full: it goes up, or its options do."""
    top, real_top = Path(os.path.abspath(root)), Path(os.path.realpath(root))
    absolute = Path(os.path.abspath(named))
    below = absolute.relative_to(top) if _is_under(absolute, top) else absolute
    leads = real.relative_to(real_top) if _is_under(real, real_top) else real
    where = f"{origin}: " if origin.file is not None else ""
    if (
        local_file_utils.in_git_folder(below)
        or local_file_utils.in_git_folder(leads)
        or local_file_utils.in_git_dir(real)
    ):
        raise filelist.FilelistError(f"{where}{filelist.shown(named)} is inside a .git folder, which is never read.")
    if credential := _credential(below, below, leads, top, real_top):
        raise filelist.FilelistError(f"{where}{filelist.shown(named)} {credential}")


def source_name(bundle: Bundle, member: Member) -> str:
    """Where ``member`` comes from, as ``$VAR/...`` when a variable named its ``_ext`` folder, and where it is when a
    link in ``root`` leads to it."""
    if bundle.is_linked_out(member):
        # In full: relative to the working folder, it can read as the path it goes up at.
        return os.path.realpath(member.path)
    if not bundle.is_outside(member):
        return member.archive
    parts = PurePosixPath(member.archive).parts
    group = bundle.groups[parts[1]]
    if group.named_by == "folder":
        # In full: relative to the working folder, it can read as an upload path, its own or another file's.
        return os.path.abspath(member.path)
    return PurePosixPath(f"${parts[1]}", *parts[2:]).as_posix()


def _group_lines(bundle: Bundle) -> list[str]:
    """A line for each ``_ext`` folder, saying what named it."""
    lines = []
    for name, group in bundle.used_groups.items():
        where = f"{EXTERNAL_FOLDER}/{name}"
        if group.named_by == "folder":
            lines.append(f"  ({where} is {os.path.abspath(group.folder)}, named after the folder)")
        else:
            lines.append(f"  ({where} is ${name}, from the filelists)")
    return lines


def report(bundle: Bundle, *, files: bool = True, blackboxes: bool = True) -> tuple[list[str], list[str]]:
    """What to tell the user about ``bundle`` before it goes up: notes, and warnings; with ``files``, each file from
    outside its folder, in a warning naming where each is when a committed ``aristotle.f``, which someone else may have
    written, is read, and a note otherwise; with ``blackboxes``, blackbox_note(). With a committed one, a warning names
    each variable the filelists took from the environment, but not its value."""
    notes, warnings = [], []
    if bundle.filelist_text is not None:
        where = " in place of the folder's own" if bundle.committed is not None else ""
        notes.append(
            f"A generated {bundle.filelist_name} goes up{where}: it holds the options Aristotle reads, with every path "
            "inside the upload."
        )
    outside = [m for m in bundle.members if bundle.is_outside(m) or bundle.is_linked_out(m)]
    if outside and files:
        folder = filelist.shown(bundle.root)
        lines = [
            f"Uploading {len(outside)} {'file' if len(outside) == 1 else 'files'} from outside "
            f"{'the current folder' if folder == '.' else folder}:"
        ]
        width = max(len(m.archive) for m in outside)
        if bundle.committed is not None:
            lines += [f"  {m.archive.ljust(width)} <- {Path(os.path.realpath(m.path))}" for m in outside]
            warnings.append("\n".join(lines))
        else:
            lines += [f"  {m.archive.ljust(width)} <- {source_name(bundle, m)}" for m in outside]
            notes.append("\n".join(lines + _group_lines(bundle)))
    if blackboxes and (note := blackbox_note(bundle)):
        notes.append(note)
    if bundle.committed is not None and bundle.filelist.variables:
        names = sorted(bundle.filelist.variables)
        it, names_, sets = ("it", "names", "sets") if len(names) == 1 else ("they", "name", "set")
        warnings.append(
            f"The filelists take {', '.join(f'${n}' for n in names)} from this machine's environment: the files "
            f"{it} {names_} go up, and the values {it} {sets} with them."
        )
    if bundle.rewrites:
        by_path = {m.path: m.archive for m in bundle.members}
        lines = ["Rewritten in the upload only, on the same line:"]
        for rewrite in bundle.rewrites:
            include = rewrite.include
            lines.append(f'  {by_path[include.file]}:{include.line}  "{include.name}" -> "{rewrite.name}"')
        notes.append("\n".join(lines))
    for ignored in bundle.filelist.ignored:
        notes.append(f"Ignored {ignored.text} ({ignored.origin}): {ignored.reason}")
    for include in bundle.as_written:
        warnings.append(
            f'`include "{include.name}" ({filelist.shown(include.file)}:{include.line}) is inside a `define, so it goes '
            "up as written: slang looks for it from wherever the macro is used, and in the upload that may reach "
            "another file, or none."
        )
    for include in bundle.unresolved:
        where = f"{filelist.shown(include.file)}:{include.line}"
        if include.form == "macro":
            warnings.append(f"`include {include.name} ({where}) names a macro, so the file it includes isn't uploaded.")
        elif include.form == "system":
            warnings.append(
                f"`include <{include.name}> ({where}) is a system include, which Aristotle doesn't look for, so no "
                "file goes up for it."
            )
        else:
            warnings.append(f'`include "{include.name}" ({where}) names no file Aristotle can find, so none goes up.')
    return notes, warnings


def blackbox_note(bundle: Bundle) -> str | None:
    """A note naming each black box, where it was named, and the files left out for it; None without any."""
    if not bundle.blackboxes:
        return None
    lines = ["Black boxes, which the prompt names, and the files left out for them:"]
    for box in bundle.blackboxes:
        left_out = ", ".join(filelist.shown(f) for f in box.files) or "no file in the upload declares it"
        lines.append(f"  {box.name} ({box.origin}): {left_out}")
    return "\n".join(lines)


def listing(bundle: Bundle) -> str:
    """Every file of the block, with where it comes from and why it goes up, then the ``aristotle.f`` that goes up."""
    width = max((len(m.archive) for m in bundle.members), default=0)
    lines = [f"The Verilog block's {len(bundle.members)} {'file' if len(bundle.members) == 1 else 'files'}:"]
    for member in bundle.members:
        source = source_name(bundle, member)
        origin = f" <- {source}" if bundle.is_outside(member) or bundle.is_linked_out(member) else ""
        lines.append(f"  {member.archive.ljust(width)}{origin}  ({member.reason})")
    lines += _group_lines(bundle)
    if bundle.filelist_text is None:
        lines.append(f"{bundle.filelist_name} goes up as it is.")
    else:
        lines.append(f"{bundle.filelist_name}, as it goes up:")
        lines += [f"  {line}" for line in bundle.filelist_text.splitlines()]
    return "\n".join(lines)


def prompt_lines(bundle: Bundle) -> str:
    """What a prompt that sends ``bundle`` ends with: a line naming its ``aristotle.f``, then one naming any black
    boxes."""
    lines = [f"Use {bundle.filelist_name} as the commands file."]
    if instruction := blackbox_instruction(bundle):
        lines.append(instruction)
    return "\n".join(lines)


def blackbox_instruction(bundle: Bundle) -> str | None:
    """The sentence asking Aristotle to treat ``bundle``'s black boxes as such; None without any."""
    names = [box.name for box in bundle.blackboxes]
    if not names:
        return None
    if len(names) == 1:
        return (
            f"Treat this module as a black box with unconstrained outputs; its source isn't in the upload: {names[0]}."
        )
    return (
        "Treat these modules as black boxes with unconstrained outputs; their sources aren't in the upload: "
        f"{', '.join(names)}."
    )


def summary(bundle: Bundle) -> dict[str, object]:
    """``bundle`` as JSON: its files, black boxes, rewrites, includes in a `` `define`` left as written, ignored
    options, unfollowed includes, ``_ext`` folders and ``aristotle.f``."""
    by_path = {m.path: m.archive for m in bundle.members}
    return {
        "files": [{"path": m.archive, "source": source_name(bundle, m), "reason": m.reason} for m in bundle.members],
        "blackboxes": [
            {"module": box.name, "at": str(box.origin), "left_out": [filelist.shown(f) for f in box.files]}
            for box in bundle.blackboxes
        ],
        "rewritten_includes": [
            {"file": by_path[r.include.file], "line": r.include.line, "from": r.include.name, "to": r.name}
            for r in bundle.rewrites
        ],
        "includes_as_written": [{"file": by_path[i.file], "line": i.line, "name": i.name} for i in bundle.as_written],
        "ignored": [{"option": i.text, "at": str(i.origin), "reason": i.reason} for i in bundle.filelist.ignored],
        "unfollowed_includes": [
            {"file": filelist.shown(i.file), "line": i.line, "name": i.name, "form": i.form} for i in bundle.unresolved
        ],
        "external_folders": {
            f"{EXTERNAL_FOLDER}/{name}": {"folder": str(g.folder), "named_by": g.named_by}
            for name, g in bundle.used_groups.items()
        },
        "filelist": bundle.filelist_text,
    }


def write_member(tar: tarfile.TarFile, bundle: Bundle | None, member: Member, archived: set[tuple[int, int]]) -> None:
    """Archive the file ``member`` names, following links, at its path in the block, with ``bundle``'s rewrites;
    record its identity in ``archived``.

    Raises:
        BundleError: the file changed since its includes were read.
        local_file_utils.FileChangedError: its path leads elsewhere than when ``bundle`` was read, or the file was
            replaced while it was being archived.
    """
    real = os.path.realpath(member.path)
    if bundle is not None and bundle.read_from.get(member.path, real) != real:
        raise local_file_utils.FileChangedError.at(member.path)
    content = bundle.content(member) if bundle is not None else None
    if content is None:
        file_stat = os.lstat(real)
        local_file_utils.add_file(tar, real, member.archive, file_stat)
    else:
        # Built by hand, as it holds the rewritten bytes, not the file's.
        file_stat = os.lstat(real)
        info = tarfile.TarInfo(member.archive)
        info.size, info.mode, info.mtime = len(content), stat.S_IMODE(file_stat.st_mode), int(file_stat.st_mtime)
        tar.addfile(info, io.BytesIO(content))
    if (identity := local_file_utils.file_identity(file_stat)) is not None:
        archived.add(identity)


def write_text(tar: tarfile.TarFile, name: str, text: str) -> None:
    """Archive ``text``, in UTF-8, as the file ``name``."""
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
    tar.addfile(info, io.BytesIO(data))
