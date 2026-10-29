# Aristotle CLI reference

The detail behind the [README](../README.md), in the order you meet it:

1. [Logging in](#logging-in)
2. [What goes up](#what-goes-up)
3. [Referring to a project](#referring-to-a-project)
4. [Following and waiting](#following-and-waiting)
5. [Results and artifacts](#results-and-artifacts)
6. [Questions](#questions)
7. [Scripts and `--non-interactive`](#scripts-and---non-interactive)
8. [Property statuses](#property-statuses)
9. [Proxies and private networks](#proxies-and-private-networks)
10. For programs: [JSON output](#json-output), [exit codes](#exit-codes) and [retries](#retries-and-lost-requests)

## Logging in

The README's [Log in](../README.md#log-in) shows the login in the browser. Also:

- `aristotle login --no-browser` opens no browser: the page to open is printed either way.
- The code expires after 10 minutes. Ctrl-C stops waiting, and says where to revoke any key Aristotle may have made.
- The CLI checks the key with Aristotle, then stores it, readable only by you, in
  `~/.config/aristotle/credentials.json` (`$XDG_CONFIG_HOME/aristotle` if set, `%APPDATA%\aristotle` on Windows).
  [API keys](https://aristotle.harmonic.fun/keys) lists it under this machine's name, where you can revoke it.
- Logging in this way again replaces the key: once the new one arrives, Aristotle retires the one the last such login
  stored here, if it's yours and for the same organization. A key given with `--api-key`, piped or pasted is never
  retired this way.
- A command that needs a key offers to log in at a terminal, but not with `--json`, `--non-interactive` or
  `questions --answer`, which never read stdin.
- If the server can't approve logins in the browser, `aristotle login` opens API keys for you to create a key, and
  reads it as you paste it, hidden.

| Another way to give a key | What it does |
|---|---|
| `printf '%s\n' "$KEY" \| aristotle login` | Checks and stores a key from stdin, for scripts. |
| `aristotle login --api-key <key>` | Checks and stores that key, which shows in the process list and your shell history. |
| `export ARISTOTLE_API_KEY=<your-key>` | Overrides the stored key, so a logged-in machine still behaves like a build agent. |
| `aristotle --api-key <your-key> submit "My prompt"` | A key for one command. |

## What goes up

`--project-dir` uploads the folder as one compressed archive of at most 100 MB; over that, `submit` says how large it
is and exits 2 before uploading. It leaves out:

| Left out | Named by `submit` | Notes |
|---|---|---|
| `.git`, build output, editor files | No | Aristotle has no use for them. |
| What an ignore rule matches | A tracked file, with the rule and how to upload it | `--include-ignored` uploads these too, such as sources a build generates, but nothing else in this table; `--dry-run` marks what it adds, but `--dry-run --json` doesn't: its `paths` lists those files with the rest. |
| Files named like credentials | Yes | The names `aristotle ci` drops, in any case, such as `.env`, `*.pem` or `.aws/credentials`, and anything in a hidden folder so named, such as `.env/`, or in a folder that holds credentials, such as `.ssh/`. The CI guide's [Security and data handling](aristotle-in-ci.md#security-and-data-handling) lists them all. Names only: a secret inside any other file goes up. |
| Result archives an earlier run saved | Yes | Files named as the CLI saves one, such as the `<project-id>-aristotle.tar.gz` that `submit --wait` and `download` save in the current folder, so a second submit doesn't upload the first one's result. Rename one to upload it. |
| Files whose names aren't UTF-8 | Yes | Aristotle can't take them. Rename one to upload it. |

| Where | Ignore rules |
|---|---|
| In a git repository | Every `.gitignore` that applies, the repository's own included when you submit a subfolder, plus `.git/info/exclude` and your global excludes. Only the repository's `.gitignore` files hold back a tracked file, since the other two are this machine's alone. |
| Elsewhere, in a folder git ignores, or, with a warning, where git isn't installed or refuses the repository, as for a checkout owned by another user | The folder's top-level `.gitignore`. |

No ignore rule holds back `.lake` or Lean's build files (`lakefile.lean`, `lakefile.toml`, `lake-manifest.json` and
`lean-toolchain`, in any folder). Still, in `.lake` and everywhere else, no file named `*.olean`, `*.ilean`, `*.ir`,
`*.o`, `*.so` or `*.trace` goes up, nor the `.hash` file of an `.olean`, `.ilean`, `.ir` or `.so` file, nor an
`.olean`'s `.lock`, `.private` and `.server` files and the `.hash` files of the last two; and neither do
`.lake/build/ir`, `.lake/build/bin`, or mathlib and the packages it depends on under `.lake/packages` (`batteries`,
`aesop`, `Qq`, `plausible`, `proofwidgets`, `importGraph`, `LeanSearchClient` and `Cli`). Other build output in
`.lake`, such as `.a` or `.dylib` files, goes up.

**Verilog sources.** Without a filelist (an `aristotle.f` in the folder, `-f` or `-F`; see
[Verilog build configuration](verilog.md)), if ignore rules leave out every `.v` and `.sv` file, as with Verilog
generated from templates and ignored, `submit` names up to five, each with its `.gitignore` rule where git can say. It
suggests `--include-ignored`, or a filelist, which uploads every file it names, ignored or not. A header, such as a
`.vh` file, isn't a source. If no `.lean`, `.c` or `.rs` file goes up either, there's no design to verify, so `submit`
exits 2 before uploading, as `--dry-run` does; otherwise both warn.

**Files with a prompt.** `continue --files PATH...` uploads selected local files when the project is idle. File
arguments are relative to the current directory or absolute. `--project-dir ROOT` determines their paths in the project; it defaults to the current
directory. For example, `--project-dir out --files out/Proofs/Foo.lean` sends `Proofs/Foo.lean`. Selecting just one file
does not remove its parent directories. Files outside ROOT, also through links, paths containing `..`, duplicate
paths and names that multipart cannot preserve are usage errors (exit 2), checked before any request.

`download --artifact` saves files at their project-relative paths beneath `--destination` (default:
`aristotle-artifacts`). Use that folder as the root when uploading them again. Plain `download` saves the archive
without extracting it or changing its directory structure.

**Long prompts.** `--prompt-file`, on `submit` and `continue`, reads the prompt from a file, for one too long for the
command line: Linux takes at most 128 KiB in one argument, and fails a longer one before the command starts.
`--prompt-file -` reads a pipe, and refuses a terminal, which would cut a long line short. The file is read as UTF-8,
or as UTF-16 with a byte-order mark, as Windows PowerShell 5.1 writes it, without its trailing newlines:
`aristotle submit --prompt-file prompt.md --project-dir ./rtl --wait`.

## Referring to a project

Wherever a command takes a `<project-id>`, it also takes the project's name or the first 8 or more characters of its
id, as in `aristotle show "FIFO overflow"` or `aristotle download 2fa30605 --destination result.tar.gz`. A full id, or
a name shaped like one, is used as an id, with no extra request. Anything else is looked up among your 2,000 most
recent projects, as an id prefix (case and dashes don't matter) or an exact name, then as the name in another case. If
several match, the command lists them with their ids and exits 2, doing nothing; if none does, it exits 4. Task ids
(`--task`, or a task given to `cancel`) are always given in full.

## Following and waiting

At a terminal, `show` follows a running task until it ends. Piped, as in `show <id> | tail`, it prints the task as it
stands and says how to wait for it, as `--json` does.

A wait (`submit --wait`, `continue --wait`, or `show` of a running task) ends with how the task ended and, if the
project has properties, a count such as `Properties: 25 proved · 5 refuted · 0 in progress`. If it breaks off or is
interrupted, or the task fails, is cancelled or runs out of budget, it prints on stderr the command that carries on the
same way:

| The wait | Ended | The command it prints |
|---|---|---|
| `--non-interactive` | Broke off or was interrupted | `aristotle show <project-id> --task <task-id> --non-interactive` |
| `submit --wait` or `continue --wait` | Broke off or was interrupted | `aristotle show <project-id> --task <task-id> --wait` |
| Any | Task failed, cancelled or out of budget | `aristotle continue <project-id> Continue`, with the wait's `--wait`, `--wait --json` or `--non-interactive` |
| Any, with a `--destination` | Task failed, cancelled or out of budget | `aristotle continue <project-id> Continue`, which takes no `--destination`, then `aristotle show <project-id>` with it |

A task that billing stopped ends `OUT_OF_BUDGET` with a `budget_failure_reason`. The wait prints Aristotle's message
about it, and says when to resume: once credits are bought, in a few minutes when Aristotle couldn't read them, or,
under the free credits' limit of one active project, once no other project of the organization is running.

The `show` commands keep `--json` and `submit`'s `--destination`. Interrupted after the wait has seen the task finish
and before the result archive is saved, or during the download, `submit` and a `show` with `--destination` say it wasn't
saved, and name the `aristotle download` command that saves it there. `show --wait` waits as `submit --wait` does,
without stopping for Aristotle's questions: at a terminal it offers to answer them, and otherwise Aristotle carries on
without answers after 15 minutes. It saves the result archive to `--destination` if given, and exits on how the task
ended.

## Results and artifacts

A wait that ends saves the result archive, or prints the `aristotle download` command that saves it:

| Wait | Saves the result archive |
|------|--------------------------|
| `submit --wait` or `--non-interactive` | To `--destination`, else into the current folder. |
| `show --wait` or `--non-interactive` | Only to a `--destination`; without one, it prints the command. |
| `continue --wait` or `--non-interactive`, and a plain `show` at a terminal | Never; it prints the command. |

`submit` without a wait ignores `--destination` and warns that it does; 4.0 will refuse it.

`--destination`, on `download`, `submit --wait`, `show --wait` and `show --non-interactive`, is a file to write,
replaced if it exists, or a directory to save into under the archive's own name, such as
`<project-id>-aristotle.tar.gz`. A path ending in `/` is a directory, and missing directories are made.
`submit --wait` checks it before submitting anything, and `show` before sending any request.

**Artifacts** are files Aristotle linked to a property, such as a refuted property's counterexample trace or a
testbench that reproduces it. `aristotle property` lists them as `Artifacts`, then the command that downloads them into
`aristotle-artifacts`. So that the command pastes the same into any shell on any system, it names only paths of
letters, digits and `_.+-`, with `/` between folders, that every system can save; give any other with `--artifact`
yourself, quoted for your shell.

`download --artifact` saves files by their paths in the project, such as a property's `artifact_paths`, instead of the
archive: `aristotle download <project-id> --artifact sim/trace.vcd --artifact sim/trace_tb.sv`. Each goes at its path
under `--destination`, a folder, by default `aristotle-artifacts` in the current one, keeping its folders and
replacing a file there. Paths are relative, with `/` between folders. These exit 2 before anything is downloaded:

- a `.`, `..` or empty segment, a backslash, a drive or a leading `/`;
- a `.git` folder or a file `HEAD`, in any case, which keeps git from taking the files for a repository;
- on Windows, a name Windows would save otherwise, such as `CON` or a name ending in a dot;
- a path a link in the folder would lead out of it, into a `.git` folder or a git directory, such as a bare
  repository, or to a file `HEAD`; or a folder inside a `.git` folder or a git directory, as named or through links;
- without `--destination`, an `aristotle-artifacts` that is a link: the message says where it leads, and
  `--destination` names another folder.

A path the project has no file at is named, the rest are saved, and the command exits 4. Aristotle sends contents,
not modes, so the files are saved without execute permission, except one that replaces a file, which keeps that
file's mode. Run a script with the interpreter its `#!` line names, as in `bash aristotle-artifacts/sim/run_cex.sh`,
or `chmod +x` it first.

## Questions

Aristotle may ask something part-way through a run, whether or not you wait. It keeps working meanwhile, so skipping
or cancelling a question doesn't stop the run. When it needs an answer, it waits up to 15 minutes, then carries on
without one, leaving what depends on it unverified.

- Until they're answered, in the CLI or the web app, the progress display ends with
  `2 questions pending — answer with: aristotle questions <project-id>`. A new question brings back the whole
  pending set.
- The CLI prompts only at an interactive terminal. Piped, redirected or in the background, it prints the questions
  above that line. Without `--wait`, `submit` and `continue` print the `aristotle questions` command.

| Command | What it does |
|---|---|
| `aristotle questions <project-id>` | Lists the task's questions, to answer later or from a terminal that wasn't watching. At a terminal it offers to answer the pending ones; in a script or a CI job it only logs how many are pending. Once the run has ended, its open questions show as `UNANSWERED`, since no answer can reach Aristotle, and only the project's owner is offered to answer. |
| `aristotle questions <project-id> --answer <question-id>="Active low" --answer <question-id>=8` | Answers without prompting, by the ids in the `--json` output. Sends nothing unless every id names a question that is still open. |
| `--no-questions`, for a batch run nobody will answer | Aristotle can't ask: it verifies what doesn't need a decision only you can make, leaves the rest unverified, and names the open question in its summary, as when a question gets no answer. On `continue`, it applies only when the prompt starts a new task; a running task keeps its setting. |

In the SDK, `Project.create()`, `Project.create_from_directory()` and `Project.ask()` let Aristotle ask unless you pass
`agent_questions_setting=AgentQuestionsSetting.DISABLED`. `aristotle ci` and `aristotle review` never let a run ask;
`aristotle ci setup` does.

## Scripts and `--non-interactive`

`--non-interactive`, on `submit`, `continue` and `show`, waits without ever prompting. When Aristotle asks something,
it stops, prints the questions as one JSON object and exits 10:

```json
{"project_id": "<project-id>", "task": {"agent_task_id": "<task-id>", "status": "IN_PROGRESS", "...": "..."},
 "questions": [{"question_id": "<question-id>", "content": "Is the reset active low?", "suggestions": ["Yes", "No"],
                "...": "..."}]}
```

Answer them, then carry on with the `show --non-interactive` command it prints on stderr, which keeps `submit`'s
`--destination`, so the archive still lands there. A question stays open until the run ends, so that command stops
again, with the same questions, until all are answered. To leave a decision to Aristotle, say so, as in
`--answer <question-id>="Use your best judgment"`. A run submitted without waiting can ask too, and
`show --non-interactive` picks its questions up. Questions `aristotle questions` doesn't list, which some runs ask only
as events that `show` displays, can't be handed back: `--non-interactive` logs them on stderr and waits on.

```bash
aristotle submit "Prove the FIFO never overflows" --project-dir rtl --destination result.tar.gz \
  --non-interactive > run.json
status=$?
while [ "$status" -eq 10 ]; do
  asked=$(jq -c '[.questions[].question_id]' run.json)
  # Read the questions from run.json and decide the answers.
  aristotle questions <project-id> --answer <question-id>="Active low"
  answered=$?
  aristotle show <project-id> --non-interactive --destination result.tar.gz > run.json
  status=$?
  # A refused answer that leaves the same questions open would loop forever.
  if [ "$status" -eq 10 ] && [ "$answered" -ne 0 ] &&
     [ "$(jq -c '[.questions[].question_id]' run.json)" = "$asked" ]; then
    status=$answered
    break
  fi
done
exit "$status"
```

The loop ends when the run does: 0 if the task completed, 1 if it finished without completing the request, with
`run.json` holding the finished run in the command's `--json` shape. Whichever command finished the wait saved the
result archive to `--destination`, and its `result_path` says where (`null` when the project has no files), so the
script needs no `aristotle download`. Any other code is in [the table](#exit-codes). After a 3,
`aristotle show <project-id> --json` tells what happened, and `show --non-interactive` picks a broken-off wait up again.
If `submit` fails after printing `Project created:`, the project exists: resume it rather than submit again.

## Property statuses

The README's [Read the results](../README.md#read-the-results) says what each word `aristotle properties` shows
means. Each comes from the status Aristotle reports, with `IN PROGRESS` and `UNRESOLVED` as the web app shows them:

| Status | `aristotle properties` shows |
|---|---|
| `PROVED` | `PROVED` |
| `REFUTED`: refuted by a counterexample, a proof of the negation, or both | `REFUTED`, with the kind in `EVIDENCE` |
| `IN_PROGRESS`: no verdict yet | `IN PROGRESS` |
| `FAILED`: the run stopped short of it, or its proof no longer checks | `UNRESOLVED`, with the reason |

Every `REFUTED` property requires `evidence`: `COUNTEREXAMPLE` (1), `NEGATION_PROVED` (2) or `BOTH` (3).
`aristotle property` shows the evidence kind separately and the agent's `evidence_message` before `Artifacts`.
The SDK preserves the message as received, including inline Markdown. It normalizes old API responses to this
contract, inferring missing evidence without creating a message. A kind of evidence this version doesn't know shows as
`UNKNOWN`, and as `0` in JSON. A `Declaration` created in Python requires explicit evidence of a known kind.

- A refuted property's failing case is in the run's summary, and the project's files (`aristotle download`) can hold
  a reproducer under `sim/`.
- `--status` takes `REFUTED`, `PROVED`, `IN_PROGRESS`, `UNRESOLVED`, `FAILED` or `ALL`, and repeats.
  `NOT_VERIFIED`, the old name for `UNRESOLVED`, still works. `--status REFUTED` lists every refutation; the filter
  does not select evidence kinds.
- A proof or refutation Aristotle never checked shows `(unchecked)` after its status until a later task updates it,
  because the task that set it ran out of budget or, for a proof, failed or was cancelled ([the rule](#json-output)).
  `--status` goes by the reported status, so `--status PROVED` lists unchecked proofs too, though `show` and `--json`
  count them as unchecked. In `--json`, each `properties` and `property` record says so in `unchecked`, and keeps its
  `status` as reported.
- `--created-by you` lists only the properties you asked for, and `--created-by aristotle` those Aristotle proposed.
  `--kind definition` lists the definitions Aristotle wrote as supporting context instead of properties: they're never
  proved or refuted, so it takes no `--status`.
- `aristotle property` looks among properties and definitions both. A name that matches several lists them, with
  their kinds and ids, and exits 2: give the full name or the id, or, when a property and a definition share the name,
  `--kind property` or `--kind definition`.

## Proxies and private networks

The client honors `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY`, with an `http://`, `https://`, `socks5://` or `socks5h://`
proxy. Behind a TLS-inspecting proxy, point it at your internal CA bundle with `ARISTOTLE_CA_BUNDLE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` or `SSL_CERT_FILE`. The only host it needs is `aristotle.harmonic.fun`, on
443. The CI guide's [Security and data handling](aristotle-in-ci.md#security-and-data-handling) has the rest.

A refused request, an HTTP 403, reads:

| When | `Error:` line | `Hint:` line |
|---|---|---|
| It lacks Aristotle's JSON error format and goes through a configured proxy | `request blocked by a proxy (403 Forbidden, via proxy.example.com)` | `ask your network admin to allow aristotle.harmonic.fun.` |
| Its source can't be identified | `request refused (403 Forbidden)` | `this may come from a proxy or firewall on your network. Run with --verbose to see the response headers.` |
| Aristotle refuses the key during login | `this account doesn't have access (403)` | `it may be awaiting approval or no longer in the key's organization. Contact aristotle@harmonic.fun.` |

`--verbose`, as in `aristotle login --verbose`, adds sanitized response headers and a short excerpt, such as an HTML
page's title. Trusting the corporate CA lets HTTPS connect, but doesn't override the proxy's access policy. A proxy
can return JSON too, so the format is a clue to the source, not proof.

Each request's `User-Agent` header names the client, as in `aristotlelib/3.1.0 (cli; python/3.12; linux)`: the SDK's
version; `cli` for a command, `ci` for `aristotle ci` and `aristotle review`, `action` when the GitHub Action runs
them, or `library` for a Python program; then the Python version and operating system. Nothing else: no arguments,
paths, host or user names, or anything about your projects.

## JSON output

Every command that returns data takes `--json`: the result goes to stdout as JSON, and nothing else does; progress,
warnings and errors go to stderr.

- A listing's `pagination_key` is the next page's key, as `--pagination-key` takes it, or `null` on the last page.
  It's printed on stderr too, as without `--json`.
- With `--wait`, `submit --json`, `continue --json` and `show --json` wait with no progress display or prompt, then
  print the task as it last stood: finished, or still running if the wait broke off (exit 3). Read `task.status` with
  the exit code.
- A `--json` wait (`submit --wait`, `continue --wait`, `show --wait`, `show --non-interactive`) cut short by Ctrl-C or
  a SIGTERM, such as `timeout` sends, still prints the task as it last stood and exits 130: `property_counts`,
  `requested_counts` and `properties` are null, and `result_path` names the archive only if it was saved first. So its
  output file holds the run's last known state.

| Command | Prints |
|---------|--------|
| `list` | `{"projects", "pagination_key"}` |
| `tasks` | `{"project_id", "tasks", "pagination_key"}` |
| `show` | `{"project_id", "project", "task", "web_url", "events", "result_path", "property_counts", "requested_counts", "properties"}` |
| `submit` | `{"project", "task", "web_url", "result_path", "property_counts", "requested_counts", "properties"}` |
| `continue` | `{"project_id", "project", "task", "web_url", "property_counts", "requested_counts", "properties"}` |
| `cancel` | a task |
| `rename` | a project |
| `download` | `{"project_id", "result_path"}` |
| `download --artifact` | `{"project_id", "files", "missing"}` |
| `login` | `{"verification_uri", "user_code", "expires_in"}` while it waits for approval in the browser, then `{"credentials_path", "key_name"}` |
| any `--non-interactive` wait that stops for questions (exit 10) | `{"project_id", "task", "questions"}` |
| `submit` or `continue` whose request may have gone through (exit 7) | `{"outcome": "unknown", "error"}` |
| `submit` or `continue` whose request didn't get through or was refused for too many runs or by billing, or `continue` or `questions --answer` whose lookup failed before anything was sent (exit 3, or 6 when rate limited or out of credits) | `{"outcome": "not_processed", "error"}` |
| `questions` | `{"project_id", "task", "questions"}` |
| `properties` | `{"project_id", "declarations", "pagination_key"}` |
| `property` | a declaration |

| Command | Notes |
|---|---|
| `show` | Waits only with `--wait`, even for a running task. `task`: the one asked for, else the latest (`null` if none). `events`: its latest events, oldest first; none for a finished task unless `--limit` asks. `result_path`: where `--destination`, with `--wait` or `--non-interactive`, saved the result archive: `null` without `--destination`, and otherwise as in `submit`'s. |
| `submit` | `task`: the project's task (`null`, with a warning on stderr, if it couldn't be listed; `--wait` then exits 3, naming the `aristotle show` command that waits for it). `result_path`: where `--wait` saved the result archive; `null` without `--wait`, with nothing to download, when the wait ended first, or when saving failed, which exits 3. |
| `continue` | `task`: the one the prompt went to, new if the project was idle, else the running one it steered. |
| `show`, `continue` | `project`: as it stands when printed, after any wait (`null` for `show --task` if it couldn't be fetched). `web_url`: its page in the web app, as in `submit`'s. If an interrupt cut the result short, or the project couldn't be refreshed, its `status` and `latest_run_status` are the latest task's, which the wait kept fresher, unless `show --task` names a finished task, which may be older. |
| `properties` | Reads every page itself, so its `pagination_key` is always `null`, and it prints every match unless `--limit` is given. |
| `download --artifact` | `files`: each file saved, in the order given, with its `artifact_path` in the project and its `saved_path`, where it went. `missing`: the paths with no file, which exit 4. |
| `login` | The first object as soon as it waits for approval, so a program can give a person the page to open (`verification_uri`) and the code to enter there (`user_code`); `expires_in`: the code's life in seconds. The second once the key is stored: `credentials_path`, the file; `key_name`, the key's name on API keys, or `null` for a key given with `--api-key`, piped or pasted, which prints only the second. |
| `questions` | `task` is `null` when the project has none. |

**Properties and counts.** `show`, `submit` and `continue` give the properties as they stand when the result is
printed, after any wait, most recently updated first, leaving out any Aristotle withdrew. Each has its
`declaration_id`, `name`, `status` and `failure_reason` as its declaration has them, with `status_name` and
`failure_reason_name`, whether it's `unchecked`, its `source` and `source_name` (`USER` if you asked for it, `AGENT` if
Aristotle suggested it), its declaration's `evidence`, `evidence_message` and `artifact_paths`, and its `outcome`. The `outcome` is the word every command
uses for what the status comes to: `proved`, `refuted` (`REFUTED`), `in_progress` (`IN_PROGRESS`:
no verdict), `unresolved` (`FAILED`: the run stopped first, or the proof no longer checks), `unchecked`, or `unknown`
for a status this version of the CLI doesn't know.

| Topic | Detail |
|---|---|
| `property_counts` | Counts only `proved`, `refuted`, `in_progress`, `unresolved` and `unchecked`, once the task has ended. Each refutation counts once, including one with both kinds of evidence; evidence stays on each property. Until then, or for a task status this version doesn't know, it's `null`. For `show --task`, it waits for the project's latest task, which can be newer than the one shown. |
| `requested_counts` | Counts the same way, but only the properties you asked for, and is `null` when `property_counts` is. Gate on it when you named the properties, and on `property_counts` for an open-ended run, whose properties may all be Aristotle's. |
| Unchecked | Aristotle checks a task's results when it finishes, so a proof or refutation is unchecked if the task that set it, the newest created by its `last_updated`, ended first: any when that task is `OUT_OF_BUDGET`, and a proof when it's `FAILED` or `CANCELED`. It stays unchecked until a later task updates it, and a checked one stays checked when a later task that leaves it alone is cancelled. Unchecked ones count as nothing else in `property_counts`, and keep their reported `status` in `properties`. |
| `null` | The counts and `properties` are `null` if the properties couldn't be fetched, or the project's tasks couldn't be listed, since no verdict can then be told checked or not. When the tasks couldn't be listed, `show --json`, with or without `--wait`, exits as for the error (3 for a server error, 6 when rate limited), and `submit --wait` and `continue --wait` exit 3, after saving any files they would. Don't send the run again: `task.status` says whether it finished. `continue` without `--wait` still exits 0, as the prompt was sent. |

**Fields.** Timestamps are ISO 8601 in UTC, with a `+00:00` offset, such as `2026-10-11T09:50:21.413442+00:00`, which
Python's `datetime.fromisoformat` reads as an aware datetime. Numeric codes other than `evidence` have their names
in fields with `_name` added, such as `status_name`: `PROVED` beside `status` 3. A numeric code this version doesn't
know is `0`, named `UNKNOWN`. An unknown task status is `UNKNOWN`.

| Record | Fields |
|---|---|
| Project | `project_id`, `name` (`Project.description` in Python, as the API calls it), `status` (1 running, 2 idle) and `status_name`, `latest_run_status` (how its latest run stands or ended, named as a task's `status` is; `null` before its first run), `created_at`, `last_updated`, `has_input`, `has_files`. |
| Task | `agent_task_id`, `project_id`, `status` (`QUEUED`, `IN_PROGRESS`, `COMPLETE`, `COMPLETE_WITH_ERRORS`, `OUT_OF_BUDGET`, `FAILED`, `CANCELED` or `UNKNOWN`), `budget_failure_reason` (why billing stopped an `OUT_OF_BUDGET` run: 1 your organization ran out of credits, 2 Aristotle couldn't read its credits, 3 free credits allow one active project per organization; `null` when billing didn't stop it, and for someone else's shared project) and `budget_failure_reason_name`, `percent_complete` (deprecated: `null` from current servers; each property's `outcome` in `properties` says how the run stands), `description`, `output_summary`, `file_name`, `created_at`, `started_at` (when the run's job started, shortly before the run reports `IN_PROGRESS`; `null` while it is queued, for a run cancelled before it started, and from a server that doesn't send it, though a run that failed before it started may carry one), `last_updated_at`, `read_only` (someone else's shared project). |
| Event | `event_id`, `agent_task_id`, `event_type` (a number; see `aristotlelib.EventType`) and `event_type_name`, `content`, `explanation`, `file_path`, `suggestions`, `status` (1 complete, 2 awaiting an answer) and `status_name`, `created_at`, `duration_seconds`. `EventType.AGENT_FOLLOW_UP` (`18`) carries a post-run message in `content`, such as a bug-fix offer or an invitation to continue after reaching the time limit. It's informational, and doesn't open the question-answer flow: act on it with a new prompt, through `Project.ask()` or `aristotle continue`. |
| Question | `question_id`, `event_id`, `project_id`, `agent_task_id` (the task), `content`, `suggestions`, `response`, `state` (`PENDING` while it awaits an answer, `ANSWERED`, `IGNORED` when it closed without one, or, in `questions`, `UNANSWERED` when its run ended first, as the table shows it), `created_at`, `updated_at`, `blocking_since`, `ever_awaited`, `answered_at`, `ignored_at`. |
| Declaration | `declaration_id`, `project_id`, `declaration_type` (1 property, 2 definition) and `declaration_type_name`, `name`, `status` (0 unknown, 1 in progress, 2 refuted, 3 proved, 4 removed, 6 failed) and `status_name`, `failure_reason` (1 out of budget, 2 system error, 3 the proof no longer checks, 4 the run finished without a verdict on it, 5 the run was cancelled) and `failure_reason_name`, `evidence` (0 unknown, 1 `COUNTEREXAMPLE`, 2 `NEGATION_PROVED`, 3 `BOTH`; required for `REFUTED`, otherwise nullable), `evidence_message` (the agent's explanation, preserved as received; it may contain inline Markdown), `source` (1 Aristotle, 2 you) and `source_name`, `filename`, `source_file`, `source_line`, `source_anchor`, `lean_code`, `description`, `artifact_paths` (the paths in the project of the files Aristotle linked to it, such as a counterexample's trace, as `download --artifact` takes them; empty when there are none), `created_at`, `last_updated`. |

The shapes are the SDK's pydantic models: a program can validate against them, or take their JSON Schema, as with
`aristotlelib.results.TaskSnapshot.model_json_schema(mode="serialization")`, which names every key the output carries,
computed ones such as `outcome` and `status_name` included.

## Exit codes

Every command exits with one of these codes, so a script can tell a failed run from a mistyped id:

| Code | Meaning |
|------|---------|
| 0 | Success. |
| 1 | The task a wait followed (`submit --wait`, `continue --wait`, `show --wait`, a `--non-interactive` run, or a plain `show` at a terminal) finished without completing the request (`COMPLETE_WITH_ERRORS` or `OUT_OF_BUDGET`). Or the verdicts failed the build: `aristotle ci`'s on its manifest's properties, or `aristotle review`'s when `--fail-on` is `failure` or `no-verdict` ([What your team sees](aristotle-in-ci.md#what-your-team-sees)), a run Aristotle stopped for decisions only you can make included. Or `aristotle ci setup` found no proposed properties to write. |
| 2 | Usage error: a missing or invalid argument, input Aristotle can't take, a project name or id prefix that matches more than one project, or an `aristotle ci --project` run that isn't of this checkout. |
| 3 | The command couldn't complete: a server or network error outlasted the retries; a task you waited on failed or was cancelled; a wait ended with the task still running; an `aristotle ci` or `aristotle review` run's time budget cut it, whatever `--fail-on` is; an `aristotle ci` run, or an `aristotle review` run when `--fail-on` is `failure` or `no-verdict`, ran out of budget, or billing stopped it, before Aristotle checked its results; `aristotle login` checked a key but couldn't store it; a key a browser login handed out can't be sent; or an unexpected error. |
| 4 | The project, task, property or question doesn't exist or your key can't see it, no project has that name or id prefix, or Aristotle refused the action: cancelling a finished task (one already cancelled counts as cancelled), or `questions --answer` for a closed question, an ended run or someone else's project. Also `download` of a project with no files yet, or of an `--artifact` it has no file at. |
| 5 | No API key was found, Aristotle rejected it, it can't be sent (whitespace around a key is ignored, and what's left is empty or holds a space, a control character or a non-ASCII character), or `aristotle login` was denied in the browser or its code expired. |
| 6 | Aristotle is rate limiting your account: too many runs in progress or waiting to start, or too many requests at once, even after the retries. The message says which, with Aristotle's limits when it gives them. Retry later; for runs, wait for one of yours to finish, or cancel one. Or your organization doesn't have the credits to start a run: once an organization admin buys them at https://aristotle.harmonic.fun/settings/billing, run it again. Also an `aristotle ci` or `aristotle review` run still queued when its time budget ran out. |
| 7 | The request may have gone through: its response was lost after Aristotle may have acted on it, so it wasn't sent again, or an earlier copy was still in progress when the retries ran out. Check before sending it again ([Retries and lost requests](#retries-and-lost-requests)). |
| 10 | A `--non-interactive` wait stopped because Aristotle is waiting for answers; stdout holds the questions. |
| 130 | Interrupted with Ctrl-C, or ended by SIGTERM. A task you were following keeps running, and the log says how to follow or cancel it; `aristotle ci` and `aristotle review` cancel their run unless `--on-timeout leave` is set, and `aristotle ci --project` leaves it going unless `--on-timeout cancel` is. A `submit` interrupted after sending its create says the project may exist, which can take a few minutes to show, and how to find and cancel it; one interrupted before says nothing was sent. A `submit --wait`, or a `show` with `--destination`, interrupted after its wait has seen the task finish and before the result archive is saved, or during the download, says the archive wasn't saved, and names the `aristotle download` command that saves it where the wait would have. `aristotle ci`, `aristotle review` and `aristotle ci setup` interrupted once their create was sent look for the project by the name it was uploaded under for up to 10 seconds, and cancel its run once it starts unless `--on-timeout leave` is set; if they can't find the project, or its run hasn't started, they say how to find and cancel it. |
| 141 | On Linux and macOS: stdout or stderr was closed before everything was written, as by `\| head`. |

`aristotle show` reports a task without judging it: an ended task's status, or that of a running one it prints piped
or with `--json`, doesn't make it exit non-zero. It exits 3 when it stops following a running task before the end,
and any other error with its code above. A `show` that waits exits on how the task ended, as `submit --wait` does:
`show --wait` and `show --non-interactive` always, and a plain `show` once it has followed a task to its end at a
terminal.

## Retries and lost requests

| Case | What the CLI does |
|---|---|
| Too many requests at once (any 429 but the one for too many runs in progress), a server error, a proxy's 5xx or SOCKS failure, a dropped connection, a timeout, which includes a connection that isn't open within 25 seconds, as through a proxy that stops answering its handshake or tunnel request, or an answer that isn't Aristotle's, such as a captive portal's or a proxy's page: text, or no content type, that isn't JSON | Retries, waiting at least as long as `Retry-After` asks, and starts no retry more than 90 seconds after the first attempt, so a request gives up within about two minutes. |
| The reply to an upload | Waits 30 seconds plus the upload's size at 16 KB/s, up to 10 minutes, since its last bytes can still be on their way to Aristotle once written. |
| A server certificate this machine doesn't trust | Doesn't retry. |
| An outage while a wait (`--wait`, `--non-interactive`, `show`) follows a live run | Rides it out for up to 10 minutes, reconnecting every 30 seconds at most, then stops, saying why, while the run goes on. It says once that the connection failed and once that it's back, and the progress display shows the outage. |

Each process starts at most five requests a second, which keeps a script polling through the SDK under Aristotle's
per-second limit. That limit counts every request from your account, so commands run side by side can still exceed
it, and then lean on the retries.

**A request that may have gone through.** `submit`, `continue` and answers are sent again only when Aristotle can't
have acted on them. When one may have gone through, the command exits 7 and says so, and `submit --json` and
`continue --json` print `{"outcome": "unknown", "error": "<what failed>"}`. Check, and send it again only if it didn't
go through:

| After | Look in |
|---|---|
| `submit` | `aristotle list`, for a project newer than the command, which may take a few minutes to show up; after a `submit --name`, `aristotle show -- <name>`, where an older project of that name would show too |
| `continue` | `aristotle tasks <id>` |
| an answer | `aristotle questions <id> --json`, where an answered question has `response` set |

The rest handle it themselves:

| Command | When its request may have gone through |
|---|---|
| `cancel` | It's sent again like a read, since cancelling twice cancels once. If it still fails, or Aristotle refuses it, `cancel` reads the task, and a task that reads cancelled counts as cancelled. |
| `aristotle ci`, `aristotle review` | They look for the run themselves and follow it, and exit 7 only when they can't find a run that started. |
| `aristotle login` | When an answer that may have held the key is lost, the next answer decides, since Aristotle hands a key out once: one saying it's still waiting carries on, and anything but a denial exits 7. A key Aristotle made then shows on API keys, named after the machine, to revoke before logging in again. |

**A request that didn't get through.** When no attempt got through, because no connection opened, the HTTP library
refused to send the request, or Aristotle refused each as too many requests at once or because it couldn't read your
organization's credits, nothing was done. When Aristotle refused it for too many runs in progress, for credits or while
your organization is still setting up, no run was started and no prompt sent. Either way the command says so: it exits
3, or 6 for too many requests or runs and for credits, and `submit --json` and `continue --json` print
`{"outcome": "not_processed", "error": "<what failed>"}`. `continue` and `questions --answer` do the same when a lookup
they make first, such as the project's, fails with no response, a server error or too many requests at once. Then the
same command is safe to run again. Otherwise `questions --answer` sends each answer on its own, so send again only
those whose error says they weren't answered.
