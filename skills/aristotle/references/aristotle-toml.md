# aristotle.toml and `aristotle ci`

## The manifest

`aristotle ci` reads `aristotle.toml` in `--directory`, else at the root of the CI checkout; outside CI, the nearest
one from the current directory up to the repository's top level. `--manifest` names another file, and then, with no
`--directory`, the repository is checked from its top level.

```toml
[check]
max_wall_time_seconds = 1800   # optional time budget; the default is 60 minutes

[requested_properties.arbiter_one_grant]
kind = "natural_language"
property = "In module arbiter (rtl/arbiter.v), after reset, at most one bit of grant is high in every cycle."

[requested_properties.fifo_no_overflow]
kind = "sva"
identifier = "fifo.assert_no_overflow"   # a labelled concurrent assertion in the design that must hold
assumptions = "rst_n is held low for at least 64 cycles after power-up."

[requested_properties.fifo_push_no_ub]
kind = "c_function_no_ub"
identifier = "fifo_push"                 # a C function that must have no undefined behavior
```

| Key | Meaning |
|---|---|
| `[requested_properties.<id>]` | One property; at least one is required. The id uses letters, digits, `_` and `-`, names the test that reports the property, and should stay the same from run to run. |
| `kind` | Required: `natural_language`, `sva` or `c_function_no_ub`. |
| `property` | The statement in plain language. Only for `natural_language`. |
| `identifier` | One name without spaces. For `sva`, the labelled concurrent assertion that must hold (`<label>: assert property (...)`), named by its label, optionally after its module; state an immediate assertion (`assert #0`, `assert final`) or an unlabelled one as `natural_language`. For `c_function_no_ub`, the C function that must have no undefined behavior. |
| `assumptions` | Optional: what the property is checked under, in plain language, such as an answer to one of Aristotle's questions; the only place for one on an `sva` property. The log shows it with the verdict. |
| `check.max_wall_time_seconds` | The time budget in whole seconds, from 1 to 604800. `--timeout` (in minutes) overrides it. The default is 60 minutes. |
| `expect` | Optional, and only `"refuted"`: a deliberate counterexample. A refutation passes (`refuted, as expected`), and a proof fails (`unexpectedly proved: update the manifest`), usually because the bug was fixed. |
| `repo_id`, `check.max_cost_dollars`, `check.enable_proof_repair`, `property_result_id` | Accepted but not yet enforced. |

Validation is strict: an unknown key, a missing `kind`, a statement under the other kind's key, or a character that
isn't allowed in an id or `identifier` exits 2 before anything is uploaded, listing every problem under its dotted
key. The file is TOML 1.0: an inline table spread over several lines is rejected.

## What a run reports

Each manifest property gets one result under its id:

- `PASS`: proved, or, with `expect = "refuted"`, refuted.
- `FAIL`: refuted, or, with `expect = "refuted"`, proved. The log gives the source line,
  the property and the run's summary, which usually describes the counterexample.
- `SKIP`, with no verdict, and why: in progress (the run finished without checking it, or the time budget ran out
  with the run left going); unresolved (the run ended short of it, by failing, running out of budget, being cancelled
  (as `aristotle ci` does when its time budget runs out) or finishing without completing the request (as when
  Aristotle stops it for questions), or the proof no longer checks, so re-run to re-prove it);
  `not registered` (Aristotle registered nothing under the id, or withdrew it); or `not registered, run cut short`
  (the run ended first). A run that ran out of budget or time ended before Aristotle checked its results, so each
  proof or refutation it reported is a `SKIP` labelled `unchecked`. So is a proof from a run that failed or was
  cancelled; a refutation from one keeps its verdict.

Properties that Aristotle proposes on its own are listed as suggestions, and never change the exit code. To check one
on every run, add it to the manifest.

`--fail-on` decides what fails the build:

- `failure`, the default: a `FAIL`, no property in the manifest proved or refuted, or a run Aristotle stopped for
  decisions only the user can make. When the build passes with properties that have no verdict, the log names them.
- `no-verdict`: also any property with no verdict: in progress, unresolved or unchecked. `unresolved` is its old name,
  still accepted.
- `never`: exits 0 once a run finishes.

The exit codes:

- 0: the build passes.
- 1: the verdicts failed the build, or Aristotle stopped the run for decisions only the user can make, which the
  run's summary in the log lists.
- 2: no manifest, an invalid one, or an unusable upload (over 100 MB, or nothing to verify in it).
- 3: the run failed, was cancelled elsewhere, or ran out of budget or time, or Aristotle couldn't be reached.
- 5: no API key, a rejected one, or one that can't be sent: blank, or holding a space, a control or a non-ASCII
  character.
- 6: no run slot freed up in time.
- 7: the response to creating the run was lost and the run couldn't be found: check `aristotle list` before running
  again.

## How a run behaves

- The upload is the git-tracked files, minus those `.gitignore` excludes, those whose names aren't UTF-8, and those
  named like credentials or kept in a folder that holds them, such as `.env/` or `.ssh/`. The check matches names
  only, so a secret inside another tracked file goes up. Lean's build files go up whenever git tracks them. The
  upload is one compressed archive of at most 100 MB, which `aristotle ci --dry-run` builds and checks.
- Every run proves every property from scratch.
- When the account is at its limit of projects in progress, `aristotle ci` waits for a slot, out of its time budget,
  and exits 6 if none frees up in time, or if its run is still queued when the budget runs out.
- A SIGINT or SIGTERM, such as Ctrl-C or a CI job being cancelled, cancels the run, unless `--on-timeout leave` is set.
  So does running out of budget.
- `aristotle ci --project <id>` reports on a run already started, such as one left going with `--on-timeout leave`:
  it uploads nothing, waits within the time budget, reports as a new run would, and leaves the run going unless
  `--on-timeout cancel` is given. It exits 2 first, saying what differs and how to stop the run, unless
  `aristotle ci` started the project's only run on the files this checkout would upload and the same properties.
- It writes a JUnit report to `aristotle-results.xml`, unless you pass `--no-junit` or `--junit <path>`.
- Its project is named `<repository>[/<directory>]: ci @ <branch> <commit>`, with `+changes` when tracked files
  differ from the commit, unless `--name` sets another.

## Verilog: `aristotle.f`

`aristotle ci` and `aristotle review` take no `--top` or `-D`. A design that needs its top module, include directories
or defines needs an `aristotle.f` committed at the root of the folder the run checks: `--directory`; by default the
checkout in CI; outside CI, the manifest's folder for `aristotle ci` (the top level with `--manifest`), and the top
level for `aristotle review`. Aristotle uses it as the build configuration, and any other filelist only as a candidate.

- List the design's source files, with any file holding an assertion a property names but no testbenches, one per
  line, and the build's settings as `--top <module>` (the design's top module, not a testbench's), `-I <dir>`,
  `-D <name>=<value>` and `-G <parameter>=<value>` lines, with the build's values. It is read as a simulator reads a
  filelist, nested `-f`/`-F`, `-v`, `+incdir+`, `+define+` and joined forms such as `-Iinc` included, and every file
  it names or includes goes up. An environment variable in it stops the run, and so does a network path such as
  `\\host\share\x.v`, a Windows device path or a drive-relative one such as `C:x.v`, in it, in an include or as a
  folder `--allow-untracked` names.
- Each of those files, and any file a link among them leads to, must be tracked by git in the folder the run checks,
  unless `--allow-untracked <folder>` on the command line names a folder it is in. When the build generates files it
  names, or it includes headers from a shared folder outside that folder, run the build, then check with
  `aristotle ci --dry-run --allow-untracked <folder>`, once for each folder those files are in. A relative folder is
  from the current directory, so the CI job, after the step that builds the files, names them from where it runs,
  usually the checkout root: `aristotle ci --directory hw/fifo --allow-untracked hw/fifo/gen`. Only the command line
  gives them, never a file in the repository, so propose that change to the user.
  `aristotle submit --project-dir <dir> --include-ignored` uploads generated files an ignore rule matches, and its
  `--dry-run` marks each one.
- A `--blackbox <module>` line has Aristotle treat a module, such as a foundry cell, as a black box with unconstrained
  outputs: write no stub for it. A tracked file the filelists name, or a tracked `.v`, `.sv`, `.vh` or `.svh` file they
  don't, that declares nothing but black boxes stays out of the run's upload; name one in the filelists only if git
  tracks it. `aristotle ci setup`, and an `aristotle review` that checks no Verilog file, don't read `aristotle.f`, so
  they send such a file with the rest of the checkout: match one that mustn't be sent with a `.gitignore` rule, or keep
  it out of the repository.
- A new `aristotle.f` counts only once git tracks it: `git add` it with the change that adds it, asking first if the
  user wrote it. The dry run then says `Using the Verilog filelist committed at aristotle.f` and lists what goes up.
  Changes to a tracked one go up as they are on disk, without staging.

## Creating a manifest

- Write it by hand in the format above, then run `aristotle ci --dry-run`.
- `aristotle ci setup --from <project-id>` writes one from a finished project's properties, under the ids Aristotle
  chose, with a note above each saying what that run found. It writes `expect = "refuted"` for a property Aristotle
  says shows its counterexample on purpose: confirm each with the user. The note also flags a statement that may
  depend on that project's run, such as one mentioning "the input contract", or one fixing a parameter to a number
  the code never sets: check those. It refuses to replace an existing manifest unless you pass `--force`. From a
  subfolder it writes the manifest `aristotle ci` reads there, else a new one in that folder.
- `aristotle ci setup` with no `--from` starts a run that proposes properties and asks questions, at an interactive
  terminal only.

## `aristotle review`

`aristotle review` is the open-ended run: Aristotle works out what should hold of the files in scope, and checks it.
Its properties differ from run to run, so it is advisory, and `--fail-on` defaults to `never`. `--scope changed`, the
default, covers the source files (Verilog, C, Rust and Lean) this change touched, compared with `--base-ref` or the
branch the CI environment names, and every Lean file under a Lean build file it touched. Outside CI, without
`--base-ref`, it covers only the last commit, so for uncommitted work use `--scope paths --path '<glob>'`, which
uploads only tracked files: ask the user before you `git add` a new one. `--scope all` covers every source file.
Use the properties it proves as candidates for the manifest. It takes `aristotle ci`'s `--timeout`, `--on-timeout`,
`--junit`, `--no-junit`, `--directory`, `--name` and `--dry-run`, and it is paid just the same.
