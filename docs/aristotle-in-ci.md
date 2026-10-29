# Aristotle in your CI pipeline

## Formal verification as a merge gate

Aristotle proves the properties your team lists in `aristotle.toml` about your Verilog, C, and
Rust — on every pull request that could affect them, reported the same way your unit tests are.
Verilog and C are available on every account, and Rust on accounts where it's enabled.

No new dashboard to check. No specialist to schedule. If a change breaks a property your
team cares about, the build goes red and the engineer who wrote the change finds out on
the same pull request, in the tool they're already looking at.

---

## The problem this solves

Formal verification works. The reason most teams get so little out of it is scheduling, not
capability:

- **It happens too late.** A formal effort kicks off weeks after the RTL lands, so the
  engineer who introduced the bug has moved on and lost the context.
- **It needs a specialist.** Writing properties and discharging proofs is its own skill.
  Teams have two or three people who can do it, and those people are a queue.
- **The results live somewhere else.** Even a successful campaign produces a report in a
  separate tool that most of the team never opens.

The result is that formal is used defensively on a handful of critical blocks, once, and
then goes stale the moment the design changes.

CI is where this belongs. Your team already trusts a red build. The job is to make formal
results arrive there.

---

## How it works

**1. List the properties in `aristotle.toml`.** State each one in plain language, or name a
SystemVerilog assertion or a C function already in the code. Each gets an id. `aristotle ci setup`
has Aristotle propose a first list for you to review.

**2. Add one step to your pipeline.** One container image, the same one for every CI system.

**3. Aristotle proves each property against the checkout.** It formalizes the property and
attempts to prove it, or to refute it when it does not hold.

**4. Results come back as tests.** Each property is a test case named by its id. Proved is
green. A refuted property is red, and the run's summary usually describes the counterexample
Aristotle found; a property marked `expect = "refuted"` turns that around. Your CI renders it natively — no plugin, no new UI.

For an open-ended audit, where Aristotle works out what should hold instead of checking a list,
see [`aristotle review`](#aristotle-review).

---

## The manifest

`aristotle ci` reads `aristotle.toml` at the root of the repository, or of the subproject that
`working-directory` (`--directory`) names; run outside CI, it reads the nearest one from the current
directory up to the repository's top level, and with `--manifest` and no `--directory` it checks the
repository from its top level. Commit it with the code it describes. To read another file, set the Action's `manifest`
input, `--manifest` or `ARISTOTLE_MANIFEST`.

```toml
[check]
max_wall_time_seconds = 3600   # the time budget when timeout is not set

[requested_properties.arbiter_one_grant]
kind = "natural_language"
property = "The arbiter never grants two requesters in the same cycle."

[requested_properties.arbiter_fair]
kind = "natural_language"
property = "Every request is granted within 16 cycles."

[requested_properties.fifo_no_overflow]
kind = "sva"
identifier = "fifo.assert_no_overflow"

[requested_properties.fifo_push_no_ub]
kind = "c_function_no_ub"
identifier = "fifo_push"
```

| Key | Meaning |
|---|---|
| `[requested_properties.<id>]` | One property; at least one is required. The id uses letters, digits, `_` and `-`. It names the test that reports the property and stays the same from run to run, so GitLab and Jenkins keep a history per property. |
| `kind` | Required: `natural_language`, `sva` or `c_function_no_ub`. |
| `property` | The property in plain language. `natural_language` only. |
| `identifier` | One name, with no spaces: the labelled concurrent SystemVerilog assertion that must hold (`sva`), or the C function that must have no undefined behavior (`c_function_no_ub`). An `sva` property names an assertion written `<label>: assert property (...)`, by its label, optionally after its module (`fifo.a_no_overflow`). An immediate assertion (`assert (...)`, `assert #0`, `assert final`) or an unlabelled one can't be named: state it as `natural_language`. |
| `assumptions` | Optional. What the property is checked under, in plain language, such as an answer to one of Aristotle's questions. It goes to the run with the property, and the log, the JUnit report and the job summary show it with the verdict, as "holds, assuming …" for an ordinary pass. It's the only place for such answers on an `sva` property, whose statement is the assertion's name. |
| `check.max_wall_time_seconds` | The time budget in whole seconds, from 1 to 604800 (a week). It applies when the `timeout` input, `--timeout` and `ARISTOTLE_TIMEOUT` are all unset; when this is unset too, the budget is 60 minutes. |
| `expect` | Optional, and only `"refuted"`: a deliberate counterexample. A refutation of it passes, reported as `refuted, as expected`; a proof of it fails, as `unexpectedly proved: update the manifest`, since the counterexample is gone, usually because the bug was fixed. Without a checked verdict it has none, as any property. |
| `repo_id`, `check.max_cost_dollars`, `check.enable_proof_repair`, `property_result_id` | Accepted but not yet enforced: a manifest may set them, and the log lists the ones it sets. `repo_id` and a property's `property_result_id` are non-empty strings, `max_cost_dollars` a positive number, and `enable_proof_repair` `true` or `false`. |

The manifest is TOML 1.0, which the image's Python parses; syntax new in TOML 1.1, such as an
inline table spread over several lines, is rejected there with the usage-error code.

Validation is strict, so a typo cannot quietly drop a property from the gate. An unknown key at
any level is an error, and so is a missing `kind`, a statement under the key the other kinds use,
or an id with any other character. An invalid manifest exits with the usage-error code (`2`)
before anything is uploaded, and the error names every problem under its dotted key:

```
ERROR - /workspace/aristotle.toml is not valid:
  requested_properties.fifo_no_overflow.identifer: unknown key; expected one of kind, property, identifier, assumptions, expect, property_result_id
  requested_properties.fifo_no_overflow.identifier: missing; kind sva needs it
```

Without a manifest, the step also exits `2` before it uploads anything or looks for an API key:

```
ERROR - No aristotle.toml found. Write one, or have `aristotle ci setup` propose one: at a terminal, or from a finished project with --from <project>.
Looked for /workspace/aristotle.toml; pass --manifest to read another file.
```

Outside a git repository it says that instead.

To create one, run `aristotle ci setup` at a terminal in the repository. Aristotle reads the
code, asks you about anything it cannot settle (whether a counter should wrap or saturate, which
modules matter most), and proposes properties. The command writes them to `aristotle.toml`, each
under the id Aristotle chose and a comment naming the code it is about, and lists them. A property is `natural_language`,
and its statement names the file it is about, and the Verilog module, adding them when Aristotle
didn't; a list number from a prompt, as in `(1) No overflow: …`, is dropped, and so is a lead-in
from Aristotle's report, `Suggested:` or `Claim that`. A property on a
labelled SystemVerilog assertion already in the code is written as `sva`, naming that assertion,
when it says it is that assertion: its name ends with the label, or its description opens by
saying the assertion holds. What a property holds under, such as an answer you gave, goes under
its `assumptions`. Aristotle is asked to propose rather than prove, but it may check
some as it goes; the comment says when a property was already proved or refuted, or when
Aristotle expects it to be refuted, and the command warns about both, since `aristotle ci` would fail
on them. A property Aristotle says shows its counterexample on purpose, as a demonstration does,
is written with `expect = "refuted"` instead, unless a checked proof settles it, and the command
names it so you can make sure it is deliberate. A verdict the run never checked is noted as unchecked instead: any from a setup run that
ran out of budget, and a proof from one that failed or was cancelled. A refutation noted so draws
no warning unless Aristotle also expects the property to be refuted, or calls it a deliberate
counterexample. A property with both sentences isn't marked. Review and edit the file, check it
with `aristotle ci --dry-run`, which starts no run, then commit it:

```
$ aristotle ci setup
...
Task complete!
Wrote aristotle.toml with 6 properties:
  counter_rst_clears_count: In module `counter`, assuming `rst` is high in clock cycle 0: in every cycle t where `rst` is high at the rising edge of `clk`, `count` equals 4'd0 in cycle t+1, whatever the value of `en` (synchronous reset takes priority over enable). (In `counter.v`.)
  counter_wraps_at_15: In module `counter`, assuming `rst` is high in clock cycle 0: in every cycle t where `rst` is low, `en` is high and `count` equals 4'd15, `count` equals 4'd0 in cycle t+1 (the counter wraps around and does not saturate at 4'd15). (In `counter.v`.)
  ...
Its time budget is 60 minutes; the run it was written from took 12.
Review them and edit the file as you see fit, then check it without starting a run:
  aristotle ci --dry-run
and commit it:
  git add -- aristotle.toml
  git commit -m 'Add aristotle.toml'
```

`aristotle ci setup` runs only at a terminal, and never replaces an existing manifest unless you
pass `--force`. Run from a subfolder, it writes the manifest `aristotle ci` would read there: the nearest
`aristotle.toml` up to the repository's top level, which it won't replace without `--force`, else a
new one in that folder; `--directory .` sets up the folder itself. A CI job reads a manifest below
the top level only with `--directory`, so the command then prints the `aristotle ci` line the job
needs. Its project is named like a run's, as `rtl: ci setup @ main 1a2b3c4`, unless `--name` says
otherwise. The manifest's time budget, `check.max_wall_time_seconds`, is half as long again as
the run it is written from took (for `--from`, the run that last updated the properties it
copies, the longest if several did, which the comment names), and at least an hour; give the CI
job longer than that. A
GitHub-hosted job stops after 6 hours, so a budget past that needs a self-hosted runner or a lower
`max_wall_time_seconds`.
`--from <project>` (`ARISTOTLE_FROM`) writes the manifest from a finished project's properties
instead, without a run or a terminal: a project you worked on in the web app, or a setup run you
stopped following with Ctrl-C, which keeps going on the server, once it finishes. It copies their
statements as they are, and flags one that may depend on that project's run, such as one whose
statement or `assumptions` mention "the input contract", which may come from its conversation, or
fix a parameter to a number the code sets only to others, with a comment and a warning: check those
before you commit.

A manifest can also be written by hand in the format above. `aristotle ci --dry-run` reads it,
reports any problem, and lists the properties it would check, without submitting anything or needing an
API key.

---

## Get started

The GitLab and Jenkins recipes below pull the image `ghcr.io/harmonic-ai/aristotle:3`, which
each release publishes. The GitHub Action runs the same image at its own release's version, so
`harmonic-ai/aristotle@v3` runs the newest 3.x release and `@v3.1.0` stays on 3.1.0. The image is
built for x86-64 (`linux/amd64`) only, so a runner on Arm needs x86-64 emulation to run it.

Until Aristotle can re-check results it has already established, every `aristotle ci` run is a
full proving run of every property in the manifest, with the time and cost that implies. That is
why each recipe runs the step only when relevant files change: list the directories your
properties depend on, and `aristotle.toml` itself.

### GitHub Actions

```yaml
name: Formal verification
on:
  pull_request:
    paths:
      - "rtl/**"
      - "aristotle.toml"

concurrency:            # a new push cancels the jobs, and their runs, for the commit before it
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  verify:               # the gate: checks the properties in aristotle.toml
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: harmonic-ai/aristotle@v3
        with:
          api-key: ${{ secrets.ARISTOTLE_API_KEY }}

  review:               # advisory: what else should hold of the files this pull request changed
    needs: verify       # after the gate, pass or fail: side by side they'd compete for one run slot
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: harmonic-ai/aristotle@v3
        with:
          api-key: ${{ secrets.ARISTOTLE_API_KEY }}
          command: review
```

Results appear as annotations on the pull request, on the source line when the run records
one, plus a summary on the job page. An error that ends the step before it reports a run, such as
an invalid input or a manifest it can't find, is an error annotation too. The `verify` job is the gate. The `review` job runs
[`aristotle review`](#aristotle-review), which passes once its run finishes whatever the
verdicts, so leave it out of your required checks; drop the job if you only want the gate.

The action's `command` input is `ci` by default, or `review`. It also takes `manifest` for `ci`;
`scope`, `paths`, `base-ref` and `instructions` for `review`; and `fail-on`, `timeout`,
`on-timeout`, `junit`, `working-directory` and `project-name` for both. `fail-on`, `timeout` and
`on-timeout` are empty by default, which leaves them to the command: `ci` fails on `failure` and takes its
budget from the manifest's `max_wall_time_seconds`, while `review` fails on `never` and runs for
60 minutes, and both cancel their run when the budget runs out, except that `ci --project` leaves the run it
reports on going. It passes no other option, such as `--allow-untracked` (see
[Generated sources and shared folders](#generated-sources-and-shared-folders)).

The report goes to the `junit` path, `aristotle-results.xml` by default. Give it relative to the
top of the workspace, where the checkout is, not to `working-directory`: a subproject's report
needs the folder in its path, as in `junit: hw/fifo/aristotle-results.xml`.
The action runs in a container, and only what it writes in the workspace stays for later steps:
a path elsewhere, such as in `${{ runner.temp }}`, is written inside the container and gone when
the step ends. Once the report is written, the step sets its `junit` output to the path as
given, which a later step can read as `${{ steps.<id>.outputs.junit }}` to publish the report.

Each job is a run of its own, and both count against your account's limit of projects in
progress. Side by side, one would wait for the other out of its own time budget, and could run
out of it and exit `6`, even when it is the gate. That is why `review` runs after `verify`
(`needs`), whether the gate passed or failed (`if: ${{ !cancelled() }}`). The `concurrency` group
cancels both jobs, and their runs, when a new push to the pull request supersedes them.

That orders the jobs of one pull request only. A review holds a slot for up to its budget, and
another pull request's gate on the same key waits for it. A gate submits only while a quarter of
its budget is left, so a 60-minute gate waits at most 45 minutes. If your account allows one
project in progress, give the review job a shorter `timeout` (for example `timeout: 30`), or run
it on a key of another account, which has a limit of its own.

When a path filter skips the workflow, GitHub leaves its check pending, and a pull request
cannot merge while a required check is pending. If you make this check required, a pull request
that touches none of the paths cannot merge.

### GitLab CI

```yaml
formal-verification:
  image: ghcr.io/harmonic-ai/aristotle:3
  timeout: 90m  # longer than the manifest's time budget, 60 minutes here; GitLab's default, 60m, isn't
  rules:
    - changes:
        - rtl/**/*
        - aristotle.toml
  script:
    - aristotle ci
  artifacts:
    reports:
      junit: aristotle-results.xml
```

Results appear in GitLab's native merge-request test widget. Add your key as a masked CI/CD
variable named `ARISTOTLE_API_KEY`; GitLab puts it in the job environment, where the CLI
reads it.

`rules: changes` compares a merge request pipeline with its target branch, and a branch
pipeline with the commit the branch pointed at before the push. A pipeline with nothing to
compare with, such as a scheduled one or the first push of a new branch, runs the job.

### Jenkins

Store the key as a **Secret text** credential — `aristotle-api-key` below — and bind it to
`ARISTOTLE_API_KEY`, which is where the CLI looks for it.

```groovy
pipeline {
  agent { docker { image 'ghcr.io/harmonic-ai/aristotle:3' } }
  environment { ARISTOTLE_API_KEY = credentials('aristotle-api-key') }
  stages {
    stage('Checkout') {
      steps { checkout scm }
    }
    stage('Formal verification') {
      steps { sh 'aristotle ci' }
    }
  }
  post {
    always { junit allowEmptyResults: true, testResults: 'aristotle-results.xml' }
  }
}
```

Results appear in the standard Jenkins test-results view, including history and trend
graphs. `allowEmptyResults` matters: if the run cannot start at all, there is no report to
record, and without it Jenkins replaces the actual error in the log with a stack trace about
a missing file.

To run the stage only when relevant files change, give it a condition:

```groovy
    stage('Formal verification') {
      when {
        anyOf {
          changeset 'rtl/**'
          changeset 'aristotle.toml'
        }
      }
      steps { sh 'aristotle ci' }
    }
```

Jenkins takes the changeset from the commits since the job's previous build, so the first build
of a branch has an empty one and skips the stage.

### Anything else

The image runs anywhere containers run — CircleCI, Buildkite, Azure DevOps, TeamCity,
Tekton, or a bare `docker run` in a shell script. It emits standard JUnit XML and a
meaningful exit code, which is all any CI system needs. Use your CI's own path filter to
run it only when relevant files change.

Run the container directly and let your CI pick up the JUnit report:

```bash
docker run --rm -v "$PWD:/workspace" -w /workspace \
  -e ARISTOTLE_API_KEY \
  ghcr.io/harmonic-ai/aristotle:3 aristotle ci
```

To run the step in an image of your own instead, install git in it, and the CLI with pip:
`pip install "aristotlelib>=3"`, as the published image does, starting from the official
`python:3.12-slim`. A distribution's own Python, such as Debian 12's or Ubuntu 24.04's, refuses
a pip install outside a virtual environment: install the CLI in one, and put its `bin` folder
on `PATH`. For a
Jenkins job, also install `ps` and `kill`, which Jenkins runs in the container to stop a step:
without them, aborting the build leaves its run going. Debian has both in `procps`, which slim
images leave out. git refuses a checkout the job's user doesn't own, as when `docker run -v`
mounts one into a container that runs as root, until it is marked safe: run
`git config --global --add safe.directory <checkout>` first. The published image has `ps` and
`kill`, and marks every folder safe. The rest is as in the recipes above: give the job your API
key as `ARISTOTLE_API_KEY` from your CI's secret store, as [GitLab CI](#gitlab-ci) and
[Jenkins](#jenkins) do, and run `aristotle ci`. Its flags are in
[Flags and environment variables](#flags-and-environment-variables), and its time budget in
[Time and cost](#time-and-cost).

### Flags and environment variables

`aristotle ci --help` lists the flags. Every flag that takes a value also reads an environment variable
(`ARISTOTLE_MANIFEST`, `ARISTOTLE_TIMEOUT`, `ARISTOTLE_ON_TIMEOUT`, `ARISTOTLE_FAIL_ON`, `ARISTOTLE_JUNIT`,
`ARISTOTLE_DIRECTORY`, `ARISTOTLE_NAME`, `ARISTOTLE_PROJECT`), which is how the container is configured,
except `--allow-untracked`, which only the command line gives (see
[Generated sources and shared folders](#generated-sources-and-shared-folders)). The switches `--dry-run`
and `--no-junit` have no variable. `aristotle ci` takes no `--scope`, `--path`, `--base-ref` or
`--instructions`: those belong to [`aristotle review`](#aristotle-review). Given to `aristotle ci`, they
exit `2` with a message that says so, and `aristotle ci` warns when `ARISTOTLE_SCOPE`, `ARISTOTLE_PATHS`,
`ARISTOTLE_BASE_REF` or `ARISTOTLE_INSTRUCTIONS` is set.

### Generated sources and shared folders

The step uploads what git tracks in the folder it checks. When the build generates sources that the
root `aristotle.f` names, such as Verilog a preprocessor writes from templates git tracks, or the
design includes headers from shared folders outside that folder, run the build first, then name each
folder those files are in with `--allow-untracked`. Here a job of its own generates `gen/` and passes
it on as an artifact, and the block in `hw/fifo` includes headers from `hw/macros`:

```yaml
formal-verification:
  image: ghcr.io/harmonic-ai/aristotle:3
  needs: [generate]   # writes hw/fifo/gen/ and keeps it as an artifact
  script:
    - aristotle ci --directory hw/fifo --allow-untracked hw/fifo/gen --allow-untracked hw/macros
```

A relative folder is from the current directory, as `--directory` is: the checkout, here. A network
path, a Windows device path or a drive-relative path stops the step before anything looks it up, as in a
filelist. Files from
outside the folder the step checks go up under `_ext/`, and the log lists each file the option lets
through, with the folder that did, as `--dry-run` does. A container sees only what is mounted in it,
so a shared folder outside the checkout has to be mounted too. A folder reached through a link stops
the step, so name the folder the link leads to, and name the narrowest folders. The GitHub Action
passes no such option, so run the step with the image or the installed CLI, as above.
`aristotle review` takes it as well, but its `--scope changed` and `--scope paths` choose what to
verify from the files git tracks: the files it lets up go with the design, and only `--scope all`
verifies them. What the option never lets
through is under [Security and data handling](#security-and-data-handling).

---

## What your team sees

A build log that reads like a test run, one line per property in the manifest:

```
Aristotle — formal verification
  2 proved, 1 refuted, 1 in progress

  PASS  arbiter_one_grant
  PASS  fifo_push_no_ub

  FAIL  [refuted] fifo_no_overflow
      rtl/async_fifo.v:118
      The write pointer never passes the read pointer: a write to a full FIFO is dropped.
      Artifacts:
        sim/fifo_no_overflow_cex.vcd
      Download them with:
        aristotle download <project-id> --artifact sim/fifo_no_overflow_cex.vcd
  SKIP  arbiter_fair  (in progress)

  Suggested by Aristotle, not part of the verdict:
  -     Ready.recurs_after_reset  (proved)
        After reset, ready is asserted again within 4 cycles.

  Aristotle's summary of the run:

    fifo_no_overflow does not hold. Counterexample at cycle 12: with wr_en held high and
    rd_en low from cycle 4, the write pointer wraps past the read pointer.

  Full run: https://aristotle.harmonic.fun/projects/<project-id>
```

A property in the manifest is proved (pass), refuted (fail), or left
without a verdict. It is in progress when the run finished without checking it, or the time budget
ran out with the run left going. It is unresolved when the run ended short of it, by failing,
running out of budget, being cancelled (as the step cancels it when the time budget runs out) or
finishing without completing the request (as when Aristotle stops it for questions), or when its
proof no longer checks, so a re-run should re-prove it. The annotation and JUnit entry of a property still
in progress say so, and why, before its statement. A run that stopped short of checked
verdicts is an error in the JUnit report, with the reason: one Aristotle stopped for questions, one that
ran out of budget or time, failed or was cancelled, and one that never started. A property with `expect = "refuted"`, a
deliberate counterexample, turns that around: a refutation passes, as `refuted, as expected`, and a
proof fails, as `unexpectedly proved: update the manifest`. A refuted property means the code or the
property is wrong: fix the code, correct the property if it doesn't state what you want, or, if the
counterexample is deliberate, mark it `expect = "refuted"`. Don't drop or weaken a correct property
to get a passing build. Under the default `fail-on`, a property without a verdict doesn't fail the
build: the log names those properties and says the build still passes, and `fail-on: no-verdict`
fails it on them, in progress, unresolved or unchecked.
One the run has no property under, because Aristotle registered none or withdrew it, is
without a verdict too, as `not registered`, or `not registered, run cut short` when the run ended before
registering it: ran out of budget or time, failed or was cancelled. A run that verifies none of the manifest's properties fails
the build, even under the default `fail-on`, and the log names the likely causes and links the run.
If Rust isn't enabled for the account, Aristotle leaves that code out of the run. The step can't ask
Aristotle whether it is, so the log names Rust as a possible cause. Only
`fail-on: never` lets such a run pass. Aristotle checks a
run's results when the run finishes, so a run that runs out of budget, or hits the time budget,
reports what it reached as unchecked: a proof or refutation it reported has no verdict, labelled
`proved, unchecked` or `refuted, unchecked`, and the step fails with `3`: for a run the time
budget cut, whatever `fail-on` is, and for one that ran out of budget, unless `fail-on` is
`never`. The JUnit report records the cut as an error, with its reason. The summary line counts the two apart, as in `3 unchecked (1 refuted and 2 proved)`, and
an unchecked refutation's annotation comes before those of the other properties without a verdict, so
GitHub's limit of 10 annotations of each level per step drops those first. If it had no property under any of the
manifest's ids, the log says that instead. A run that failed or was cancelled elsewhere also ends
before that check, so a proof it reported is labelled `proved, unchecked` too, while a refutation
keeps its verdict, and the JUnit report records it as an error too.
Properties Aristotle proposes on its own are listed as suggestions, in the log and in
the GitHub job summary under "Suggested by Aristotle". They are never in the JUnit report or the
annotations, they never affect the exit code, and they don't count as verified; to check one on
every run, add it to the manifest.

Each run is a project in the web app, named after the repository and the directory in it, what
the run checked, and the branch and commit, such as `rtl/hw: ci @ main 1a2b3c4`,
`rtl: ci gates/nightly.toml @ main 1a2b3c4` for another manifest, or
`rtl: review changed @ feature/fifo 9f8e7d6`, so the runs of different subprojects and commits can be
told apart there. The repository is the last part of `origin`'s URL, else the CI system's repository,
else the checkout's folder. The branch is the one the CI system reports, else git's, and a detached
checkout outside CI shows the commit alone. A run whose directory holds tracked files that differ from
that commit, staged or not, as when you check a change before committing it, is named
`@ main 1a2b3c4+changes`, and the log says so. Set the `project-name` input (`--name`,
`ARISTOTLE_NAME`) to name it yourself. The name is set just after the project is created; if that
fails or takes over 10 seconds, the run goes ahead under the name of the directory it uploaded, and
the log says so. `--dry-run` prints the name a run would get.

A failing row shows the property as Aristotle formalized it. When Aristotle links files to a
refuted property, such as its counterexample's waveform (a VCD) or a testbench that reproduces
it, the row lists them under `Artifacts`, with the `aristotle download --artifact` command that
saves them into `aristotle-artifacts`, and so do the property's annotation and its JUnit failure.
The command names only paths that read the same in every shell, so it pastes safely on any
machine; a path that would need quoting is listed but left out of it.
A counterexample Aristotle describes in plain language comes in the run's summary, which the log
prints whenever a property fails or, unless the time budget cut the run, has no verdict, where it
may also say why Aristotle didn't check one. The
GitHub job summary quotes the run's summary, and the JUnit report carries it with each failure and
with the error of a run that ran out of budget, failed or was cancelled; both stop at 4,000 characters, and the log has it whole. The source line
appears when the run records where in your code the property lives; otherwise the result names
the property alone. On GitHub, the failure is also an annotation, on that line of the diff when
there is one. On GitLab and Jenkins, it lands in the native test report with full history, so
you can see which commit turned a property red.

### Exit codes

Exit codes follow the [CLI's table](cli-reference.md#exit-codes), so a pipeline can tell a proof failure
from an outage. `1` means the verdicts on the manifest's properties failed the build: a
property that fails it (refuted, or proved while marked `expect = "refuted"`), a run that verified none of them, or with `--fail-on no-verdict` also a
property without a verdict, one not registered included. It also means Aristotle stopped the run
before finishing for decisions only you can make; the log prints the run's summary, which lists
them. A run that failed or was cancelled elsewhere before it finished exits `3`, unless a property
had already failed. A run that ran out of budget, or hit the time budget, exits `3`
whatever it reported, since its results are unchecked. With `--fail-on never`, a run that finishes
exits `0` even if it failed, while one the time budget cut exits `3`, as it hasn't finished; the other codes still report a run that could not be started or
followed. A missing or invalid manifest exits `2`. Four
things differ from the table for `aristotle ci` and `aristotle review`: an interrupted job
cancels its run unless `--on-timeout leave` is set (`aristotle ci --project` leaves it going
unless `--on-timeout cancel` is), a transient error (5xx, 429, network) that
outlasts the retries while fetching the results exits `3`, a run refused for too many
projects in progress [waits for a slot](#concurrent-pipelines), exiting `6` if none frees up in
time, as does a run still queued when the time budget runs out, whatever `--fail-on` is, and when
the response to creating the run is lost, the step looks for the run among your newest projects
and follows it, exiting `7` if it can't find it.

---

## What gets verified

| Language | What Aristotle proves |
|---|---|
| **Verilog / SystemVerilog** | Temporal properties over module traces — liveness, bounded response, mutual exclusion, no-deadlock, protocol conformance, overflow freedom |
| **C** | Memory safety, absence of undefined behavior, functional correctness against a specification |
| **Rust** | Functional correctness, panic freedom, invariants across `unsafe` boundaries |

You choose the properties, and stating one takes a sentence or a name, not a formal
specification. Aristotle formalizes each one and describes it in plain language, so a reviewer
can judge whether it's the property they actually wanted. It may also propose properties of its
own; those come back as suggestions, outside the verdict, for you to add to the manifest or
leave.

---

## Time and cost

Every `aristotle ci` run proves every property in the manifest, so the time and cost of a run
follow the size of the manifest, not the size of the change. How often it runs is up to the path
filters in your pipeline: run the step when the code the properties are about, or the manifest,
changes.

The time budget is the `timeout` input (`--timeout`, `ARISTOTLE_TIMEOUT`) in minutes, at most
10080 (a week). When that is unset, the manifest's `check.max_wall_time_seconds` applies, and
without either the budget is 60 minutes. It is the whole step's: waiting for a free slot, before
the run is submitted or in Aristotle's queue, counts toward it (see [Concurrent
pipelines](#concurrent-pipelines)). A run that hits the budget after it started fails
the step with `3` whatever `fail-on` is, and whatever it had reported by then: it hasn't finished,
and Aristotle hadn't checked its results, so none counts as proved or refuted. A run that runs out
of Aristotle's own budget fails it with `3` too, unless `fail-on` is `never`, as it has ended. A run still queued when the budget runs out exits `6` instead,
whatever `fail-on` is, as when no run slot frees up in time. A run that keeps hitting the budget
needs more time: raise `timeout` (`--timeout`) or `check.max_wall_time_seconds`. `aristotle review`
is advisory, but a review the budget cuts exits `3` too, so give it time as well, or let its job
fail without failing the pipeline, as below.
So shorten the budget only once you know how long your design actually takes, and leave room to
spare. Give the job itself longer than the budget plus setup: GitLab and Azure Pipelines end a
job after 60 minutes by default, before a 60-minute budget runs out, and a job killed that way
writes no report. When the budget runs out, or the job itself is cancelled (a superseding push,
a manual cancel), the step cancels the run so it stops consuming budget; set
`on-timeout: leave` (`ARISTOTLE_ON_TIMEOUT=leave`) to let it finish in the background instead.
A job cancelled while the step uploads looks for the project for up to 10 seconds and cancels its run once it starts.
The step waits up to 5 seconds for Aristotle to answer a cancel and then checks the run once, so the budget is overrun
by a few seconds at most; if the run doesn't read cancelled by then, the log gives the `aristotle cancel` command.
When the budget runs out on a run left going, the log says so, with the `aristotle show` command
that follows it. It also says how to report on the run later, in the log and on GitHub in the job
summary: run the same command with `--project` and the run's id.

To report on a run left going, run the same command again with `--project` (or
`ARISTOTLE_PROJECT`) and the run's id, which that log names. `aristotle ci --project <id>`
uploads nothing: it waits for the run within the time budget, then reports the manifest's
properties as a new run would, with the same JUnit report, job summary and exit code. If the
budget runs out again, or the command is stopped, it leaves the run going and says how to report
on it later, in the log and on GitHub in the job summary, unless `--on-timeout cancel` is given. Each run
`aristotle ci` starts records the commit it started at, a digest of the files it uploads and one
of the manifest's properties (`--dry-run` shows the record), and `--project` exits `2` before
waiting unless the run matches the checkout it is in: the project's only run, recorded by
`aristotle ci`, on the files this checkout would upload, uncommitted changes included, and for the
same properties: the same ids, kinds, statements and assumptions, in any order. A manifest among the
uploaded files counts as one of them, so any edit to it counts; of a manifest outside them, only the
properties do. Its message says what differs, and to check out the run's commit, if the run started
from a clean checkout, or start a fresh run with plain `aristotle ci`, with `ARISTOTLE_PROJECT` unset
if that named the run, and, if the run hasn't ended, gives the `aristotle cancel` command that stops
it, so a fresh run doesn't leave it going. So collect a run before the code or the manifest changes. This is how a job, or an agent whose commands are cut off after a few minutes, checks a
design that takes longer:

```bash
aristotle ci --timeout 9 --on-timeout leave                        # exits 3 when the budget runs out
aristotle ci --timeout 9 --on-timeout leave --project 1a2b3c4d     # repeat until it ends otherwise
```

A run still queued when the budget runs out exits `6` instead of `3`, whatever `fail-on` is. A run
that has started exits `3` whatever `fail-on` is, as it hasn't finished, and the log says it goes
on.

Run the container with `--dry-run` to validate the manifest and see what would be uploaded, and
how large it is compressed, without submitting anything or spending quota. The upload can be at
most 100 MB (104857600 bytes) compressed; the dry run checks that too, and a step over it exits `2`.

### Concurrent pipelines

Aristotle limits how many projects each user can have in progress at once, and every
project of that user's counts: ones started from the web app, from a script, or by another
pipeline. When Aristotle refuses a new run over the limit, `aristotle ci` and `aristotle review`
wait for one of those projects to finish, logging Aristotle's reason and the projects they are
waiting on, and then submit. They upload again once one of them finishes and no other run starts
in its place, or, when this key can see none of them (a project in another organization counts
too), after a backoff that grows to 10–15 minutes. Waiting pipelines aren't served in order: the
first to check after a project finishes takes the slot. The wait comes out of the time budget, and
a run is submitted only while a quarter of the budget, and at least a minute, is still left for
it. If no slot frees up by then, nothing is verified and the step exits `6`.

Where Aristotle queues a run over the limit rather than refusing it, the run waits behind your
others, and the wait comes out of the same budget. If the budget runs out before the run starts,
nothing is verified: the step says the run never started, reports each property in the manifest
as `run never started`, and exits `6`, as when no slot frees up in time, whatever `--fail-on` is.

Run CI as a dedicated account in your organization, such as `ci@yourcompany.com`, with an
API key of its own, rather than on a person's key. Its pipelines then don't wait on that
person's projects, nor theirs on its pipelines, and its key keeps working when someone
leaves the organization, which expires their keys for it. The account's own pipelines still
share one limit; talk to us if it is too low.

---

## `aristotle review`

`aristotle review` is the open-ended run: with no manifest, Aristotle works out what should hold
of the files in scope, formalizes those properties, and checks them. Its properties differ from
run to run, so a test that passed on one commit may not exist on the next. That is why it is not
the CI gate: it is advisory. `--fail-on` defaults to `never`, so once the run finishes the step
exits `0` whatever the verdicts; `--fail-on failure` makes it gate on them. Its JUnit report
records what happened whatever `--fail-on` is: a refuted property is a failure, and a run cut short,
failed, cancelled, never started or stopped for questions is an error, so to keep the step advisory, leave the report out
of any publisher that marks a build unstable on them, as Jenkins's `junit` step does. Use it to
explore a design, find properties worth adding to the manifest, or audit a change.

It runs from the same image, with `aristotle review` as the command, or from the GitHub Action
with `command: review` (see [GitHub Actions](#github-actions)). For example, on GitLab:

```yaml
formal-review:
  image: ghcr.io/harmonic-ai/aristotle:3
  timeout: 90m
  script:
    - aristotle review
  variables:
    ARISTOTLE_SCOPE: changed
  allow_failure:
    exit_codes: [3, 6]   # a review its budget cuts, or one still queued, has no results yet
  artifacts:
    reports:
      junit: aristotle-results.xml
```

On Jenkins, run it as `sh(script: 'aristotle review', returnStatus: true)` and fail the stage only on
a code other than `0`, `3` and `6`; on GitHub, leave the review job out of required checks.

It takes the same `--fail-on`, `--timeout`, `--on-timeout`, `--junit`, `--no-junit`,
`--directory`, `--allow-untracked`, `--name` and `--dry-run` as `aristotle ci`, with a budget of 60
minutes unless `--timeout` says otherwise. It reads no manifest, and warns when `ARISTOTLE_MANIFEST` is
set, and it checks the whole repository from any subfolder, with `--path` patterns relative to its top
level. Its scope decides what Aristotle spends its budget proving:

| Setting | Behavior |
|---|---|
| `--scope changed` (`ARISTOTLE_SCOPE=changed`) | Only the source files touched by this change, plus what they depend on. The default. |
| `--scope paths` | An explicit allowlist, given with `--path` (`ARISTOTLE_PATHS`, newline- or comma-separated; a flag replaces its variable rather than adding to it) — for example, only `rtl/core/**`. |
| `--scope all` | Every source file in the repository. Suited to a nightly or pre-release job. |

```bash
aristotle review --dry-run           # print the resolved scope, submit nothing
aristotle review --scope changed     # the files this change touched (default)
aristotle review --scope paths --path 'rtl/core/**'
aristotle review --scope all         # every source file — for a nightly job
```

Source files are Verilog and SystemVerilog (`.v`, `.sv`, `.svh`, `.vh`), C (`.c`, `.h`), Rust
(`.rs`) and Lean (`.lean`). Lean's build files (`lakefile.lean`, `lakefile.toml`,
`lake-manifest.json` and `lean-toolchain`) are not verified themselves, but `changed` counts a
change to one as a change to every Lean file in its folder and below, since it decides how they
build.

`--instructions` (`ARISTOTLE_INSTRUCTIONS`) adds instructions to the run, for example to use the
assertions already in the repository.

For a pull or merge request, `changed` covers every commit since the branch left its target.
On GitLab, a pipeline on a branch other than the default is compared with the default branch.
Otherwise, for a push it covers every commit in the push, measured from the commit the branch
pointed at before it, which GitHub and GitLab report; elsewhere, or when the branch is new, it
falls back to the last commit alone, and the log says which comparison it made. Set
`ARISTOTLE_BASE_REF` (`--base-ref`) to compare against a branch instead.

A CI checkout often holds only the commit being built. When it lacks the history to find where
the change began, the step fetches the target branch — or the default branch, on a GitLab branch
pipeline, or the commit before a push — from `origin` itself, deepening a shallow clone as far as
it needs. If it cannot, it stops with the usage-error code (`2`) rather than guess from the last
commit alone, whatever `fail-on` is.

A `paths` or `all` scope that matches no source file exits with the usage-error code (`2`),
and so does a `--path` pattern that matches no tracked source file, so a mistyped pattern
cannot leave the step green. `--scope all` takes no `--path`, and rejects one rather than
verifying more than was asked. A change that touches no source file is different: there is
nothing to verify, so the step passes and writes an empty JUnit report, even under
`--fail-on no-verdict`, and says in the log that nothing was verified. A change whose only
source files have names that aren't UTF-8 writes the same empty report and says so, and fails
under `--fail-on no-verdict`, since those files can't be verified until they're renamed. A change
whose only source files to verify are ones an ignore rule holds back, though git tracks them, did
touch something to verify. When one of them is Verilog, the step reads the block the root
`aristotle.f` describes, and those the block takes go up and are verified. Otherwise, or when it
takes none, the step exits with the usage-error code (`2`), whatever `fail-on` is, and names them,
as a `paths` or `all` scope of only such files does. To keep such a file out of the review on
purpose, as a model that mustn't be sent, give `--path` (`ARISTOTLE_PATHS`) globs that don't match
it.

Scope is about effort, not exposure: the whole tracked checkout is uploaded either way, so
that a scoped module can be analyzed against its real dependencies. See
[Security and data handling](#security-and-data-handling).

---

## Security and data handling

Straight answers, because for most teams reading this these are the first questions.

**What leaves your network.** The repository's git-tracked files, and the run's instructions:
the properties in the manifest for `aristotle ci`, or for `aristotle review` the list of files in
scope (uploaded as `.aristotle-focus.txt`) and any `instructions`. Nothing else, unless the step's
command line names folders with `--allow-untracked`, below. Proving a
property about one module requires its dependencies, its headers, and its build configuration,
so the upload is the checkout rather than the diff — `review`'s scope controls what gets
*verified*, not what gets sent. Because the set is `git ls-files`, less any tracked file that a
`.gitignore` rule now matches, anything your `.gitignore` excludes — build output, waveform
dumps, vendor blobs, local credentials — never leaves the machine. The exceptions are Lean's
build files (`lakefile.lean`, `lakefile.toml`, `lake-manifest.json` and `lean-toolchain`, in any
folder), which go up whenever git tracks them, because a Lean project can't build without them, and
the design's files that `--allow-untracked` lets through.
The step names each tracked file it holds back, with the rule that matched it and how to upload
it. With an `aristotle.f` at the root, it, every filelist it names, every file it names or
includes, and the file any link among them leads to, must be tracked by git in the folder the step
uploads, neither named like a credential nor in a folder that holds credentials, and none of them
may use an environment variable: one that breaks this stops the step before anything is sent and
is named, with nothing of its content printed, so a pull request can't make the step send a file
from the build machine or the job's environment through a filelist, an `` `include`` or a link.
A network path (`\\host\share\…` or `//host/share/…`), a Windows device path or a drive-relative
one, in a filelist or an `` `include``, stops the step before anything looks it up, on every
platform: on Windows, looking up a network path connects to the computer it names and signs in to it.
The one way past that is `--allow-untracked <folder>` on the step's command line, for sources the
build generates and shared folders outside the folder the step uploads. It lets through only the
files the root `aristotle.f` reaches in the folders it names. Those can't be network paths or
reached through a link, and can't be or hold the folder the step uploads, the folder where the CI
system keeps the job's
credential files (GitHub's `RUNNER_TEMP`, where `actions/checkout` keeps its token, Azure's
`AGENT_TEMPDIRECTORY`, GitLab's `<project>.tmp` and Jenkins' `<workspace>@tmp`), the home folder or a
temporary folder, as a credential file another step or tool wrote there, under a name no rule matches,
would be in reach. It never
lets through a file in a `.git` folder, named like a credential or in a folder that holds them, or in
`/proc`, `/sys` or `/dev` outside the folder the step uploads, nor one a link leads out of those
folders: such a file stops the step. Like the API key, it trusts the job's definition: no file in the repository can set it. It
also trusts what the build writes into those folders, so a build step that copies files a change
names, as a preprocessor following a template's includes does, shouldn't write into them. A tracked file the design needs, `aristotle.f` included, goes up even when an ignore rule
matches it, and the step names it. When the committed `aristotle.f` uses options Aristotle doesn't read, a
generated one, derived from it, goes up in its place. A file it leaves out of the design with
`--blackbox` stays out of the upload of a step that reads it, which `aristotle ci` always does and
`aristotle review` does for the whole checkout, whose review verifies the block's sources too, or for
Verilog files, held back or not, and then verifies each held-back file in its scope that the block
takes; `aristotle ci setup` and other
reviews send it with the rest of the checkout. An `` `include`` by absolute path into the
checkout is rewritten in the uploaded copy, but files otherwise go up as they are. It also leaves out, and names, a tracked file whose name isn't UTF-8, which Aristotle can't
take. A symbolic link goes up only when its target does, and a tracked path that isn't a file in the checkout,
such as a submodule or a file outside a sparse checkout, is left out. Tracked files are dropped too
when their name, ignoring case, matches one of `.env`, `.env.*`, `.envrc`, `*.pem`, `*.key`,
`*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `*.ppk`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ecdsa_sk`,
`id_ed25519`, `id_ed25519_sk`, `.netrc`, `_netrc`, `.git-credentials`, `.npmrc`, `.pypirc`,
`.pgpass`, `.my.cnf`, `.htpasswd`, `.vault-token`, `.s3cfg`, `.boto`, `kubeconfig`,
`*.tfstate`, `*.tfstate.*`, `*.tfvars`, `*.tfvars.json`,
`credentials` (as in `.aws/credentials`), `credentials.json`, `credentials.toml`,
`application_default_credentials.json` or `*.setup.json`, and so is every tracked file in a hidden
folder so named, such as `.env/`, or in a folder that holds credentials: `.docker/`, `.ssh/`, `.kube/`,
`.gnupg/`, `.aws/`, `.azure/`, `.terraform.d/`, `.config/gcloud/`, `.config/gh/`, `.config/glab-cli/`,
or gcloud's, GitHub CLI's and Terraform's folders under `AppData\Roaming` on Windows. These count from
the folder the step checks too, by its own name and the names above it that complete one of them there:
a repository at `~/.config` leaves out `gh/hosts.yml`, one at `%APPDATA%` leaves out
`GitHub CLI\hosts.yml`, and one at `%APPDATA%\gcloud` leaves out everything in it. The step prints
what it excluded. That is a match on names only: a key committed under any other name, or pasted into a
source file, is uploaded. Git directories are the exception, as they count by what they hold: the files
of one committed as plain files, such as a vendored bare repository, whose `config` may hold a remote's
token, are left out too, and a folder the step checks that is one, or is inside one, stops the step. The filtering runs on your side, in a
step you can read the output of, rather than being something you have to take our word for.

**Where it goes.** Aristotle's hosted service over TLS, authenticated with an API key you
provision and can revoke.

**What the step prints.** File names, the manifest and everything Aristotle reports are printed
with control characters other than tabs and newlines escaped, and on GitHub none of that text can
act as a workflow command, so a pull request can't use it to set a step output, mask the log or
silence the step's own annotations. A problem matcher that your workflow registers still reads
every line, as it does any step's.

**Proxies and private networks.** The container respects `HTTPS_PROXY`, `ALL_PROXY`, and
`NO_PROXY`, with an `http://`, `https://`, `socks5://` or `socks5h://` proxy. `HTTP_PROXY` is read
too, but applies only to `http://` addresses, and Aristotle is served over `https://`; a malformed
value in any of these variables stops the command. If your build machine sits behind a TLS-inspecting
proxy, point it at your internal CA bundle with any of `ARISTOTLE_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE`, or `SSL_CERT_FILE`: the first one that names a real file wins. Without any of those,
`SSL_CERT_DIR` names
a directory of hashed certificates (as `c_rehash` or `openssl rehash` makes them) to trust instead of
the bundled ones. An authority you name is trusted without the keyUsage extension, which many company
authorities lack, on Python 3.13 and later too, where Python's own checks demand it. If the proxy's
authority is in none of them, a command fails at once with an error that names `ARISTOTLE_CA_BUNDLE`,
or the file or folder you named. The bundle covers the server's certificate; an `https://` proxy's own
certificate is checked against the system's trusted authorities, with Python's own checks. A build
machine needs egress to exactly one host: `aristotle.harmonic.fun` on 443.

**Air-gapped and on-premises.** If code cannot leave your network at all, talk to us before
you start a pilot — that's a deployment conversation, not a configuration flag.

---

## What this is not

Worth being direct, because these will come up:

- **It is not a replacement for simulation or a UVM testbench.** It complements them.
  Formal is strongest on control logic, protocols, and corner cases that random stimulus
  rarely reaches.
- **It will not prove everything.** Hard properties time out. Unresolved is a normal
  outcome, reported honestly rather than dressed up as a pass.
- **It is not instant.** Even a small design takes tens of minutes, and every run proves
  every property in the manifest. Run it as its own pipeline stage, in parallel with your
  other checks, and only when relevant files change — not in a pre-commit hook.
- **Properties still need review.** Aristotle describes each property in plain language
  precisely so an engineer can confirm it formalized the one the manifest asked for, and judge
  whether a suggestion is worth adding. A proved property is only as valuable as its statement.

---

## FAQ

**Do we need to write properties first?**
For `aristotle ci`, yes: it checks the properties in `aristotle.toml`, and exits `2` without
one. You don't have to write it from scratch: `aristotle ci setup` has Aristotle propose properties
and writes the manifest for you to review. Editing one is not a formal-methods task either: a
property is a sentence in plain language, or the name of an assertion or a C function already in
the code. For more candidates, run `aristotle review`, which proposes properties for the files in
scope, or read the suggestions an `aristotle ci` run lists alongside its verdict.

**What happens on the first run?**
Aristotle registers each property in the manifest under its id, attempts to prove it, and
reports one test per property, with any suggestions of its own listed separately. Expect to
spend some time on the first few runs reading how it stated each property. Runs do not yet reuse
each other's results, so every later run proves every property again.

**Can a property be advisory instead of blocking?**
Not within one manifest. `fail-on` applies to all of the manifest's properties: `failure` (the
default) fails the build on a property that fails it (refuted, or proved while marked
`expect = "refuted"`), when no property got a verdict, or when
Aristotle stopped the run for decisions only you can make, or with `3` when the run failed or
was cancelled elsewhere before it finished, or ran out of budget or time before Aristotle
checked its results; `no-verdict` also fails it when any property has no
verdict, one not registered included (`unresolved`, its old name, still works); and `never` makes
the step advisory: once a run finishes, the step exits `0` whatever the verdicts, even if the run itself failed. It
still exits non-zero when no run can be started or followed — `2` for a usage problem such as a missing or invalid
manifest or an unreadable repository, `3` when the service cannot be reached or the time budget
cuts the run, `5` for a missing or rejected API key, or one that can't be sent, such as a secret
with a space or a non-ASCII character in it (whitespace around it is ignored), `6` for too many requests at once, when no run slot frees up in time (see
[Concurrent pipelines](#concurrent-pipelines)), or when your organization doesn't have the credits to start a run, and `7` when the response to creating the
run was lost and the step can't find the run among your newest projects. To gate on some properties and not
the rest, put the advisory ones in a second manifest and run a second step with `manifest`
pointing at it and `fail-on: never`; each step is a full run of its own, and that step still
exits `3` when its time budget cuts its run, so let it fail without failing the pipeline, as for
`aristotle review` above. Give that step its own
`junit` path, so its report doesn't replace the gating step's, and leave it out of any JUnit
publisher that marks a build unstable on a failed test, as Jenkins's `junit` step does (or pass
`--no-junit`). Suggestions are always advisory.

**What if Aristotle needs to ask something?**
Nobody is there to answer in CI, so `aristotle ci` runs can't ask, and Aristotle doesn't guess
either. At a decision only your team can make, such as what a block is meant to do where the
code and its documentation disagree, Aristotle finishes everything that doesn't depend on it,
lists its open questions in the run's summary, and stops before finishing. The step fails with
`1` (unless `fail-on` is `never`) and prints the summary under "Aristotle stopped before
finishing:"; on GitHub an error annotation points to it, and the JUnit report records the stop as
an error. Answer the questions where the next run can read them: in a property's statement, in its
`assumptions`, which is the place for an `sva` property's answers, or in the code or documentation
they concern.

**Does it work on forked pull requests?**
Not out of the box. CI systems withhold secrets from pull requests opened from forks, so the
step has no API key and exits with the authentication-error code (`5`). Skip the step for
forked pull requests, or run it only on branches of the main repository.

**What does it cost to run on every PR?**
Every run proves every property in the manifest, so the cost follows the manifest and how often
the step runs, which the path filters decide. Talk to us about volume for your repository size
and merge rate.

**How do we give Aristotle our top module, defines and include directories?**
Commit an `aristotle.f` at the root of the repository, or of the subproject `working-directory`
names. `aristotle ci` and `aristotle review` take no `--top` or `-D` of their own. It is read as
a simulator reads a filelist, nested `-f` and `-F` files, `+incdir+`, `+define+` and joined
options included, so the one your flow writes usually works as it is. Every file it names or
includes goes up, and each must be tracked by git in the folder the step uploads; an environment
variable in it stops the step. See
[Verilog build configuration](verilog.md) for what it reads and an
example.

**What about foundry cells and third-party IP we can't upload?**
Name each such module with `--blackbox <module>` in `aristotle.f`, and leave its files out of the
filelists when they aren't in the repository. Aristotle treats it as a black box with unconstrained
outputs, and writes whatever stub the proof needs, so don't write one. A tracked file the filelists
name, or a tracked `.v`, `.sv`, `.vh` or `.svh` file they don't, that declares nothing but black
boxes stays out of `aristotle ci`'s upload, but a model encrypted whole declares nothing the step
can read, so leave such a file out of the filelists, and match it with a `.gitignore` rule; a review
of a change to that file alone then stops, unless `--path` globs leave it out.
`aristotle ci setup`, and an `aristotle review` that checks no Verilog file, don't read
`aristotle.f`, so they send a tracked file a black box leaves out with the rest of the checkout: to
keep it out of every upload, match it with a `.gitignore` rule as well. `aristotle ci --dry-run`
lists each black box with the files it leaves out, and the line the run's instructions gain.

**We use Perforce, not Git.**
Not yet. The step reads the files to upload, and for `aristotle review` the change, from git, so
it needs a git checkout. Talk to us if your code lives in Perforce.
