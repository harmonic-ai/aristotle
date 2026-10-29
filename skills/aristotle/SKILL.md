---
name: aristotle
description: Formally verify code with Harmonic's Aristotle through the `aristotle` CLI. Submits a project, answers its questions, reports which properties were proved or refuted, and keeps an existing `aristotle.toml`, the properties `aristotle ci` checks, in step with the code. Use when the user asks to prove, formally verify or find a counterexample to a property of Verilog/SystemVerilog, C or Rust code, mentions Aristotle or `aristotle.toml`, or changes code or a spec that an `aristotle.toml` covers.
---

# Aristotle

Aristotle formally verifies code. You state a property in plain language, or name an assertion or a function in the
code, and Aristotle either proves it or finds a counterexample. This skill targets **aristotlelib 3.1.0**. Its
commands are for POSIX shells; in Windows PowerShell, read [references/windows.md](references/windows.md) first.

## Before the first command

1. Run `aristotle --version`. It must print `aristotlelib 3.` followed by the rest of the version.
   - If the command isn't found, or prints `aristotlelib 2.` or older, ask the user to install it with
     `uv tool install "aristotlelib>=3"`. If uv says an executable named `aristotle` already exists, it's usually an
     older aristotlelib that pip installed: ask the user before adding `--force`. Until the user has 3.x, follow
     [Older CLIs](#older-clis).
   - Output that doesn't start with `aristotlelib`, or a version older than 3 after that install, comes from another
     `aristotle` on `PATH`: run Aristotle's by its full path, such as `"$(uv tool dir --bin)/aristotle"`, or ask the
     user to fix `PATH`.
   - A command or flag below that the CLI rejects means the CLI is older than this skill. Check
     `aristotle <command> --help`, which has every flag's detail, and work with what it offers.
2. Check access with `aristotle list --limit 1 --json`. Exit 5 means there is no API key, or Aristotle
   rejected it, or it can't be sent: it is blank, or holds a space, a control character or a non-ASCII character
   (whitespace around it is ignored). If stderr says that of the key in `ARISTOTLE_API_KEY`, or that it is not valid,
   ask the user to unset or replace it.
   - Otherwise, before you start a login, ask the user which way they want: you log in for them here, which needs them
     to see your messages as you work, or they run `aristotle login` in their own terminal or set `ARISTOTLE_API_KEY`.
     In Windows PowerShell, offer only the last two. Ask with your tool for asking the user, such as Claude Code's
     AskUserQuestion, or else end your turn with the question, and start a login only when they answer that they want
     one. A headless session, such as `claude -p` or `codex exec`, ends with that question, as it should: the user
     would see a login's code only once the session ended, which stops the login.
   - To log in for them: they approve this machine in their browser, and the key goes straight to the CLI. Make the
     private directory `<out>` (see Ground rules), and start the login so that it keeps running while you talk to
     the user: in Claude Code, run this without the trailing `&`, with `run_in_background`; elsewhere, as it stands.

     ```bash
     { aristotle login --json > <out>/login.json 2> <out>/login.log; echo "exit $?" > <out>/login.exit; } > /dev/null 2>&1 &
     ```

   - Within a few seconds `<out>/login.json` holds `{"verification_uri", "user_code", "expires_in"}`. Tell the user
     at once, in a message: open `verification_uri` where they're signed in to Aristotle, type `user_code`, check
     that the page names this machine (the name `hostname` prints), and approve. The code expires after
     `expires_in` seconds, 10 minutes.
   - Then wait for `<out>/login.exit`, checking every 20 seconds or so. `exit 0` means the key is stored: run the
     check again. `exit 5` means the user denied the login or the code expired, as `<out>/login.log` says: ask before
     starting another. For any other code, report `<out>/login.log`. If the file hasn't appeared 11 minutes after the
     start, the login was stopped.
   - Ask the user to run `aristotle login` in their own terminal instead, or to set `ARISTOTLE_API_KEY`, when the
     login was stopped, and when the login can't approve in the browser: `<out>/login.log` says `Create an API key at`
     (a server that takes only a pasted key) or `unknown option --json` (an older CLI). Once they say it's done,
     whichever they chose, run the check again, and if it still exits 5, report that rather than asking again.

   Never ask for the key in the conversation, and never print, log or commit it. In Codex, whose sandbox blocks the
   network without asking, the command retries and then seems to hang: ask the user to set `network_access = true`
   under `[sandbox_workspace_write]` in `~/.codex/config.toml`, and for `codex exec` also
   `sandbox_mode = "workspace-write"` at the top of that file, above the table.

## Ground rules

- Every run is a paid agent run that takes tens of minutes, and can take an hour or more. Start one only when the user
  asked for verification or agreed to one, never as a side effect.
- Many accounts can have only one project in progress: a second exits 6 or waits in a queue. Never cancel, continue
  or answer questions on a project you did not start.
- Never submit the same work twice. Once stderr shows `Project created: <id>`, the project exists: resume it. A
  `submit` or `continue` that exits 7 may have gone through too: [look for it](#finding-a-run-that-may-exist) first.
- With `--json` or `--non-interactive`, the result is JSON on stdout, and progress and errors go to stderr. Redirect
  stdout to a file, read it, and check the exit code every time.
- Keep outputs (JSON, logs, archives) in a private directory, never in the project directory, whose files go up with
  the next run. Make one with `mktemp -d`, and write its path out in full wherever the commands below say `<out>`:
  each command may run in a fresh shell.
- Never create, modify or stage a file in the user's repository unless the user asked for that change: not an
  `aristotle.f` to name a top module, and not an `aristotle.toml` after a run. Offer instead, as in "Want me to add an
  `aristotle.toml` so CI checks this property?". The exception is code that an `aristotle.toml` already covers: a change
  you make to it keeps the manifest current (see [Keeping aristotle.toml current](#keeping-aristotletoml-current)).
- Pass a project's full id, rather than a prefix of it or its name.
- If git refuses a folder (its message names `safe.directory`), ask the user what to do: never change git's
  configuration yourself. Ask the user before changing their ignore rules, too.

## Submit and wait for one result

Write the prompt to `<out>/prompt.md` with your file-writing tool, or a heredoc whose delimiter is quoted (`<<'EOF'`),
so the shell leaves its backticks, `$` signs and quotes alone. Then:

```bash
timeout -s INT 540 aristotle submit --prompt-file <out>/prompt.md \
  --project-dir <dir> --name "<short name>" --destination <out>/result.tar.gz \
  --non-interactive > <out>/run.json
echo "exit $?"
```

- **The prompt:** state each property precisely: the module or function, the file, the signals or variables, the
  assumptions (reset, input ranges, preconditions) and what must hold, as in
  `Prove that <property> for <module or function> in <file>.` Ask for one property, or a few, per run.
- **The folder:** pass a directory that holds what the proof needs, such as the sources, headers, build files and
  spec, and nothing secret: files named like credentials stay out, but the check goes by names only.
  `aristotle submit --dry-run --prompt-file <out>/prompt.md --project-dir <dir>` lists what would go up.
- **Verilog:** name the top module, the defines and the include directories in the prompt. `submit` reads the
  `aristotle.f` filelist at the folder's root, if there is one, and each filelist that `-f` or `-F` names, uploading
  every file they name. With a filelist that names a source file, `submit --top <module>` names the top module too;
  without one, `submit` refuses `--top` and `--blackbox`, saying the block names no source file (exit 2), so name those
  modules in the prompt instead. Never write an `aristotle.f` for `submit`.
- **A module whose source isn't there or shouldn't be sent,** such as a foundry cell, a vendor memory model or
  third-party IP: add `--blackbox <module>`, once per module, to `submit`, or, where `submit` refuses it, say in the
  prompt that the module is a black box with unconstrained outputs. Never write a stub for it: an empty stub isn't a
  black box, since its undriven outputs may read as 0. Under `--blackbox`, a file the filelists name, or a `.v`, `.sv`,
  `.vh` or `.svh` file they don't in a folder or checkout that goes up, that declares nothing but black boxes stays out
  of the upload. A model the CLI can't read, such as an encrypted one, or one named only in the prompt, still goes up:
  keep it out of the folder or checkout that goes up, and check the dry run.
- **A parameterized module:** name the parameter values to check in the prompt, rather than asking for every
  configuration. When you report a verdict, say which configurations it covers.
- **Questions:** `--non-interactive` stops with exit 10 when Aristotle asks something. Aristotle also asks on a run
  nobody waits for, so check `aristotle questions <id> --json` when you come back to one. Pass `--no-questions` only
  when the user wants a run that never asks.
- **The result:** once the run finishes, `run.json` holds the `project`, the `task` and the
  [property results](#reading-the-results), and `result_path`, where the files were saved. If
  `<out>/result.tar.gz` is missing then, run `aristotle download <project-id> --destination <out>/result.tar.gz`.

### Long waits

A run usually outlasts a tool's limit on one command (10 minutes in Claude Code), and a headless session, such as
`claude -p` or `codex exec`, ends as soon as you reply, losing any run left in the background. So wait in slices,
each in the foreground and never in the background, and in a headless session don't reply before the run has
finished: `submit` above waits 9 minutes. Give each slice your tool's longest time limit (`600000` ms in Claude
Code), so the slice ends first; under a shorter limit, shorten the slice. Save the id from stderr's
`Project created: <id>` line to `<out>/project-id` at once: an error can leave `run.json` empty. Then **resume**:

```bash
timeout -s INT 540 aristotle show <project-id> --non-interactive --destination <out>/result.tar.gz > <out>/run.json; echo "exit $?"
```

Exit 124 or 130 means the slice ended. Then, by `task.status` in `run.json`:
- `QUEUED` or `IN_PROGRESS`: run the same `show` again.
- Any other status: the run had finished as the slice ended. Unless `result_path` names the archive, run
  `aristotle download <project-id> --destination <out>/result.tar.gz`; and if `property_counts` is null, run
  `aristotle show <project-id> --json`.
- An empty `run.json`, after a `submit` cut short before `Project created:`: if stderr says no project was created,
  submit again; otherwise [look for it](#finding-a-run-that-may-exist).

Stopping `submit`, `show` or `continue` leaves the run going, but stopping `aristotle ci` cancels its run (see step 3
under [Keeping aristotle.toml current](#keeping-aristotletoml-current)). To stop a run you started by mistake, run
`aristotle cancel <project-id>`, which exits 4 if the run had already finished (0 if it was already cancelled).

Where `timeout` is missing (exit 127; on macOS it may be `gtimeout`, from `brew install coreutils`), start the run
with `--json` in place of `--non-interactive`, and without `timeout` or `--destination`, so it returns once the run
is sent. Until `task.status` shows the run has finished, run `aristotle show <project-id> --json` and
`aristotle questions <project-id> --json`, which don't wait, then `sleep 100`, as one foreground command, and answer
what Aristotle asks. Then run `aristotle download <project-id> --destination <out>/result.tar.gz`, and read the last
`show --json`, which exits 0 whatever the status, as the exit-code table reads a finished run.

### Finding a run that may exist

A `submit` or `aristotle ci` that exited 7, or a `submit` that stopped before stderr showed `Project created:`, may have
made its project, which can take a few minutes to appear. Look in `aristotle list --json` for a project created after
the command started (`created_at`, in UTC), and again a few minutes later: a `submit` whose reply was lost may not carry
its `--name`. After `continue`, look in `aristotle tasks <id> --json`; after `questions --answer`, in
`aristotle questions <id> --json`, where an answered question has `response` set. Carry on with what you find, and send
the request again only if it isn't there.

## Exit codes and what to do next

Whatever the exit code, an error that says `Error: request refused` or `Error: request blocked by a proxy` means a
proxy or a firewall may have refused the request before Aristotle saw it. If your tool reports that its own sandbox
blocked the request, as Claude Code does, allow the host it names the way it says, or ask the user to. Otherwise,
report the error and its `Hint:` line to the user. Don't log in or change the ids. Once the host is allowed, run the
command again, or resume a run it had already started rather than start another.

| Exit | Meaning | Next step |
|---|---|---|
| 0 | The run finished `COMPLETE`. | [Read the results](#reading-the-results). |
| 1 | The run finished without completing the request. | Report what `task.output_summary` and the properties say was and wasn't established. If `task.budget_failure_reason_name` is set, billing stopped the run: tell the user what the summary says. Resume it in a few minutes for `BILLING_UNAVAILABLE`, and otherwise only once an organization admin has bought credits or, for `TRIAL_CREDIT_CONCURRENCY_LIMIT`, once their other project has finished. |
| 10 | Aristotle is waiting for answers; stdout holds the questions. | [Answer them](#answering-questions), then resume. |
| 2 | Usage error, such as a bad argument or an ambiguous project name. | Read stderr and fix the command. Use the full project id. |
| 3 | The run failed or was cancelled, or the command couldn't finish. | If stderr says nothing was done or no run was started (under `--json`, `{"outcome": "not_processed"}`), run the same command again, after the wait its message asks for, if any; if the files weren't saved, run `aristotle download <id> --destination <out>/result.tar.gz`. Otherwise read `task.status` with `aristotle show <id> --json`: resume a `QUEUED` or `IN_PROGRESS` run, report a `FAILED` or `CANCELED` one, and otherwise retry later. |
| 4 | Not found, or refused: a closed question or an ended run. | Check the ids, for example with `aristotle questions <id> --json`. Don't retry the same command. |
| 5 | No API key, or Aristotle rejected it, or it can't be sent (blank, or a space, a control or a non-ASCII character in it). From `aristotle login`, the user denied the login or its code expired. | Ask the user which way to log in (see above). After a denied or expired login, ask before starting another. |
| 6 | Rate limited: too many projects in progress, or too many requests. Or the organization doesn't have the credits to start a run. | If stderr says the organization needs credits, tell the user that an organization admin can buy them at https://aristotle.harmonic.fun/settings/billing, and run the command again only once that's done, or, when it says each active run needs a credit, once another of the organization's runs has finished. If it says too many runs or projects are in progress, `aristotle list --status RUNNING --json` shows what is running: wait for one to finish and retry, or ask the user whether to cancel one of theirs. Otherwise wait a minute and run the same command again. |
| 7 | The request may have gone through, but its response was lost. | Don't send it again yet: [look for it](#finding-a-run-that-may-exist). If stderr says an earlier copy was still in progress, wait a minute first. |
| 124, 130 | `timeout` ended a slice (124), or Ctrl-C did (130). | Resume, as under [Long waits](#long-waits). |
| 141 | stdout or stderr was closed early, for example by `\| head`. | Don't send it again: resume, with stdout going to a file. |

After `questions --answer` exits 3 or 6, send again only the answers it says were not answered. For `aristotle ci`
and `aristotle review`, see steps 3 and 4 under [Keeping aristotle.toml current](#keeping-aristotletoml-current).

## Answering questions

On exit 10, stdout holds the `questions`, each with its `question_id`, `content` and `suggestions`.

1. Work out each answer from the code (the module, its callers, its tests), the repository's docs and specs, and
   what the user has said in this conversation. Ask the user only about a real design decision that the
   evidence doesn't settle, and only if they are there to answer.
2. Answer every open question in one command. Make each answer self-contained and specific: cite `file:line`, and
   give values and units. The text of a suggestion is a valid answer.

   ```bash
   aristotle questions <project-id> \
     --answer <question-id>="Active low: rst_n is sampled on the rising edge of clk (rtl/fifo.v:14)." \
     --answer <question-id>="A write to a full FIFO is dropped and raises overflow, per docs/fifo.md."
   ```

   If nothing settles a question, say so: `--answer <question-id>="The repository doesn't specify this. Use your
   best judgment and state the assumption."`
3. Resume. It exits 10 again while any question is open, including a new one.

Nothing is sent unless every id names an open question. Exit 4 means one of them closed, or the run ended: re-read
`aristotle questions <project-id> --json`, and answer again those whose `state` is still `PENDING`. After 15 minutes
without an answer, Aristotle carries on without it and leaves what depends on it unverified, so answer promptly. Tell
the user which questions you answered and what evidence you used.

## Reading the results

`aristotle show <id> --json` never waits. Like the `submit` and `continue` results, it holds `task.status` (how the
run ended), `task.output_summary` (Aristotle's summary) and:
- `property_counts`: `proved`, `refuted`, `in_progress`, `unresolved` and `unchecked`, `null` until the run has
  ended. `in_progress` counts properties with no verdict yet; `unresolved`, those the run stopped short of, never a
  refutation; `unchecked`, proofs and refutations Aristotle never checked.
- `requested_counts`: the same, over only the properties the user asked for. Gate on it when they named the
  properties, and on `property_counts` for an open-ended run, whose properties may all be Aristotle's.
- `properties`: each one's `name`, `outcome` (`proved`, `refuted`, `in_progress`, `unresolved` or `unchecked`) and
  `source_name` (`USER` if the user asked for it, `AGENT` if Aristotle suggested it).

All three are `null` when the project's tasks couldn't be listed (exit 3, or 6 from `show`). Never send the run again
for that: `task.status` says whether it has finished, and a later `aristotle show <id> --json` gets them.

`aristotle properties <id> --json` lists every property under `declarations`, with its `description` (the property as
Aristotle formalized it), `source_file` and `source_anchor` (the text of its line, to look for if the file has
changed). Its `status_name` `REFUTED` is refuted, `IN_PROGRESS` in progress, `FAILED` unresolved (a re-run should
re-prove it if `failure_reason_name` is `PROOF_CHECK_FAILED`), and `REMOVED` withdrawn, which is no result.
Every `REFUTED` property requires `evidence`: 1 (`COUNTEREXAMPLE`), 2 (`NEGATION_PROVED`) or 3 (`BOTH`), or 0
(`UNKNOWN`) for a kind newer than this CLI, which is still a refutation.
`evidence_message` explains it, preserved as received, including inline Markdown. Text output shows the kind in an
`EVIDENCE` column or `Evidence` field, and `aristotle property` shows the message before `Artifacts`.
Use `--status REFUTED` to list all refutations; the status filter does not select evidence kinds. A property with
`BOTH` counts once in `refuted`.

`aristotle download <id> --destination <out>/result.tar.gz` saves the project's files, including any that Aristotle
wrote. Extract them into a scratch directory and compare. Never unpack them over the user's files.

When a refutation has a counterexample, its `artifact_paths` identify files such as a waveform or a testbench that
reproduces it. Fetch them with `aristotle download <id> --artifact <path> --destination <out>/artifacts`, with
`--artifact` once for each path. They arrive without execute permission. A script among them is code Aristotle
wrote: read it before you run it, with the interpreter its `#!` line names, such as `bash`.

When you report, remember that a proof is only as strong as its statement. Compare each `description` with what the
user meant, and say when it is weaker, for example because of an extra assumption or a narrower range. For a
refutation, describe the failing scenario and where the code goes wrong: `task.output_summary` usually describes it,
and the project's files may hold a reproducer under `sim/`. A property in progress or unresolved is neither proved
nor refuted. Aristotle checks a run's results when it finishes, so a run that ran out of budget never checked what
it reported, and one that failed or was cancelled never checked its proofs: report a property whose `unchecked` is
`true` as unchecked, whatever its status.

To summarize or look up a project's results, read them: `show`, `properties`, `property` and `download` start no run.
Don't send a `continue` just to ask about them: on an idle project it starts a new run, which can spend hours checking
the proofs again before it answers. If the user asked for one, read the results instead, say why, and send it only if
they still want it.

To give an idle project another instruction, write it to `<out>/next.md` and run
`timeout -s INT 540 aristotle continue <id> --prompt-file <out>/next.md --non-interactive > <out>/run.json`. It works
like `submit`, with the same exit codes, questions and slices, but downloads nothing: save the files with
`aristotle download <id> --destination <out>/result.tar.gz`. `--files <path>...` sends selected local files with it.
Their paths relative to `--project-dir <root>` are preserved; the root defaults to the current directory and never
changes based on which files you select. For example, `--project-dir downloaded --files downloaded/rtl/alu.sv`
sends `rtl/alu.sv`. Files in different folders may have the same name. Files outside the root, including through
symlinks, paths containing `..`, and duplicate upload paths exit 2 before any request. File arguments remain relative
to the current directory or absolute. `download --artifact` preserves paths beneath its destination: use that
folder as the root when uploading again. Plain `download` saves the archive unchanged; extract it and choose its
project root first.

## Keeping aristotle.toml current

`aristotle.toml`, at the repository root, lists the properties that `aristotle ci` proves on every run, each
reported as a test named by its id. Write, change or stage a manifest, or its `aristotle.f`, only when an
`aristotle.toml` already covers the code, or the user asked for Aristotle in CI; otherwise, offer to add one (see
[Ground rules](#ground-rules)). Steps 3 and 4 under "Then check it" apply to every `aristotle ci` and
`aristotle review` run. Read [references/aristotle-toml.md](references/aristotle-toml.md) for its format before you
write one.

When you change the code or the spec, update the manifest in the same change:

- For a new behavior or requirement, add a property. For a changed requirement, edit the statement and keep the id,
  which carries the test's history. For a removed feature, remove its property.
- A refuted property means the code or the property is wrong: fix the code; if the property doesn't state what
  the user wants, correct it with them; if the user confirms the counterexample is deliberate, mark it
  `expect = "refuted"`, which passes while it stays refuted. Never drop or weaken a correct property to get a
  passing build, and report every failure to the user.
- State each property as precisely as a prompt, or a good `description` from `aristotle properties`, with what it
  assumes, such as an answer the user gave, in its statement or its `assumptions`.
- For Verilog, when the build gives the design its top module, defines or include directories, write an
  `aristotle.f` at the root of the folder the run checks, in the same change, as
  [references/aristotle-toml.md](references/aristotle-toml.md#verilog-aristotlef) describes. Every file it names must
  be tracked by git in that folder, or be in a folder that `--allow-untracked` names.
- Before writing properties by hand, look in `aristotle list --json` for a finished project that verified this code,
  and start from it: `aristotle ci setup --from <project-id>` writes a whole manifest from its properties. It copies
  statements as they are, and comments on any that may depend on that project's run: fix those first, and review
  the ones that never got a result. Confirm each `expect = "refuted"` it writes with the user before you commit.
  `ci setup` won't replace a manifest without `--force`, which you never pass: edit an existing one by hand, and
  leave replacing it to the user. Plain `aristotle ci setup` needs an interactive terminal, so leave it to the user
  too. Other candidates are what a finished project on related code proved
  (`aristotle properties <id> --status PROVED --json`), and an `aristotle ci` run's suggestions.

Then check it:

1. From the repository, or with `--directory <root>`, run `aristotle ci --dry-run`, which needs no key and starts
   no run. It validates the manifest and builds the upload, printing its size; exit 2 says what is wrong with either.
   For Verilog, check that it says `Using the Verilog filelist committed at aristotle.f` and lists the design's files.
2. Make sure git tracks every file the properties depend on, since `aristotle ci` uploads only tracked files, as they
   are on disk: `git add` each new one your change adds, such as a new source, `aristotle.toml` or `aristotle.f`, and
   stage nothing else; ask before you stage the user's own files (if your sandbox can't write to `.git`, ask the user
   to). The dry run names each tracked file an ignore rule holds back.
3. Run the real check when the user wants the gate confirmed. Each run proves every property from scratch.
   **Never stop it with a signal or a shell timeout: that cancels its run.** Give it a budget under your shell's time
   limit instead, and let the run go on when the budget runs out:
   `aristotle ci --no-junit --timeout 9 --on-timeout leave > <out>/ci.log 2>&1; echo "exit $?"`. The 9 minutes is
   your slice, so don't copy it into a CI configuration.
   - When it exits 3, 124 or 130, or 6 with the run still queued, and the log says the run goes on or starts later,
     run the same command with `--project <id>`, the id the log names, which uploads nothing, until it ends
     otherwise. Never start a new run instead.
   - Don't edit the files while a run you will collect is going: `--project` then exits 2. If it does, start a fresh
     run without `--project` (and with `ARISTOTLE_PROJECT` unset), or ask the user; never check out another commit or
     touch the user's changes to make it fit. If the message says the old run hasn't ended, stop it first with
     `aristotle cancel <id>`, asking the user first if you didn't start it.
   - `aristotle review` takes the same `--no-junit`, `--timeout` and `--on-timeout leave`, but no `--project`: resume
     a review left going with `show`.
   - Don't run a check that ran out of budget or time again as it was: for time, raise `--timeout`.
4. Read the log: one `PASS`, `FAIL` or `SKIP` line per id, then Aristotle's suggestions.
   - Exit 0: the gate passes.
   - Exit 1: a property fails the gate (refuted, or proved while marked `expect = "refuted"`), none was verified
     (the log names the likely causes), or Aristotle stopped for decisions only the user can make. Relay the
     questions under `Aristotle stopped before finishing:` to the user, put their answers in the statements, an `sva`
     property's `assumptions` or the code, and run again. Don't reply with `aristotle continue`, even if the summary
     asks for a reply: that starts a paid follow-up the gate doesn't use.
   - Exit 3: also a run that ended before Aristotle checked its results. Report each `SKIP` labelled `unchecked` as
     the run's unchecked claim, not a verdict.
   - `SKIP <id> (not registered)`: a finished run registered nothing under that id, so make the statement more
     precise. `SKIP <id> (not registered, run cut short)`: the run ended first, so don't edit the statement.
   - Other codes mean what the table above says.

## Older CLIs

- **3.0.0**, and the pre-releases below, work as described here, except:
  - A refusal for credits exits 3, quoting Aristotle's answer with `OUT_OF_CREDITS`, with nothing on stdout under
    `--json`: no run was started, so go by the exit-6 row. A `submit` or `continue` whose stderr quotes
    `BILLING_UNAVAILABLE` exits 7, though nothing was created: run it again in a few minutes.
  - A run billing stopped says only that it ran out of budget, and results have no `budget_failure_reason`:
    `task.output_summary` says when billing stopped it.
- **3.0.0 pre-releases** (`3.0.0rc` and a number) work as described here, except:
  - Before relying on `aristotle ci`, check `aristotle ci --help`. If it doesn't mention `aristotle.toml`, that
    `aristotle ci` is an open-ended run that ignores the manifest: ask the user to upgrade.
  - A `submit`, `continue` or `show` result without `properties`: list them with
    `aristotle properties <id> --sort name --json`, and read `continue`'s result as the task itself, with `status` at
    the top level.
  - If `aristotle properties --help` lists `--pagination-key`, `properties` without `--sort`, `--status` or
    `--created-by` lists only the 30 most recently updated: list them all with
    `aristotle properties <id> --sort name --json`.
  - Before 3.0.0rc6, results have no `unchecked`, so read every proof and disproof of a run that didn't end `COMPLETE`
    as unchecked. There is no `--prompt-file`: pass `"$(cat <out>/prompt.md)"` as the argument (`next.md` for
    `continue`), which the shell doesn't expand again. `show` has no `--destination`, and an interrupted wait prints
    nothing: take the project id from `submit`'s stderr, resume without `--destination`, and once the run has ended,
    save the files with `aristotle download <project-id> --destination <out>/result.tar.gz`.
  - Before 3.0.0rc7:
    - `list`, `tasks`, `properties` and `questions` print bare arrays. `property_counts` says `failed` for
      `unresolved`, a question `agent_run_id`, `download` `path`, and a project `description` for `name`. Timestamps
      have no offset, and questions no `state`: an open one has `response` and `ignored_at` null.
    - Results have no `requested_counts`, `disproved`, `outcome` or `source_name`, and `continue`'s and a resumed
      `show`'s have no `project` or `web_url`: gate on `property_counts`, counting `negated` and `counterexample` as
      disproved. Codes come without names: `status` 3 proved, 2 negated, 5 counterexample, 1 in progress, 6
      unresolved and 4 withdrawn; `source` 2 for the user's, 1 for Aristotle's; `failure_reason` 3, a proof that no
      longer checks, which a re-run should re-prove. `properties --status` takes only `PROVED`, `NEGATED`,
      `COUNTEREXAMPLE`, `IN_PROGRESS` and `FAILED`.
    - Under `--fail-on never`, `aristotle review`'s default, a run the time budget cut exits 0, and its unregistered
      properties say only `not registered`: check the log for a cut-short run before you edit a statement.
    - A request that never got through reads like any network error, with nothing on stdout under `--json`: look for
      it as after exit 7 before sending it again.
    - `submit` doesn't read `aristotle.f` and has no `--top`, `-f`, `-F` or `--dry-run`: name the top module, the
      defines and the include directories in the prompt. `aristotle ci` and `aristotle review` read an `aristotle.f`
      only with one source file, or one option and its argument, per line: write out nested filelists, `-v`,
      `+incdir+`, `+define+` and joined forms such as `-Iinc`.
    - Uploads leave out `.docker/` and folders named like credential files, such as `.env/`, but not `.ssh/`,
      `.kube/` or `.config/gh/`, so `.ssh/config`, `.kube/config` and `.config/gh/hosts.yml` go up: pass a directory
      that holds no such folder.
  - Before 3.0.0rc8:
    - `--blackbox` stops the command, and `submit` doesn't take it: leave the module's files out of the filelists, and
      say in the prompt, or for `aristotle ci` in the assumptions of the properties it touches, that the module is a
      black box with unconstrained outputs.
    - `aristotle ci` and `aristotle review` have no `--allow-untracked`, so a file the root `aristotle.f` reaches that
      git doesn't track in the folder the run checks, such as a generated source or a header in a shared folder, stops
      the run: verify such a block with `submit`, which uploads those files, or ask the user whether to commit them.
    - The stop for `--ignore-unknown-modules` says each module the design instantiates needs a source, if only a stub:
      write no stub. Drop the option, and treat each module that has no source as a black box, as above.
    - Properties have no `artifact_paths`, and `download` has no `--artifact`: look for a counterexample's files, such
      as a waveform or a testbench, in the archive `aristotle download <id> --destination <out>/result.tar.gz` saves.
  - Before 3.0.0rc10, `submit` has no `--include-ignored`: name the ignored files in a filelist, which uploads them.
  - Before 3.0.0rc11:
    - From 3.0.0rc7, `property_counts` and `outcome` say `unresolved` for `in_progress` and `not_verified` for
      `unresolved`, so their `unresolved` means no verdict yet. Tables and summary lines say unresolved for in
      progress and not verified for unresolved, and `properties --status UNRESOLVED` lists `IN_PROGRESS`.
    - `--fail-on` takes `unresolved` for `no-verdict`, which later versions still accept.
  - Before 3.0.0rc12:
    - Under `--json`, a `submit` or `continue` refused for too many runs in progress exits 6 with nothing on stdout,
      and stderr gives Aristotle's reason alone: nothing was created or sent, so go by the exit-6 row. A
      `submit --wait` whose new project's task couldn't be listed exits 3 with nothing on stdout, though the project
      exists: take its id from stderr's `Project created: <id>` line and resume it, as under
      [Long waits](#long-waits), or [look for it](#finding-a-run-that-may-exist), rather than submit it again.
    - `continue` has no `--project-dir`, and `--files` sends each file to the top level of the project under its name
      alone: `rtl/alu.sv` goes up as `alu.sv`, and the project's `rtl/alu.sv` stays as it was. Send one file of each
      name: two exit 2, or, before 3.0.0rc11, the second replaces the first. To send changed files to their paths,
      submit the whole folder as a new project.
  - Before 3.0.0rc13:
    - A refuted property's `status_name` is `NEGATED` or `COUNTEREXAMPLE`, and its `outcome` is `disproved`. The
      counts have `disproved`, the sum of `negated` and `counterexample`, in place of `refuted`, and tables and the CI
      reports say disproved. List refutations with `--status DISPROVED`, since `--status REFUTED` exits 2. Results
      have no `evidence` or `evidence_message`, and from current servers a counterexample reads `NEGATED` too, so
      take how a property was refuted from `task.output_summary`.
    - `aristotle.toml` takes `expect = "disproved"` and refuses `expect = "refuted"`, and 3.0.0 refuses
      `"disproved"`. Change the value together with the version the CI job pins.

  To upgrade, ask the user to run `uv tool install "aristotlelib>=3"`: `uv tool upgrade` keeps a release candidate
  where it is.
- **2.x** has no `--json`, `--non-interactive`, `questions`, `properties`, `login` or `aristotle.toml`, and exits 0 on
  most errors, so don't trust the exit code; its questions can't be answered. Until the user upgrades, start the run
  with `aristotle submit "$(cat <out>/prompt.md)" --project-dir <dir>`, which returns once stderr shows
  `Project created: <id>`, with questions off. Then wait in foreground slices of
  `timeout -s INT 540 aristotle show <id>`, which can return early: after each, check `STATUS` with
  `aristotle tasks <id> --limit 1`, which doesn't wait, and run another slice while it is `QUEUED` or `IN_PROGRESS`
  (without `timeout`, run that check and `sleep 100` as one foreground command). Once the run has ended, run
  `aristotle download <id> --destination <out>/result.tar.gz`, and read stderr and the archive.
