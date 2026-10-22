# Verilog build configuration

A Verilog design needs its include directories, defines, and library paths to elaborate.
If your repository commits an `aristotle.f` at its root (or at `--directory`), `aristotle ci` and
`aristotle review` point Aristotle at it as the build configuration. One git doesn't track isn't read
or uploaded, and they say so. Other `.f` / `.flist` files
are offered to Aristotle as candidates, to use if one covers the design. Otherwise the build
configuration is inferred on the server from the sources in the upload; nothing is derived on the
build machine and nothing is written into your checkout.

Commit an `aristotle.f` when the design's top module, defines, include directories or parameters
cannot be read off the source tree. It is the only place for them in `aristotle ci` and
`aristotle review`, which take no `--top` or `-D`. When `aristotle submit` sends a Verilog design,
from an `aristotle.f` in `--project-dir` or from `--top`, `-f` and `-F`, it adds a line to the prompt
pointing Aristotle at the `aristotle.f` that goes up, and one naming any black boxes, as `ci` and
`review` do.

## How `aristotle.f` is read

`aristotle submit --project-dir`, `aristotle ci` and `aristotle review` read `aristotle.f` as a
simulator reads a filelist, so the one your flow writes usually works as it is:

- Nested filelists, to any depth: paths in a `-f` file are relative to the folder in force, which is
  the folder holding `aristotle.f` at the top, and paths in a `-F` file are relative to that file.
- `+incdir+`, `+define+`, `+libext+`, `-v`, joined forms such as `-Irtl/include` and `--top=soc_top`,
  comma lists, `#`, `//` and `/* */` comments, quotes, absolute paths, and environment variables
  written `$VAR`, `${VAR}` or `$(VAR)`. A variable that isn't set, or is empty, stops the command,
  naming its line, as does a name that can't name a folder, such as `${..}`. `aristotle ci` and `aristotle review` expand none: a variable stops the step, so a
  change can't send the job's environment.
- Simulator options that don't change which files make up the design, such as `-sv`,
  `-access +rwc`, `-assert svaext` or `+nowarnTFIPC`, are listed and left out. Any other option
  stops the command, as does `--ignore-unknown-modules`: Aristotle needs a source for every module
  the design instantiates, so name each one it should treat as a black box instead, such as a
  foundry cell, with `--blackbox <module>` (see below).
- A file or filelist it names that isn't there stops the command before anything is sent.
- A network path, such as `\\host\share\x.v` or `//host/share/x.v`, a Windows device path, starting `\\?\`, `\\.\`
  or `\??\`, or a drive-relative path, such as `C:x.v`, stops the command, naming its line, before anything looks it
  up, on every platform, and so does an `` `include`` of one: on Windows, looking up a network path connects to the
  computer it names and signs in to it.

## The design on the command line

`aristotle submit` also takes the design on the command line. `--top`, `-f` and `-F` each add a line
to the folder's `aristotle.f`, or make one, in the order given, with paths relative to the current
directory. Each `-f` or `-F` names one filelist by its path as written: unlike in a filelist's line, a
comma doesn't separate filelists there, and `*`, `?` and `...` are part of the name, not wildcards.
A `--top` replaces any top module the filelists name, and the ones it replaces are listed.
`--top` and `--blackbox` need a filelist that names a source file, the folder's `aristotle.f` or one
`-f` or `-F` names: without one, `submit` stops with `2` before anything is sent. Pass one with `-f`,
which can be kept outside the folder, as under [Black boxes](#black-boxes). A folder with no filelist
can instead go up with `--project-dir` alone, with the top module named in the prompt.
Without `--project-dir`, the design's files are all that goes up, laid out from the current
directory:

```
aristotle submit "Prove that sum is a+b, a cycle later" --top alu_top -F $WORKAREA/design/alu/alu.f
```

The shell expands `$WORKAREA` there, so the CLI sees only the path: files from outside the current directory go up
under `_ext/`, in a folder named after the one they share, such as `_ext/alu/`. A variable the CLI reads itself, in a
filelist's line, names that folder instead, as in
[Includes, and files from outside the folder](#includes-and-files-from-outside-the-folder).

## Black boxes

`--blackbox <module>` has Aristotle treat a module as a black box with unconstrained outputs, such
as a foundry cell or a behavioural model you don't want to send. It works at any depth of
`aristotle.f`, and on `submit`'s command line, where it can repeat and, like `--top`, needs a
filelist that names a source file. The CLI writes no stub:
Aristotle writes what the proof needs. Each file the filelists name, `-v` files included, that
declares the module and no other design unit (a `module`, `macromodule`, `primitive`, `interface`,
`program`, `package`, `checker` or `config`) stays out of the upload, and so does such a `.v`, `.sv`,
`.vh` or `.svh` file they don't name, in a folder that goes up whole. A line added to the prompt
names every black box. A module no file declares, such as a
vendor cell with no source in your tree, gets that line alone. The CLI finds a file's design units
without elaborating it: comments and strings aside, every declaration counts, in both branches of an
`` `ifdef `` too, though not a unit a macro expands to, and a model encrypted whole declares none it
can read, so take such a file out of the filelists, and out of a folder that goes up whole,
yourself. A file left out takes the rest of what
it holds with it, such as a `` `define `` or a `typedef` outside its modules, and what the files it
includes declare. A file that declares a black box beside another design unit stops the command
before anything is sent, and names the others: move the module to a file of its own, or black-box
them too. A header the design includes that declares one stops it too, since the include brings it
back: drop the `` `include `` and name the file in the filelists instead. So does a black box that
names the top module. The `aristotle.f` that goes up is then generated, without `--blackbox`, which
Aristotle doesn't read, so like any generated one it can't use `-y` yet. `aristotle ci` and
`aristotle review` take `--blackbox` in the root `aristotle.f` the same way, and a review verifies
no file a black box leaves out. `review` reads the block only when it reviews the whole checkout or
Verilog files, and `aristotle ci setup` never does: those send such a file with the rest of the
checkout, so keep one that mustn't be sent out of the repository, or match it with a `.gitignore`
rule.

With this `aristotle.f`, `vendor/sram_1p.v`, which declares only `sram_1p`, stays out of the upload, and
Aristotle treats `sram_1p` as a black box:

```
--top soc_top
--blackbox sram_1p
-F rtl/soc.f
vendor/sram_1p.v
```

For a folder with no filelist, keep one outside it that names the design's sources by their paths
from the current directory, and pass it with `-f`: the model stays out of the upload, and nothing is
written into the repository. With `../top.f` holding the line `rtl/top.v`:

```
aristotle submit "Prove that q is stable" --project-dir . -f ../top.f --blackbox sram_1p
```

## See what goes up

`--dry-run` on `submit`, `ci` and `review` lists every file of the design with where it comes from
and why it goes up, each black box with the files it leaves out, each rewritten include and each
option left out, and prints the `aristotle.f` that would go up and the prompt's line naming any
black boxes. `submit --dry-run` also lists the folder's other files that go up, marking those
`--include-ignored` adds, and those an ignore rule leaves out; like `submit`, it stops with `2` when
nothing would go up. It sends nothing and needs no key.

## Includes, and files from outside the folder

The CLI follows each `` `include`` to the header it names, searching the including file's folder and
then each `-I` folder in order, as slang does. Every file the design needs goes up, even if
`.gitignore` excludes it; for `aristotle ci` and `aristotle review` it, and what a link leads to,
must be tracked by git in the folder the step uploads, or be in a folder `--allow-untracked` names,
or the step stops. A file from outside the
folder goes up under `_ext/`, in a folder named after the variable that leads to it, keeping the
folders below: `$WORKAREA/design/alu/alu.v` goes up as `_ext/WORKAREA/design/alu/alu.v`, so relative
includes still find their headers. A file no variable in the filelists leads to goes under the name
of the folder such files share. A file reached through a link in the folder keeps its place there,
as the build reads it. Each file from outside the folder, and each reached through a link out of it, is listed
with where it is before uploading: in a warning when the folder's own `aristotle.f` is read, which also
names each environment variable the filelists take, since someone else may have written it. A file
the design needs that is named like a credential or kept in a folder that holds them, under its own
name or the one a variable or a link gives it, that sits in a `.git` folder, or that has a name that
isn't UTF-8 stops the command, since leaving it out would break the build. So does one that would go
up where the folder already has a file or a folder. An `` `include`` by absolute path, or one that
would no longer find its header from where its file goes up, gets the header's path in the upload,
relative to the including file, in the uploaded copy only and on the same line, so line numbers
hold; your files aren't changed. One inside a `` `define`` keeps its name, since slang looks for it
from wherever the macro is used, and the CLI warns that it may reach another header there, or none;
one by absolute path there stops the command.

## Generated sources and shared folders

For a design whose sources the build generates, such as Verilog a preprocessor writes from
templates git tracks, or whose headers come from shared folders beside the one the step uploads,
run the build first, then name each folder those files are in:

```
aristotle ci --allow-untracked rtl/gen --allow-untracked ../macros --allow-untracked ../common
```

[Generated sources and shared folders](aristotle-in-ci.md#generated-sources-and-shared-folders), in the CI guide,
has what `--allow-untracked` lets through, and what it never does.

`aristotle submit --project-dir` leaves out what an ignore rule matches, as it usually does generated
sources, unless a filelist names them. Run the build, then add `--include-ignored` to upload them too:

```
aristotle submit "Prove the FIFO never overflows" --project-dir rtl --include-ignored --dry-run
```

It uploads the files in the folder that a `.gitignore`, `.git/info/exclude` or your global excludes
file matches, or, outside a repository or in a folder git ignores, the folder's top-level
`.gitignore`, and `--dry-run` marks each file it adds. Files named like credentials or kept in a
folder that holds them, `.git` folders, result archives an earlier run saved and files whose names
aren't UTF-8 still stay out, as does a link that leads out of the folder, and the 100 MB limit
holds. Only the command line turns it on, never an `aristotle.f`.

## The `aristotle.f` that goes up

What goes up is an `aristotle.f` in the subset of the filelist syntax Aristotle reads, shown below.
The folder's own one goes up as it is when it's already in that subset, with every path inside the folder. Otherwise a
generated one goes up in its place, holding the top module, defines, parameters and timescale, the
`-I` folders that hold a file in the upload, and every source and `-v` file, by its path in the
upload. It also holds `--single-unit`, so that a macro one file defines reaches the files after it,
as a simulator reads a filelist. Aristotle reads a `-v` file as a source, so a filelist with one
needs `--top`. A filelist that needs generating can't use `-y` yet: name the library's files with
`-v` or on lines of their own instead. The generated file is UTF-8, so a top, define or parameter
that isn't stops the command. Aristotle reads `*` and `?` in its paths, and `...` at the start of a
folder's or file's name, as wildcards, as slang does, and splits an `-I` folder's path at its commas,
so a source, `-v` file or `-I` folder that it would name by such a path stops the command, naming
it: rename it. The subset, for a file written by hand:

```
# One option, or one source file, per line.
--top soc_top
-I rtl/include
-D DATA_WIDTH=32
-D ASSERT_ON
-G DEPTH=16
-y rtl/lib
-Y .sv
rtl/pkg/soc_pkg.sv
rtl/fifo.sv
rtl/arbiter.sv
"rtl/third party/uart.sv"
rtl/soc_top.sv
```

- A line holds one source file, or one option, a space, and its argument. The options are
  `--top <module>`, `-I` or `--include-directory <dir>`, `-D` or `--define-macro <name>[=<value>]`,
  `-U` or `--undefine-macro <name>`, `-y` or `--libdir <dir>`, `-Y` or `--libext <extension>`,
  `-G <parameter>=<value>` for a parameter of the top module, `--timescale <value>`, and
  `--single-unit`, which takes no argument.
- Paths are relative to the directory that holds `aristotle.f`, and must stay inside it. Name
  every file on a line of its own, with `/` between directories: there are no globs and no
  environment variables. The CLI uploads every file named.
- A line that starts with `#` or `//` is a comment. A comment cannot share a line with anything else.
- Double quotes keep a path or value with spaces in one piece. Inside quotes or not, `\"`, `\\`
  and `\ ` (a backslash and a space) stand for a quote, a backslash and a space.
