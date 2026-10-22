"""Reading a simulator-style Verilog filelist with the rules slang applies to command files."""

import dataclasses
import functools
import os
import re
from collections.abc import Callable, Mapping, Sequence
from enum import Enum
from pathlib import Path

from aristotlelib import wildcard


class FilelistError(Exception):
    """A filelist that can't be read; the message names the file and line."""


@dataclasses.dataclass(frozen=True)
class Origin:
    """Where a value was named: ``line`` of the filelist ``file``, or the command line when ``file`` is None."""

    file: Path | None = None
    line: int = 0

    def __str__(self) -> str:
        return "the command line" if self.file is None else f"{shown(self.file)}:{self.line}"


def shown(path: Path) -> str:
    """``path`` relative to the working directory when it is below it, else as it is."""
    try:
        cwd = Path.cwd()
    except OSError:
        return str(path)
    return os.path.relpath(path, cwd) if path.is_relative_to(cwd) else str(path)


_NETWORK_PATH = (
    "a network path, which is never read: on Windows, looking one up connects to the computer it names and signs in "
    "to it"
)
_DEVICE_PATH = "a Windows device path, which is never read: one can lead to another computer or a device"
_DRIVE_RELATIVE_PATH = (
    "a drive-relative path, which is never read: on Windows, one leads into that drive's current folder, wherever "
    "that is"
)


def never_read(path: str) -> str | None:
    """Why ``path``, as written, is never looked up, on any platform, for a message: in Windows' forms, with either
    slash, it names another computer, a device or a drive's current folder; None for any other path."""
    head = path[:4].replace("/", "\\")
    if head.startswith("\\\\"):
        return _DEVICE_PATH if head[2:3] in ("?", ".") and head[3:4] in ("", "\\") else _NETWORK_PATH
    if head == "\\??\\":
        return _DEVICE_PATH
    if path[1:2] == ":" and path[2:3] not in ("\\", "/"):
        return _DRIVE_RELATIVE_PATH
    return None


def _refuse_unread(value: str, origin: Origin, named: str) -> None:
    """Stops on ``value``, the ``named`` file or folder as written, if never_read() refuses it."""
    if why := never_read(value):
        raise FilelistError(f"{origin}: the {named} named is {why}")


class Kind(str, Enum):
    SOURCE = "source"
    """A source file, named on its own."""

    LIBRARY_FILE = "library_file"
    """A ``-v`` file."""

    INCLUDE_DIR = "include_dir"
    """An ``-I`` or ``+incdir+`` folder."""

    LIBRARY_DIR = "library_dir"
    """A ``-y`` folder, searched for modules nothing else defines."""

    def __str__(self) -> str:
        return self.value


@dataclasses.dataclass(frozen=True)
class Entry:
    """A file or folder a filelist names."""

    kind: Kind
    path: Path
    """Absolute and normalized, as the filelist names it."""
    real: Path
    """What ``path`` reaches, with symbolic links resolved."""
    origin: Origin


@dataclasses.dataclass(frozen=True)
class Named:
    """A value other than a path, such as a top module or a define, and where it was named."""

    value: str
    origin: Origin


@dataclasses.dataclass(frozen=True)
class Ignored:
    """An option the upload's build configuration leaves out, as written, and why."""

    text: str
    origin: Origin
    reason: str


@dataclasses.dataclass
class Filelist:
    """A block's build configuration as its filelists state it."""

    entries: list[Entry] = dataclasses.field(default_factory=list)
    """Files and folders in the order named, each once per kind."""
    tops: list[Named] = dataclasses.field(default_factory=list)
    defines: list[Named] = dataclasses.field(default_factory=list)
    """``NAME`` or ``NAME=VALUE``."""
    undefines: list[Named] = dataclasses.field(default_factory=list)
    parameters: list[Named] = dataclasses.field(default_factory=list)
    """``NAME=VALUE``, from ``-G``."""
    library_extensions: list[Named] = dataclasses.field(default_factory=list)
    timescale: Named | None = None
    blackboxes: list[Named] = dataclasses.field(default_factory=list)
    ignored: list[Ignored] = dataclasses.field(default_factory=list)
    variables: dict[str, str] = dataclasses.field(default_factory=dict)
    """Each environment variable expanded, with its value."""
    read_at: str | None = None
    """Where Reader.read_file() read its filelist, links resolved; None if it read none."""

    def of_kind(self, kind: Kind) -> list[Entry]:
        return [e for e in self.entries if e.kind is kind]


@dataclasses.dataclass(frozen=True)
class _Option:
    action: str
    takes_value: bool = True
    comma_list: bool = False


#: slang's options that the reader acts on, by name without dashes; ``+`` names are the plus forms.
_OPTIONS = {
    **dict.fromkeys(("I", "include-directory", "+incdir"), _Option("include_dir", comma_list=True)),
    **dict.fromkeys(("D", "define-macro", "+define"), _Option("define")),
    **dict.fromkeys(("U", "undefine-macro"), _Option("undefine", comma_list=True)),
    **dict.fromkeys(("y", "libdir"), _Option("library_dir", comma_list=True)),
    **dict.fromkeys(("Y", "libext", "+libext"), _Option("library_extension", comma_list=True)),
    **dict.fromkeys(("v", "libfile"), _Option("library_file", comma_list=True)),
    "f": _Option("filelist", comma_list=True),
    "F": _Option("filelist_rebased", comma_list=True),
    "top": _Option("top", comma_list=True),
    "G": _Option("parameter"),
    "timescale": _Option("timescale"),
    "single-unit": _Option("single_unit", takes_value=False),
    "blackbox": _Option("blackbox"),
    # slang options with no bearing on which files make up the block.
    **dict.fromkeys(("std", "W", "compat", "j", "threads", "T", "timing", "L"), _Option("ignore")),
    **dict.fromkeys(
        (
            "allow-use-before-declare",
            "relax-enum-conversions",
            "relax-string-conversions",
            "allow-hierarchical-const",
            "allow-dup-initial-drivers",
            "lint-only",
            "ignore-unknown-macros",
        ),
        _Option("ignore", takes_value=False),
    ),
    "ignore-unknown-modules": _Option("unsupported", takes_value=False),
}

#: The actions whose value is a path, which a value from the command line names whole, commas included.
_PATH_ACTIONS = frozenset({"include_dir", "library_dir", "library_file", "filelist", "filelist_rebased"})


@dataclasses.dataclass(frozen=True)
class _SimulatorOption:
    arguments: int = 0
    """How many arguments follow the option when its value isn't joined to it with ``=``."""
    rename: str | None = None
    """The name in _OPTIONS this option means, if it isn't ignored."""
    keyword: bool = False
    """Whether the next argument is its own when it is a keyword, as ``svaext`` is in VCS's ``-assert svaext``, rather
    than a file."""


#: Options of simulators and linters that slang doesn't take, by name as written up to any ``=`` or second ``+``.
SIMULATOR_OPTIONS = {
    # Read as the slang option they mean.
    "-incdir": _SimulatorOption(1, "I"),
    "-define": _SimulatorOption(1, "D"),
    "--top-module": _SimulatorOption(1, "top"),
    # VCS's -override_timescale=<v>, as a default for design elements without a `timescale, which clears slang's error
    # for a design that mixes them. Xcelium's bare -override_timescale applies its -timescale, which slang reads.
    "-override_timescale": _SimulatorOption(0, "timescale"),
    # Language and dialect switches; Aristotle reads every file as SystemVerilog.
    **dict.fromkeys(
        (
            "-sv",
            "-sverilog",
            "-sv2012",
            "-sv2009",
            "-v2k",
            "-v2k_generate",
            "-v2005",
            "-v200x",
            "-v93",
            "+v2k",
            "-mfcu",
            "-sfcu",
            "-disable_sem2009",
            "-relax",
            "-svinputport",
        ),
        _SimulatorOption(),
    ),
    **dict.fromkeys(("+systemverilogext", "+verilog2001ext", "+verilog1995ext"), _SimulatorOption()),
    # Build, run, debug, licensing and reporting.
    **dict.fromkeys(
        (
            "-64",
            "-64bit",
            "-full64",
            "-q",
            "-quiet",
            "-R",
            "-c",
            "-compile",
            "-elaborate",
            "-lca",
            "-kdb",
            "-debug_all",
            "-debug_pp",
            "-debug_access",
            "-linedebug",
            "-gui",
            "-licqueue",
            "-notice",
            "-status",
            "-lint",
            "-novopt",
            "-incr",
            "-uvm",
            "-Mupdate",
            "-error",
            "-suppress",
            "-xprop",
            "--cc",
            "--binary",
            "--exe",
            "--build",
            "--trace",
            "--timing",
            "--no-timing",
            "+lint",
            "+warn",
            "+nowarn",
            "+error",
            "+acc",
            "+access",
            "+libverbose",
            "+librescan",
            "+notimingcheck",
            "+notimingchecks",
            "-notimingchecks",
            "+nospecify",
            "+no_notifier",
            "+delay_mode_zero",
            "+delay_mode_unit",
            "+delay_mode_path",
            "+delay_mode_distributed",
            "+maxdelays",
            "+mindelays",
            "+typdelays",
            "+vcs",
            "+cli",
            "+memcbk",
            "-nocopyright",
            "-noIncrComp",
            "-Mdir",
            "-sim_res",
            "-covoverwrite",
        ),
        _SimulatorOption(),
    ),
    **dict.fromkeys(("-assert", "-xlrm", "-coverage", "-cover"), _SimulatorOption(keyword=True)),
    **dict.fromkeys(
        (
            "-access",
            "-l",
            "-log",
            "-o",
            "-work",
            "-xmlibdirname",
            "-nowarn",
            "-input",
            "-errormax",
            "-uvmhome",
            "-ntb_opts",
            "-cm",
            "-cm_dir",
            "-snapshot",
            "-CFLAGS",
            "-LDFLAGS",
            "-P",
            "-cm_name",
            "-cm_hier",
            "-modelsimini",
        ),
        _SimulatorOption(1),
    ),
}

#: Simulator options written with a code joined to the name, such as VCS's ``+nowarnTFIPC``.
_SIMULATOR_PREFIXES = ("+nowarn",)

#: A simulator keyword argument, such as ``svaext``, ``svaext+enable_diag`` or ``b:e:f``, which no file name with an
#: extension matches.
_KEYWORD = re.compile(r"[A-Za-z_][\w:]*(=[^\s+]*)?(\+[A-Za-z_][\w:]*(=[^\s+]*)?)*")

#: What _lookup() returns for a simulator option whose next argument is its own if it is a keyword.
_TAKES_KEYWORD = -1

_SIMULATOR = "a simulator option"
_NO_BEARING = "doesn't change which files make up the block"

_WHITESPACE = frozenset(" \t\n\r\v\f\0")
_NEWLINE = frozenset("\r\n")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_]+")


@dataclasses.dataclass(frozen=True)
class _Token:
    text: str
    origin: Origin


class _Tokens:
    """Arguments built as slang's command-line tokenizer builds them."""

    def __init__(self) -> None:
        self.tokens: list[_Token] = []
        self._current: list[str] = []
        self._origin: Origin | None = None

    @property
    def pending(self) -> bool:
        return self._origin is not None

    def start(self, origin: Origin) -> None:
        if self._origin is None:
            self._origin = origin

    def append(self, text: str, origin: Origin) -> None:
        self.start(origin)
        self._current.append(text)

    def finish(self) -> None:
        if self._origin is not None:
            self.tokens.append(_Token("".join(self._current), self._origin))
            self._current = []
            self._origin = None


@dataclasses.dataclass(frozen=True)
class _Base:
    """The folder relative paths are joined to: as named, and resolved."""

    named: str
    real: str

    @classmethod
    def of(cls, folder: Path) -> "_Base":
        return cls(os.path.normpath(os.path.abspath(folder)), os.path.realpath(folder))


class Reader:
    """Reads filelists and command-line arguments into one Filelist, in order."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        check: Callable[[Path, Path, Origin], None] | None = None,
        no_variables: str | None = None,
    ) -> None:
        """``check`` is called with each filelist, as named and resolved, before it is read, and raises FilelistError
        to refuse it; with ``no_variables``, the reason, a variable is an error instead of expanded."""
        self.result = Filelist()
        self._environ = os.environ if environ is None else environ
        self._check = check
        self._no_variables = no_variables
        self._reading: list[str] = []
        self._seen: set[tuple[Kind, str]] = set()

    def read_file(self, path: Path) -> None:
        """Reads the filelist at ``path``, a path and not a pattern, as ``-F`` does, relative to its own folder.

        Raises:
            FilelistError: the filelist, or one it names, can't be read, names a missing file or a path never_read()
                refuses, or uses an option Aristotle doesn't know.
        """
        self._read_filelist(str(path), _Base.of(Path.cwd()), None, rebase=True)

    def read_arguments(self, arguments: Sequence[str], cwd: Path) -> None:
        """Reads ``arguments``, as a shell passes them, with each path as written, not a pattern or a comma list,
        relative ones from ``cwd``.

        Raises:
            FilelistError: as read_file() does.
        """
        self._parse([_Token(a, Origin()) for a in arguments], _Base.of(cwd))

    def _parse(self, tokens: list[_Token], base: _Base) -> None:
        expecting: tuple[_Option, _Token] | None = None
        ignoring: list[str] = []
        ignoring_origin = Origin()
        ignoring_reason = ""
        skip = 0
        keyword: _Token | None = None
        positional_only = False
        for token in tokens:
            text = token.text
            if keyword is not None:
                option_token, keyword = keyword, None
                if _KEYWORD.fullmatch(text):
                    self._ignore(f"{option_token.text} {text}", option_token.origin, _SIMULATOR)
                    continue
                self._ignore(option_token.text, option_token.origin, _SIMULATOR)
            if skip:
                ignoring.append(text)
                skip -= 1
                if not skip:
                    self._ignore(" ".join(ignoring), ignoring_origin, ignoring_reason)
                continue
            if expecting is not None:
                option, named = expecting
                expecting = None
                self._apply(option, f"{named.text} {text}", text, named.origin, base)
                continue
            if len(text) <= 1 or positional_only or text[0] not in "-+":
                self._source(text, token.origin, base)
                continue
            if text == "--":
                positional_only = True
                continue
            found = self._lookup(text, token.origin)
            if isinstance(found, int):
                if found == _TAKES_KEYWORD:
                    keyword = token
                elif found:
                    ignoring, ignoring_origin, ignoring_reason, skip = [text], token.origin, _SIMULATOR, found
                else:
                    self._ignore(text, token.origin, _SIMULATOR)
                continue
            option, values = found
            if option.action == "ignore" and option.takes_value and values is None:
                ignoring, ignoring_origin, ignoring_reason, skip = [text], token.origin, _NO_BEARING, 1
            elif values is None and option.takes_value:
                expecting = (option, token)
            elif option.action == "ignore":
                self._ignore(text, token.origin, _NO_BEARING)
            elif values is None:
                self._apply(option, text, None, token.origin, base)
            else:
                for value in values:
                    self._apply(option, text, value, token.origin, base)
        if keyword is not None:
            self._ignore(keyword.text, keyword.origin, _SIMULATOR)
        if expecting is not None:
            raise FilelistError(f"{expecting[1].origin}: {expecting[1].text} needs a value")
        if skip:
            raise FilelistError(f"{ignoring_origin}: {ignoring[0]} needs a value")

    def _lookup(self, text: str, origin: Origin) -> tuple[_Option, list[str] | None] | int:
        """``text``'s option and its joined values, or how many arguments a simulator option it names takes."""
        if text[0] == "+":
            end = text.find("+", 2)
            name = text if end < 0 else text[:end]
            if name in _OPTIONS:
                values = [] if end < 0 else [v for v in text[end + 1 :].split("+") if v]
                if not values and _OPTIONS[name].takes_value:
                    raise FilelistError(f"{origin}: {text} needs a value")
                return _OPTIONS[name], values
            return self._simulator(text, re.split(r"[+=]", text[1:], maxsplit=1)[0], origin, "+")
        long = text.startswith("--")
        rest = text[2:] if long else text[1:]
        name, equals, value = rest.partition("=")
        if name in _OPTIONS:
            return _OPTIONS[name], ([value] if equals and value else None)
        simulator_name = re.split(r"[+=]", rest, maxsplit=1)[0]
        prefix = "--" if long else "-"
        if prefix + simulator_name in SIMULATOR_OPTIONS:
            return self._simulator(text, simulator_name, origin, prefix)
        if not long and rest[0] in _OPTIONS and _OPTIONS[rest[0]].takes_value:
            return _OPTIONS[rest[0]], [rest[1:].removeprefix("=")]
        raise FilelistError(f"{origin}: {text} is not an option Aristotle knows")

    def _simulator(self, text: str, name: str, origin: Origin, prefix: str) -> tuple[_Option, list[str] | None] | int:
        option = SIMULATOR_OPTIONS.get(prefix + name)
        if option is None:
            if (prefix + name).startswith(_SIMULATOR_PREFIXES):
                return 0
            raise FilelistError(f"{origin}: {text} is not an option Aristotle knows")
        joined = text[len(prefix) + len(name) :]
        if option.rename is not None:
            value = joined.removeprefix("=")
            if not value and not option.arguments:
                # A switch elsewhere, as Xcelium's -override_timescale is, which takes no argument.
                return 0
            return _OPTIONS[option.rename], ([value] if value else None)
        if joined:
            return 0
        return _TAKES_KEYWORD if option.keyword else option.arguments

    def _ignore(self, text: str, origin: Origin, reason: str) -> None:
        self.result.ignored.append(Ignored(text, origin, reason))

    def _apply(self, option: _Option, written: str, value: str | None, origin: Origin, base: _Base) -> None:
        """Acts on ``option`` with ``value``, split at commas for a comma list unless it is a path the command line
        names; ``written`` is the option and value as the filelist writes them."""
        split = option.comma_list and not (origin.file is None and option.action in _PATH_ACTIONS)
        values: list[str | None] = [*value.split(",")] if value is not None and split else [value]
        for item in values:
            getattr(self, f"_{option.action}")(written, item, origin, base)

    def _include_dir(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        _refuse_unread(value, origin, "folder")
        found = self._glob(value, base, folders=True, literal=origin.file is None)
        if not found:
            self._ignore(name, origin, "no such folder" if name.endswith(value) else f"{value}: no such folder")
        for path, real in found:
            self._add(Kind.INCLUDE_DIR, path, real, origin)

    def _library_dir(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        _refuse_unread(value, origin, "folder")
        for path, real in self._require(value, base, origin, folders=True, name=name):
            self._add(Kind.LIBRARY_DIR, path, real, origin)

    def _library_file(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        _refuse_unread(value, origin, "file")
        for path, real in self._require(value, base, origin, folders=False, name=name):
            self._add(Kind.LIBRARY_FILE, path, real, origin)

    def _source(self, value: str, origin: Origin, base: _Base) -> None:
        if value in ("", "-"):
            raise FilelistError(
                f"{origin}: {'an empty file name' if not value else '- (standard input)'} names no file"
            )
        _refuse_unread(value, origin, "file")
        for path, real in self._require(value, base, origin, folders=False):
            self._add(Kind.SOURCE, path, real, origin)

    def _define(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        self.result.defines.append(Named(value, origin))

    def _undefine(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        self.result.undefines.append(Named(value, origin))

    def _library_extension(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        self.result.library_extensions.append(Named(value, origin))

    def _top(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        self.result.tops.append(Named(value, origin))

    def _parameter(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        if "=" not in value:
            raise FilelistError(f"{origin}: {name} needs the form NAME=VALUE")
        self.result.parameters.append(Named(value, origin))

    def _timescale(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        if self.result.timescale is None:
            self.result.timescale = Named(value, origin)
        else:
            self._ignore(name, origin, f"the --timescale at {self.result.timescale.origin} applies")

    def _single_unit(self, name: str, value: str | None, origin: Origin, base: _Base) -> None:
        # The generated aristotle.f always holds --single-unit, and a committed one goes up as it is.
        if value not in (None, "true", "True", "false", "False"):
            raise FilelistError(f"{origin}: {name} takes no value")

    def _blackbox(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        self.result.blackboxes.append(Named(value, origin))

    def _unsupported(self, name: str, value: str | None, origin: Origin, base: _Base) -> None:
        raise FilelistError(
            f"{origin}: {name} can't be sent: Aristotle needs a source for every module the design instantiates. Drop "
            "the option, then add the missing sources, or name each missing module with --blackbox <module> for "
            "Aristotle to treat as a black box with unconstrained outputs."
        )

    def _filelist(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        _refuse_unread(value, origin, "filelist")
        self._read_filelist(value, base, origin, rebase=False, name=name)

    def _filelist_rebased(self, name: str, value: str, origin: Origin, base: _Base) -> None:
        _refuse_unread(value, origin, "filelist")
        self._read_filelist(value, base, origin, rebase=True, name=name)

    def _read_filelist(
        self, value: str, base: _Base, origin: Origin | None, *, rebase: bool, name: str | None = None
    ) -> None:
        """Reads the filelist ``value`` names at ``origin``, None for the one read_file() reads."""
        for path, real in self._require(value, base, origin, folders=False, name=name):
            if real in self._reading:
                raise FilelistError(f"{_at(origin)}{shown(Path(path))} names itself, through the filelists it names")
            if self._check is not None:
                self._check(Path(path), Path(real), Origin() if origin is None else origin)
            try:
                # A byte order mark, as some Windows editors write, isn't part of the first argument.
                text = Path(real).read_bytes().decode("utf-8", "surrogateescape").removeprefix("\ufeff")
            except OSError as e:
                raise _cant_read(path, origin, e) from e
            if origin is None:
                self.result.read_at = real
            tokens = _Tokens()
            self._tokenize(text, Path(path), tokens, expand=True, at=None)
            tokens.finish()
            self._reading.append(real)
            try:
                self._parse(tokens.tokens, _Base(os.path.dirname(path), os.path.dirname(real)) if rebase else base)
            finally:
                self._reading.pop()

    def _add(self, kind: Kind, path: str, real: str, origin: Origin) -> None:
        """Adds an entry once: a file per real file, as slang loads one, and a folder per name."""
        key = (kind, real if kind in (Kind.SOURCE, Kind.LIBRARY_FILE) else path)
        if key not in self._seen:
            self._seen.add(key)
            self.result.entries.append(Entry(kind, Path(path), Path(real), origin))

    def _require(
        self, value: str, base: _Base, origin: Origin | None, *, folders: bool, name: str | None = None
    ) -> list[tuple[str, str]]:
        """The files, or ``folders``, ``value`` names, from option ``name`` as written if it's an option's: a pattern
        only when a filelist names it.

        Raises:
            FilelistError: it names none, or one it lacks the permission to look up, which is named as unreadable.
        """
        literal = origin is None or origin.file is None
        found = self._glob(value, base, folders=folders, literal=literal)
        if found:
            return found
        what = "folder" if folders else "file"
        if _has_wildcards(value) and not literal:
            raise FilelistError(f"{_at(origin)}{value} matches no {what}")
        path, real = _resolve(base, value)
        try:
            os.stat(real)
        except PermissionError as e:
            raise _cant_read(path, origin, e) from e
        except OSError:
            pass
        if os.path.lexists(real):
            if not folders and value.endswith(("/", os.sep)) and os.path.isfile(real):
                raise FilelistError(f"{_at(origin)}{value}: {shown(Path(path))} is a file; drop the trailing slash")
            raise FilelistError(f"{_at(origin)}{shown(Path(path))} is not a {what}")
        if name is not None and name[:1] == "-" and name[1:2] != "-" and name[2:] == value:
            # slang reads an unknown -vXXX as -v XXX; the option is likelier a simulator's.
            raise FilelistError(
                f"{_at(origin)}{name} is not an option Aristotle knows, and read as {name[:2]} {value} it names no "
                f"{what}"
            )
        raise FilelistError(f"{_at(origin)}{shown(Path(path))}: no such {what}")

    def _glob(self, value: str, base: _Base, *, folders: bool, literal: bool) -> list[tuple[str, str]]:
        """What ``value`` names, as written if ``literal``, else as slang's svGlob expands it: each match as named and
        resolved, sorted."""
        pattern = os.path.normpath(value) + (os.sep if value.endswith(("/", os.sep)) and not folders else "")
        if literal or (not _has_wildcards(pattern) and not pattern.endswith(os.sep)):
            path, real = _resolve(base, pattern)
            # A trailing separator names a folder.
            kept = os.path.isdir(real) if folders else os.path.isfile(real) and not pattern.endswith(os.sep)
            return [(path, real)] if kept else []
        root, relative = base, pattern
        if os.path.isabs(pattern):
            drive, rest = os.path.splitdrive(pattern)
            root, relative = _Base.of(Path(drive + os.sep)), rest.lstrip(os.sep)
        matches = sorted(_glob_relative(root.real, relative.split(os.sep), folders))
        return [_resolve(root, m) for m in matches]

    def _tokenize(self, text: str, file: Path, out: _Tokens, *, expand: bool, at: Origin | None) -> None:
        """Splits ``text`` into ``out`` as slang's CommandLine::tokenize does, with comments and variables; in a
        variable's value (``expand`` False), a backslash is kept, as in a Windows path."""
        i, n, line = 0, len(text), 1

        def here() -> Origin:
            return at or Origin(file, line)

        while i < n:
            c = text[i]
            i += 1
            if c in _WHITESPACE:
                out.finish()
                line += c == "\n"
                continue
            if c == "#" or (c == "/" and not out.pending and i < n and text[i] == "/"):
                out.finish()
                while i < n and text[i] not in _NEWLINE:
                    i += 1
                continue
            if c == "/" and not out.pending and i < n and text[i] == "*":
                out.finish()
                end = text.find("*/", i + 1)
                end = n if end < 0 else end + 2
                line += text.count("\n", i, end)
                i = end
                continue
            if c == "$" and expand and i < n:
                origin = here()
                value, i = self._variable(text, i, origin)
                self._tokenize(value, file, out, expand=False, at=origin)
                continue
            if c == "\\" and expand:
                if i < n and text[i] not in _NEWLINE:
                    out.append(text[i], here())
                    i += 1
                continue
            out.start(here())
            if c == "'":
                end = text.find("'", i)
                end = n if end < 0 else end
                out.append(text[i:end], here())
                line += text.count("\n", i, end)
                i = min(end + 1, n)
                continue
            if c == '"':
                while i < n:
                    c = text[i]
                    i += 1
                    if c == '"':
                        break
                    if c == "\\" and i < n and text[i] in '\\"':
                        c = text[i]
                        i += 1
                    elif c == "$" and expand and i < n:
                        value, i = self._variable(text, i, here())
                        out.append(value, here())
                        continue
                    line += c == "\n"
                    out.append(c, here())
                continue
            out.append(c, here())

    def _variable(self, text: str, i: int, origin: Origin) -> tuple[str, int]:
        """The value of the variable whose name starts at ``text[i]``, after a ``$``, and the index past it."""
        c = text[i]
        if c in "({":
            close = text.find(")" if c == "(" else "}", i + 1)
            if close < 0:
                return "$" + text[i:], len(text)
            name, written, end = text[i + 1 : close], f"${text[i : close + 1]}", close + 1
            if name in ("", ".", "..") or "/" in name or "\\" in name:
                # The name names the file's _ext folder in the upload: .. would put it outside _ext.
                raise FilelistError(f"{origin}: {written} can't name a folder of the upload, as a variable's name must")
        else:
            match = _IDENTIFIER.match(text, i)
            if match is None:
                return "$" + c, i + 1
            name, written, end = match.group(), f"${match.group()}", match.end()
        if self._no_variables is not None:
            raise FilelistError(f"{origin}: {written} isn't expanded, {self._no_variables}")
        value = self._environ.get(name)
        if not value:
            state = "is empty" if value == "" else "is not set"
            raise FilelistError(f"{origin}: the environment variable {written} {state}")
        self.result.variables[name] = value
        return value, end


def _at(origin: Origin | None) -> str:
    """``origin`` as a message's prefix; none for the filelist Reader.read_file() reads, which the message names."""
    return "" if origin is None else f"{origin}: "


def _cant_read(path: str, origin: Origin | None, error: OSError) -> FilelistError:
    """The error saying ``path``, named at ``origin``, can't be read, and ``error``'s reason."""
    reason = f"read {shown(Path(path))}: {error.strerror or error}"
    return FilelistError(f"Can't {reason}" if origin is None else f"{origin}: can't {reason}")


def _has_wildcards(pattern: str) -> bool:
    return "*" in pattern or "?" in pattern or "..." in pattern


def _resolve(base: _Base, value: str) -> tuple[str, str]:
    """``value`` joined to ``base``: as named, normalized, and as reached, where a ``..`` after a link leads."""
    real = os.path.realpath(os.path.join(base.real, os.path.normpath(value)))
    path = os.path.normpath(os.path.join(base.named, value))
    return (path if os.path.realpath(path) == real else real), real


def _matches(name: str, pattern: str) -> bool:
    """Whether ``name`` matches ``pattern``, where ``*`` is any run of characters and ``?`` any one, as in slang."""
    return wildcard.matches(_steps(pattern), name)


@functools.lru_cache(maxsize=64)
def _steps(pattern: str) -> tuple[wildcard.Run | re.Pattern[str], ...]:
    """``pattern``, a segment of a path in a filelist, as wildcard.matches() takes it."""
    return wildcard.steps([wildcard.Run.ANY if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern])


def _listdir(folder: str) -> list[str]:
    try:
        return os.listdir(folder)
    except OSError:
        return []


def _glob_relative(folder: str, segments: list[str], folders: bool) -> list[str]:
    """Paths under ``folder``, relative to it, matching ``segments``; an empty last segment means every file there."""
    head, rest = segments[0], segments[1:]
    if head.startswith("..."):
        found: list[str] = []
        for sub in ["", *_subfolders(folder)]:
            tail = [head[3:].lstrip(os.sep), *rest] if head[3:] else rest or [""]
            for match in _glob_relative(os.path.join(folder, sub), tail, folders):
                found.append(os.path.join(sub, match) if sub else match)
        return found
    if not rest:
        if head == "":
            if folders:
                return [""] if os.path.isdir(folder) else []
            return [name for name in _listdir(folder) if os.path.isfile(os.path.join(folder, name))]
        if not _has_wildcards(head):
            target = os.path.join(folder, head)
            return [head] if (os.path.isdir(target) if folders else os.path.isfile(target)) else []
        is_kind = os.path.isdir if folders else os.path.isfile
        return [name for name in _listdir(folder) if _matches(name, head) and is_kind(os.path.join(folder, name))]
    if not _has_wildcards(head):
        return [os.path.join(head, m) for m in _glob_relative(os.path.join(folder, head), rest, folders)]
    found = []
    for name in _listdir(folder):
        if _matches(name, head) and os.path.isdir(os.path.join(folder, name)):
            found += [os.path.join(name, m) for m in _glob_relative(os.path.join(folder, name), rest, folders)]
    return found


def _subfolders(folder: str) -> list[str]:
    """Every folder below ``folder``, relative to it, each real folder once."""
    found: list[str] = []
    visited = {os.path.realpath(folder)}
    stack = [""]
    while stack:
        current = stack.pop()
        for name in sorted(_listdir(os.path.join(folder, current))):
            relative = os.path.join(current, name)
            real = os.path.realpath(os.path.join(folder, relative))
            if os.path.isdir(real) and real not in visited:
                visited.add(real)
                found.append(relative)
                stack.append(relative)
    return found


def read(
    path: Path, environ: Mapping[str, str] | None = None, check: Callable[[Path, Path, Origin], None] | None = None
) -> Filelist:
    """The filelist at ``path``, read as ``-F`` reads it, with ``check`` as Reader takes it.

    Raises:
        FilelistError: as Reader.read_file() does.
    """
    reader = Reader(environ, check=check)
    reader.read_file(path)
    return reader.result
