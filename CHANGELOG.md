# Changelog

All notable changes to `aristotlelib`, the Aristotle Python SDK and `aristotle` CLI, are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [3.1.0] - 2026-10-29

### Added

- `budget_failure_reason` and `budget_failure_reason_name` on every task in `--json`: why billing stopped a run that
  ended `OUT_OF_BUDGET`, or `null` when billing didn't stop it or the project is someone else's. In the SDK they are
  `AgentTask.budget_failure_reason`, an `aristotlelib.agent_task.BudgetFailureReason`, and
  `AgentTask.budget_failure_reason_name`. `aristotlelib.agent_task.billing_wait()` says when such a run can go again,
  and `aristotlelib.api_request.billing_url()` gives the page where an organization admin buys credits. (#350)
- In the SDK, `AristotleAPIError.error_code` can also be the `code` of Aristotle's answer, such as `OUT_OF_CREDITS`,
  `SIGNUP_GRANT_PENDING` or `BILLING_UNAVAILABLE`, so a program can tell a billing refusal from other errors.
  `Project.create()` and `Project.ask()` raise these refusals as `AristotleNotProcessedError`, since no run was
  started, but sending one again succeeds only once the organization has the credits, has finished setting up, or
  has a balance Aristotle can read. (#350)

### Fixed

- `aristotle ci` and `aristotle review` stop with exit 2, naming the link, when `--directory` is reached through a
  symbolic link in the checkout, or a junction on Windows. On GitHub, GitLab, Jenkins and Buildkite they also stop
  when the current folder resolves outside the job's workspace, as where a step entered a committed link, and name
  both folders. A pull request could commit such a link to another folder on the CI machine, such as another job's
  checkout, and the run uploaded and verified that folder, so its file names and property text showed up in the
  job's log and reports. A link above the checkout, such as a mount or macOS's `/tmp`, still works. (#337)
- After a setup run that ends `COMPLETE_WITH_ERRORS`, as one that proposes properties without proving them usually
  does, `aristotle ci setup` says that Aristotle proposed the properties and that setup leaves proving them to
  `aristotle ci`. It said that Aristotle made progress but did not fully complete the request, which it still says
  when the run proposed no property. (#352)
- `aristotle cancel` and `AgentTask.cancel()` read the task when the cancel request fails, and count it as cancelled
  when it reads `CANCELED`. Aristotle marks a run `CANCELED` before it stops the run, which can take longer than the
  request's timeout and retries. A cancel that worked could then exit 3 with a `ReadTimeout`, and `AgentTask.cancel()`
  raised it. (#349)
- `aristotle show` prints each question Aristotle asked with its text and what became of it: your answer, that it
  is waiting for one, with the command that answers it, that it was closed without an answer, or that the run ended
  first. It printed "Aristotle asked: Asked a question" and said that Aristotle timed out and went with its best guess,
  even for a question you had answered or one still waiting. `AgentTask.show()` prints them the same way. (#351)
- A `submit`, `continue`, `aristotle ci` or `aristotle review` that billing refuses says so in Aristotle's words, and
  that no run was started or the prompt was not sent. One refused for credits names the page where an organization admin
  buys them and exits 6, and `submit` and `continue` print `{"outcome": "not_processed", "error"}` under `--json`. It
  printed Aristotle's answer as a raw dictionary, exited 3 and printed nothing on stdout. One refused while Aristotle
  can't read your organization's credits, which the CLI now sends again within its usual retries, or while your
  organization is still setting up, no longer says the project or prompt may have gone through, and exits 3 rather than
  7. `aristotle ci` no longer spends a minute looking for a project that was never made. Only the 409 for a request
  whose earlier copy is still in progress is sent again; any other 409 is final. (#350)
- A task billing stopped says so, prints Aristotle's message about it, and says when to resume it, where it said it ran
  out of budget and to resume it at once, including into the free credits' limit of one active project. `aristotle ci`
  and `aristotle review` report it as billing stopped, with a JUnit error of type `billing`. Results give the reason in
  the task's new `budget_failure_reason` and `budget_failure_reason_name`. (#350)

## [3.0.0] - 2026-10-27

The release candidates `3.0.0rc1`, `3.0.0rc2` and so on were pre-releases, not published to PyPI.

Continuation uploads now preserve paths relative to the current directory, or an explicit `--project-dir` root
(`project_dir=` in `Project.ask`). Earlier releases flattened uploaded filenames. Select downloaded files beneath
their download root to retain their project paths; files outside the root and traversal paths are refused.

The CLI can now be driven by a script, a CI job or a coding agent. It keeps an API key once you log in, prints
`--json` from every command that returns data, exits with codes from one table, and takes answers to Aristotle's
questions at a terminal or from a script. `aristotle ci` checks the properties listed in `aristotle.toml` as a CI
step, and `aristotle review` has Aristotle work out what should hold of a change and check it. This release removes
ask mode and `aristotle formalize` and changes exit codes, so read [Migrating from 2.x](#migrating-from-2x) before
upgrading a script.

### Migrating from 2.x

#### Exit codes

In 2.1.0 a handled error exited 0 (a missing or rejected key, rate limiting, `cancel` finding no task), a usage
error exited 2, and any other error was a Python traceback with exit 1. Every command now exits with one of the codes
listed in the [CLI reference](docs/cli-reference.md#exit-codes).

What a 2.x script will notice:

- A missing or rejected key exits 5, and rate limiting exits 6. Both used to exit 0.
- An id that does not exist or that your key cannot see prints one line and exits 4, where 2.1.0 printed a traceback
  and exited 1. Cancelling a task that has already finished also exits 4, unless it was cancelled, and so does
  `download` of a project that has no files yet, which 2.1.0 also reported with a traceback.
- `submit --wait` and `continue --wait` exit on how the task ended: 0 for `COMPLETE`, 1 for `COMPLETE_WITH_ERRORS`
  or `OUT_OF_BUDGET`, and 3 for `FAILED`, `CANCELED` or a wait that broke off. All of these used to exit 0. A wait
  that broke off no longer downloads anything (see Changed).
- Invalid input exits 2 before anything is sent: an empty prompt, a `--project-dir` that is empty or not a
  directory, or a `--limit` out of range.
- `aristotle show` still exits 0 whatever the status of a task that has ended, or of a running one it prints piped or
  with `--json`. Following a running task to its end at a terminal, it exits on how the task ended, as
  `submit --wait` does, and it exits 3 when it stops following one before the task finishes.

A script that treated exit 1 as "something went wrong" should test for any non-zero code.

#### Ask mode

Ask mode is removed from the CLI and the SDK.

- `aristotle continue <project-id> "<prompt>" --mode ask|instruct` is a usage error (exit 2). Drop `--mode`:
  `continue` sends an instruction, which was the default.
- `aristotlelib.FollowUpMode` is removed, and `Project.ask()` no longer takes `mode`. Its second positional
  parameter is now `files`, so pass everything after the prompt by keyword:
  `await project.ask(prompt, files=[...])`.
- If you used ask mode only to ask about results, read them instead, with `show`, `properties`, `property` or
  `download`, or in Python with `Project.get_tasks()`, `Project.get_declarations()` and `Project.get_files()`: a
  follow-up to an idle project now starts a new run.

#### `aristotle formalize`

Removed. Aristotle verifies Verilog, C and Rust code: submit the folder that holds it, with the property to prove in
the prompt:

```bash
aristotle submit "<prompt>" --project-dir <folder> --wait
```

#### SDK behavior

- `AgentTask.wait_for_completion()` returns a list of the open questions it stopped for instead of `None`. The
  list is always empty unless you pass `stop_on_questions=True`. On Ctrl-C it reports that the task is still
  running and re-raises `KeyboardInterrupt` or `asyncio.CancelledError`, where 2.1.0 swallowed `KeyboardInterrupt`.
  When its event stream drops, or keeps answering a 429 or 5xx, it logs an error and returns with the task still
  running, where 2.1.0 raised `httpx.HTTPStatusError` for a status: check `task.live` after it returns.
- `Project.from_id()` and `AgentTask.from_id()` raise `AristotleNotFoundError`, a subclass of `AristotleAPIError`,
  for an id that does not exist or that your key cannot see, with the message `Project <id> not found or not
  accessible.` or `Task <id> not found or not accessible.` `except AristotleAPIError` and `status_code` checks
  keep working; code that matched the old message text does not.
- `AgentTask.cancel()` sends the cancel again after a lost reply, and returns normally for a task that is already
  `CANCELED`, where 2.1.0 raised `AristotleAPIError`. After any other 404 it refreshes the task, so its fields are
  current, before raising.
- The event stream raises `AristotleAPIError` instead of `httpx.HTTPStatusError`.
- `aristotlelib.api_request.BASE_URL` is removed. Set `ARISTOTLE_WEB_URL` to use another Aristotle origin.
- With no key from `set_api_key()` or `ARISTOTLE_API_KEY`, the SDK uses the key `aristotle login` stored instead
  of raising. An empty `ARISTOTLE_API_KEY` counts as unset.
- Importing `aristotlelib` no longer configures logging. 2.1.0 added a stderr handler to the root logger, set it to
  `INFO` and turned httpx's logging off, so a program's own `logging.basicConfig()` did nothing and its `INFO` records
  printed. The SDK logs to the `aristotle` logger, which prints nothing until the program configures logging, so
  `AgentTask.show()`, a wait's closing messages and the rest of the SDK's messages stay silent until then;
  `logging.basicConfig(level=logging.INFO)` shows them. The CLI's output is unchanged.
- The wheel ships `py.typed`, so mypy and other type checkers check code against the SDK's annotations instead of
  skipping the package, and can report errors that they skipped before.
- `Project.create()`, `Project.create_from_directory()` and `Project.ask()` let Aristotle ask questions unless
  `agent_questions_setting=AgentQuestionsSetting.DISABLED` is passed. 2.1.0 turned them off by default.
- `Project`, `AgentTask` and `Event` serialize as the CLI's `--json` does. `model_dump(mode="json")` and
  `model_dump_json()` write timestamps with `+00:00`, which `datetime.fromisoformat` reads as aware, so arithmetic with
  a naive datetime raises `TypeError`. `model_dump(by_alias=True)` and the serialization schema call
  `Project.description` `name`. A timestamp given with a zone is stored as naive UTC, as the API sends them, where
  2.1.0 kept the zone. (#245)
- `Project.create_from_directory()` raises `aristotlelib.project.ArchiveError` for a file it can't read or an
  archive it can't write: an `OSError` that keeps the original's `errno`, `strerror` and `filename` and wraps it
  as `__cause__`. `except OSError` and `errno` checks keep working; `except FileNotFoundError` or
  `PermissionError` no longer catches it. (#221)

### Upgrading from a 3.0.0 release candidate (beta users)

From 2.1.0, skip this section.

Update scripts and manifests from 3.0.0rc12 or earlier for the refutation contract:

- Use `DeclarationStatus.REFUTED` (2); `NEGATED` and `COUNTEREXAMPLE` (5) are removed. `FAILED` stays 6.
  Each refutation requires `DeclarationEvidence.COUNTEREXAMPLE` (1), `NEGATION_PROVED` (2) or `BOTH` (3), except that
  one read from a server with a kind this version doesn't know has `UNKNOWN` (0), and still counts as refuted.
  Python-created declarations require explicit evidence of a known kind. The SDK infers missing evidence only when
  reading old API responses and preserves `evidence_message` as received.
- Use `properties --status REFUTED`; `NEGATED`, `COUNTEREXAMPLE` and `DISPROVED` filters are removed.
- Read `refuted` outcomes and counts. `PropertyCounts` has only `proved`, `refuted`, `in_progress`, `unresolved`
  and `unchecked`; remove uses of `negated`, `counterexample` and `disproved` counters.
- Change `expect = "disproved"` to `expect = "refuted"` in `aristotle.toml`; the old value is rejected.
  `--fail-on failure`, `no-verdict` (also accepted as `unresolved`) and `never` keep their behavior.

Scripts from before 3.0.0rc7 also need these JSON changes:

- `list`, `tasks`, `properties` and `questions` print an object rather than a bare array: `{"projects",
  "pagination_key"}`, `{"project_id", "tasks", "pagination_key"}`, `{"project_id", "declarations",
  "pagination_key"}` and `{"project_id", "task", "questions"}`, so `.[0]` becomes `.projects[0]` and so on. The next
  page's key is in `pagination_key`, as well as on stderr.
- In `property_counts`, `in_progress` counts `IN_PROGRESS` properties and `unresolved` counts `FAILED` ones, never a
  disproof, and each property's `outcome` says `in_progress` and `unresolved` for them. 3.0.0rc6 and earlier called the
  `FAILED` count `failed`; 3.0.0rc7 to rc10 used `unresolved` for `IN_PROGRESS` and `not_verified` for `FAILED`, in
  `property_counts` and `outcome` both, so a script reading `unresolved` from rc10 or earlier now gets the `FAILED`
  count.
- A question's `agent_run_id` is now `agent_task_id`, as every other record has it.
- `download --json`'s `path` is now `result_path`, as in `submit --json`.
- A project's `description` is now `name`. In Python it is still `Project.description`.
- Timestamps end in `+00:00`. Python's `datetime.fromisoformat` reads them as aware datetimes, so arithmetic with a
  naive datetime raises `TypeError`: compare them with aware ones, or validate the JSON with the SDK's models, whose
  attributes stay naive UTC.
- `aristotle login` with nothing on stdin, or with stdin closed, logs in by approval in the browser and waits for it,
  where it used to exit 5 at once, saying no key was entered. A script that logs in pipes the key or passes
  `--api-key`.
- `aristotle logout` takes no `--api-key`, which it never used: `logout --api-key <key>` exits 2. (#298)
- A plain `show` that follows a running task to its end at a terminal exits on how the task ended, as `show --wait`
  does: 1 when it finished without completing the request, and 3 when it failed or was cancelled. It exited 0. (#299)
- The GitHub Action's `on-timeout` input is empty by default, which leaves a run that `ci --project` reports on, set
  with `ARISTOTLE_PROJECT`, going when the budget runs out. A step that relied on the old default, `cancel`, sets
  `on-timeout: cancel`. (#299)
- `submit`'s `-f` and `-F` take the path of one filelist as written. A pattern, such as `-F 'rtl/*.f'`, a list, such
  as `-F 'a.f,b.f'`, or a folder with a trailing slash, such as `-F rtl/`, exits 2, where it read each filelist it
  matched or listed, or every file in the folder. A script names each filelist with an `-f` or `-F` of its own.
  (#300, #303)
- A source, `-v` file or `-I` folder that a generated `aristotle.f` would name by a path holding `*` or `?`, or a name
  that starts with `...`, stops `submit`, `ci` and `review` with exit 2, where it went up and Aristotle read that path
  as a pattern. So does an `-I` folder whose path holds a comma, which Aristotle read as a list. Rename the file or
  folder. (#303)
- `properties` reads every page itself and takes no `--pagination-key`, which exits 2. A script that paged it drops
  the option, the `--limit` it paged by, and its loop: with `--limit 100`, the first call now returns the 100 most
  recently updated properties and a `null` `pagination_key`, so such a loop stops there without an error. Without
  `--status`, `--sort`, `--created-by` or `--limit`, `properties --json` prints every property, where it printed the
  30 most recently updated and the next page's key. (#310)
- `AgentTask.wait_for_completion()` defaults to `interactive=False`, so it draws no progress display and asks no
  questions at a terminal. A program that relied on the old default passes `interactive=True`. (#330)

The result models, all new in 3.0.0, follow: `PropertyCounts.in_progress` and `.unresolved`,
`Question.agent_task_id` and `DownloadResult.result_path`.

The plugin `aristotle@harmonic` now installs the skill from the release's tag. A Claude Code install from before that
stops loading after `claude plugin marketplace update harmonic`, until `claude plugin update aristotle@harmonic`
follows, so run both.

The GitHub Action pulls the release's published image, where a release candidate's Action built the image on the
runner. That image is built for x86-64 only, so a job on an Arm runner needs an x86-64 runner or x86-64 emulation.

### Added

- `--verbose`, before or after any command, `ci setup` included, shows a refused request's response on stderr: for an
  HTTP 403, its status, its headers and a short excerpt of its body, such as an HTML error page's title, so a refusal
  from a proxy or a firewall can be told from Aristotle's. It redacts the API key, authorization values, cookie
  values wherever the response echoes them and the proxy's credentials, as written or Base64-encoded, replaces the
  value of any header named for authorization, a cookie, an API key, a token or a secret, and prints no terminal
  control characters. (#292)
- A property's artifacts, the files Aristotle linked to it, such as a refuted property's counterexample trace or a
  testbench that reproduces it. `aristotle property` lists them and prints the command that downloads them, and
  `property --json`, `properties --json` and the `properties` of `show --json`, `submit --json` and `continue --json`
  carry them as `artifact_paths`. `aristotle download <project-id> --artifact <path>`, repeated for more, saves those
  files instead of the archive, each at its path in the project under `--destination`, by default
  `aristotle-artifacts`. Before any request it refuses a path with a `.`, `..` or empty segment, a backslash, a drive
  or a leading `/`, one that names a `.git` folder or a file `HEAD`, in any case or spelling git reads them in, so
  that git can't take the files for a repository, on Windows one with a name Windows would save otherwise, and one a
  link in the folder leads out of it, into a `.git` folder or a git directory, such as a bare repository, or to a file
  `HEAD`; a folder inside a `.git` folder or a git directory, as named or once links are followed; and, without
  `--destination`, an `aristotle-artifacts` that is a link, naming where it leads (exit 2). A path the project has no
  file at is named, the other files are saved, and it exits 4.
  `aristotle ci` and `aristotle review` list a refuted property's artifacts, with that command, in its failing row,
  its annotation and its JUnit failure; the run's summary is printed as before. The printed command names only paths
  that every shell reads as written, unquoted, and every system can save, so it pastes the same anywhere. In the SDK,
  `Declaration.artifact_paths` and `PropertyStatus.artifact_paths` carry the paths, and
  `Project.save_files(file_paths, folder)` downloads such files into `folder`, each at its path, yielding where each
  went, or None for a path the project has no file at.
- `show --wait` waits for a running task as `submit --wait` does, without stopping for Aristotle's questions, and
  exits on how it ended. With `--json` it prints what `show --non-interactive` prints, and `--destination` saves the
  result archive. A `submit --wait`, `continue --wait`, `show --wait` or `--non-interactive` wait that breaks off or
  is interrupted names the command that carries it on the same way: `show --wait`, with `--json` and
  `--destination` as given, or `show --non-interactive` after a `--non-interactive` wait. They used to say
  `aristotle show <id>`, which doesn't wait when piped or with `--json`; a plain `show` at a terminal still does, as
  it follows the task. In the SDK, `AgentTask.wait_for_completion` takes that command as `carry_on`, the text to
  name instead of `aristotle show <id>`. (#255)
- `--prompt-file PATH` on `submit` and `continue` reads the prompt from a file, as UTF-8 text without the newlines it
  ends with, or from a pipe with `--prompt-file -`, for a prompt too long for the command line. Linux takes at most
  128 KiB in one argument, and a longer prompt failed with `Argument list too long` before the command started. A
  UTF-16 file with a byte-order mark, as Windows PowerShell 5.1 writes, is read too. (#217)
- `aristotle login` stores an API key on this machine, and every later command uses it. It prints a link and a code,
  opens the link in a browser at a terminal when it can, and waits while you enter the code on that page, signed in to
  Aristotle, and approve. Aristotle then makes a key named after the machine, which the API keys page lists and can
  revoke. The code expires after 10 minutes. Over SSH or in a container, open the link on any device; a coding agent
  can run the command and pass the link and the code on, so the key never passes through the conversation.
  `login --json` prints `{"verification_uri", "user_code", "expires_in"}` as soon as it waits, and
  `{"credentials_path", "key_name"}` once the key is stored. A denied login or an expired code exits 5. When an answer
  that may have held the key is lost, the next one decides: still waiting carries on, and anything but a denial exits
  7 and says where to revoke the key. Ctrl-C exits 130, and says where to revoke the key too when it may be on its
  way or is being checked. A later browser login on the machine replaces the key the last one stored there: the
  credentials file records which keys a device login made, and Aristotle retires that one once the new key arrives,
  if it belongs to the same user and organization. A key on the first line of stdin, or given with
  `login --api-key <key>`, is checked and stored instead, and a server that can't approve a login in the browser gets
  the paste login: the API keys page, and a hidden prompt at a terminal. Every key is checked with Aristotle before it
  is stored. `aristotle logout` removes the stored key. At a terminal, a command that finds no key offers to log in
  first. The key is kept in `~/.config/aristotle/` (`$XDG_CONFIG_HOME/aristotle/` when that is set,
  `%APPDATA%\aristotle\` on Windows) in a file only you can read, and outside Windows `login` creates that directory
  with owner-only permissions. `--api-key` and `ARISTOTLE_API_KEY` take precedence over it on every other command. In
  the SDK, `AristotleRequestClient.post(authenticated=False)` sends a request without the API key, `post(redact=...)`
  rewrites what one request logs and raises, for a body holding a secret a server may quote back, and
  `AristotleAPIError.error_code` is the `error` string of the server's JSON error response. (#121, #152, #197, #276)
- `submit --name` names a project when it is created, and `aristotle rename <project-id> <name>` renames one.
  Without `--name`, a project is named after its `--project-dir` folder, else the top module of a block given with
  `-f` or `-F` (or `block` without one), else the prompt. The name goes with the create, so the project has it from
  the start; a server that doesn't take a name there gets a rename right after. (#132, #207, #221)
- `Project.create()` and `Project.ask()`, and so every `submit`, `continue`, `aristotle ci`, `aristotle review` and
  `aristotle ci setup`, send an `Idempotency-Key`, the same on each of the request's retries. Aristotle honors the
  key: a copy sent while the first with its key is still in progress is refused with 409, and the CLI retries that
  like a per-second refusal; a request whose retries run out after such a refusal exits 7, since the earlier copy may
  yet act. A request whose response is lost still exits 7 rather than being sent again, since the CLI can't yet tell
  whether a server honors keys. (#207)
- `aristotle properties <project-id>` lists what a project proves, filtered with `--status`, `--kind` and
  `--created-by` and ordered with `--sort`. Piped or redirected, it prints names in full. At a terminal, a name too
  long to fit loses the namespace it shares with another listed name, then its middle, as far as it stays distinct
  from the other listed names, and `aristotle property` takes it as shown.
  `aristotle property <project-id> <name>` shows one property in full: its statement, status, source location and
  the Lean it was formalized as. `aristotle show` adds a line counting the project's properties.
  A refuted property names its evidence, how Aristotle refuted it: `COUNTEREXAMPLE`, `NEGATION_PROVED` for a proof of
  its negation, or `BOTH`. `properties` shows it in an `EVIDENCE` column, and `property` adds Aristotle's message
  about it. `--json` gives it as `evidence`, 1 to 3, with `evidence_message`, and in Python `Declaration.evidence` is an
  `aristotlelib.declaration.DeclarationEvidence`, which a refuted `Declaration` you create must give. An evidence kind
  this version doesn't know, such as one a later server adds, reads as `UNKNOWN`, 0, and the property still counts as
  refuted. (#129, #197, #314, #342)
- Answering Aristotle's questions. At a terminal, `submit --wait`, `continue --wait` and `show` present the
  questions as they arrive, one set at a time, with a review screen before anything is sent.
  `aristotle questions <project-id>` lists a task's questions and offers to answer the pending ones. (#130)
- `--json` on `list`, `tasks`, `show`, `submit`, `continue`, `cancel`, `rename`, `download`, `questions`, `properties`
  and `property`. The result goes to stdout as JSON and everything else to stderr. A listing is an object that names
  what it lists beside the next page's `pagination_key`, and timestamps carry their `+00:00`. The shapes are the SDK's
  pydantic models, in `aristotlelib.results` and the record classes. Each numeric code but `evidence` has its name
  beside it, such as `status_name`: `PROVED` beside a property's `status` 3, and `failure_reason_name`,
  `event_type_name`, `declaration_type_name` and `source_name`. (#137, #245)
- `submit --json`, `continue --json` and `show --json` say which properties hold: `property_counts` counts them by
  verdict, and `properties` gives each one's `declaration_id`, `name`, `status` and `failure_reason`, as they stand
  after any wait. `continue --json` prints `{"project_id", "project", "task", "web_url", "property_counts",
  "requested_counts", "properties"}`, where 3.0.0rc1 to rc4 printed the bare task, and `show --json` carries the
  `project` and its `web_url` too. 3.0.0rc6 and earlier printed neither in `continue --json` or `show --json`. Fewer
  than 100 properties take one request, and longer lists are read at most two pages a second.
  `property_counts.refuted` counts each refutation once, and each property's `outcome` says what
  its status comes to: `proved`, `refuted`, `in_progress`, `unresolved`, `unchecked` or `unknown`.
  `property_counts` counts every property, Aristotle's suggestions included; `requested_counts` counts only those you
  asked for, which each property's `source` tells apart, and `show` and a wait add a `You asked for 5:` line when
  Aristotle suggested others. Gate on `requested_counts` when you named the properties. (#176, #245)
- A property whose proof no longer checks says so. When Aristotle's end-of-run check finds a proved property's proof
  missing, failing to build or using `sorry`, it marks the property failed with a reason of its own. `properties`
  shows `UNRESOLVED (proof broken)`; `property` and `aristotle ci`'s report, summary and JUnit say the proof no
  longer checks and to re-run to re-prove it; `show` adds a line counting them; and `--json` gives `failure_reason` 3.
  Earlier versions showed `FAILED (unknown)` and `0`. `aristotle ci` counts it as unresolved, a property without a
  verdict. (#192, #245)
- A wait ends with the line counting the project's properties that `show` prints: `submit --wait`, `continue --wait`
  and `show` of a running task print it under the task's outcome. `continue --wait`, and `show` once it has followed
  a run to its end or with `--non-interactive`, then print the `aristotle download <project-id>` command for a
  project with files, where `submit --wait` saves them. `show --non-interactive --destination <path>` saves them
  there instead, and when `submit --non-interactive --destination <path>` stops for questions, the `show` command it
  prints to resume the wait carries the `--destination`, so the archive still goes where you asked. 3.0.0rc5 and
  earlier counted the properties only in `show`, before its wait, where at a terminal the progress display cleared
  the line, and a run that asked lost `submit`'s `--destination`. (#199)
- `--non-interactive` on `submit`, `continue` and `show` waits for the run without ever prompting. When Aristotle
  asks something, it prints the open questions as JSON and exits 10.
  `aristotle questions <project-id> --answer <question-id>=<answer>`, which can be repeated, answers without
  prompting, and `show --non-interactive` resumes the wait. (#138)
- Every command that takes a project id also takes the project's name or the first 8 or more characters of its id,
  matched among your 2,000 most recent projects. A name or prefix that matches more than one project lists them and
  exits 2 without doing anything. Task ids are still given in full. (#142)
- `aristotle ci` checks the properties listed in `aristotle.toml` (or `--manifest`) as a CI step, and reports each as
  a test named by its id, which stays the same across runs, so GitLab and Jenkins keep a history per property. A
  property is stated in plain language (`natural_language`), or names a SystemVerilog assertion that must hold (`sva`)
  or a C function that must have no undefined behavior (`c_function_no_ub`). A property the run registered nothing
  under has no verdict, as `not registered`. Properties Aristotle proposes on its own are listed as suggestions in the
  log and the GitHub job summary; they are never in the JUnit report and never affect the exit code. A run that
  verifies none of the manifest's properties fails with 1 unless `--fail-on never` is set. Every run proves every
  property in the manifest. A missing or invalid manifest exits 2 before anything is uploaded, and the error
  names every problem; `repo_id`, `check.max_cost_dollars`, `check.enable_proof_repair` and `property_result_id` are
  accepted but not yet enforced. It detects GitHub Actions, GitLab CI, Jenkins, Buildkite, CircleCI and Azure
  Pipelines, writes a JUnit report, and on GitHub also writes annotations and a job summary. `--fail-on`, `--timeout`
  and `--on-timeout` decide what fails the build and how long it runs; without `--timeout`, the manifest's
  `check.max_wall_time_seconds` sets the budget, else 60 minutes. `--dry-run` validates the manifest and shows the
  upload without submitting anything. Every flag that takes a value can also be set with an `ARISTOTLE_*` environment
  variable. It runs from the image `ghcr.io/harmonic-ai/aristotle:3` or as the GitHub Action
  `harmonic-ai/aristotle@v3`. It verifies Verilog and C on every account, and Rust on accounts where it's enabled. A
  run that never left the queue exits 6, whatever `--fail-on` is. (#119, #148, #184)
- `aristotle ci setup` writes `aristotle.toml`: at a terminal, Aristotle reads the repository, asks its questions and
  proposes properties, and the command writes them for you to review and commit, noting any it already proved or
  refuted. It never replaces an existing manifest without `--force`, and `--from <project>` (or `ARISTOTLE_FROM`)
  writes one from a finished project's properties instead of starting a run. (#149, #227)
- `aristotle review` has Aristotle work out what should hold of the files in scope and check it, reported as
  `aristotle ci` reports. `--scope changed|paths|all`, `--path`, `--base-ref` and `--instructions` (or
  `ARISTOTLE_SCOPE`, `ARISTOTLE_PATHS`, `ARISTOTLE_BASE_REF` and `ARISTOTLE_INSTRUCTIONS`) select what it verifies,
  and a change that touches no source file passes with an empty report. Its properties differ from run to run, so
  it is advisory: `--fail-on` defaults to `never`, and `--fail-on failure` makes it gate. `aristotle ci` refuses
  these flags with a one-line pointer to `aristotle review` (exit 2), and warns when one of the variables is set;
  `aristotle review` warns when `ARISTOTLE_MANIFEST` is. The GitHub Action runs it with `command: review`, configured
  by the inputs `scope`, `paths`, `base-ref` and `instructions`, and its `fail-on` input defaults to each command's
  own default. (#148, #153, #156)
- `aristotle ci` and `aristotle review` wait for a run slot when your account is at its limit of projects in
  progress, instead of exiting 6 at once. They name the projects they are waiting on and submit again once one
  finishes and no other run starts in its place. The wait comes out of the time budget, and if no slot frees up
  while a quarter of the budget is still left, the step exits 6 without a report. (#145, #148, #214)
- `aristotle ci`, `aristotle review` and `aristotle ci setup` name each run's project after the repository and the
  directory in it, what the run checks, and the branch and commit, as `rtl/hw: ci @ main 1a2b3c4`, rather than after
  the checkout's folder alone. `--name`, `ARISTOTLE_NAME` or the GitHub Action's `project-name` input names it
  instead, and `--dry-run` prints the name. If naming fails, the run goes ahead under the name it was uploaded under,
  the folder's name in ASCII with a random suffix, with a warning. (#162, #319)
- `docs/verilog.md` lists the `aristotle.f` syntax Aristotle reads, one option or file per line, with an example.
  `aristotle.f` is where a top module and defines go, since `aristotle ci` and `aristotle review` take no `--top` or
  `-D`. (#162)
- `submit --project-dir` reads the folder's `aristotle.f` as a simulator reads a filelist: nested `-f` and `-F`
  files, `+incdir+`, `+define+`, `-v`, joined forms, comments and `$VAR`, `${VAR}` or `$(VAR)`. It uploads every
  file the design names or includes, even one `.gitignore` excludes. A file from outside the folder goes up under
  `_ext/`, in a folder named after the variable that leads to it or, when none does, after the folder such files
  share; one reached through a link out of the folder keeps its place. Both are listed with where they are, in a
  warning when the folder's own `aristotle.f` is read, which also names each variable the filelists take. When the
  filelist uses options Aristotle doesn't read, or names files outside the folder, a generated `aristotle.f` in the
  options it reads goes up instead.
  Simulator options such as `-sv` or `-assert svaext` are listed and left out. A missing file, an unset variable,
  an unknown option, `--ignore-unknown-modules` or, when the file is generated, a value that isn't UTF-8 stops the
  command before anything is sent. An `` `include`` by absolute path, or one that would no longer find its header
  in the upload, gets a relative path in the uploaded copy, on the same line, unless it is inside a `` `define``:
  slang looks for that from wherever the macro is used, so it goes up as written with a warning, or stops the
  command when it names an absolute path. `submit --top`, `-f` and `-F` add lines to that `aristotle.f`, in the
  order given, or make one, and a `--top` replaces the filelists' top. `--top` and `--blackbox` need a filelist that
  names a source file, and exit 2 without one. Without `--project-dir`, only the design's
  files go up. A prompt that sends a design gets a line pointing Aristotle at its `aristotle.f`, as `ci`'s does.
  `submit --dry-run` lists every file of the design with where it comes from and why, each rewritten include and
  each option left out, the `aristotle.f` that would go up, the folder's other files and those an ignore rule leaves
  out, and the prompt, without sending anything or needing a key; like `submit`, it stops with exit 2 when nothing
  would go up. `aristotle ci` and `aristotle review` read a root `aristotle.f` the same way, upload it, or a
  generated one in its place, and every tracked file it reaches, ignore rules aside, and list them in `--dry-run`. A
  filelist or file it reaches, or one a link among them leads to, that git doesn't track in the folder the step
  uploads stops the step, and so does an environment variable in it. (#215, #220, #221)
- `aristotle ci` and `aristotle review` take `--allow-untracked <folder>`, repeatable, for a design whose sources the
  build generates or whose headers are in shared folders: a file the root `aristotle.f` reaches goes up from a folder
  it names though git doesn't track it there, and one from outside the folder the step uploads goes under `_ext/`, as
  with `submit`. A relative folder is from the current directory. A folder reached through a link, or that is or
  holds the folder the step uploads, the CI system's folder of the job's credential files, such as GitHub's
  `RUNNER_TEMP`, the home folder or a temporary folder, stops the step. It trusts the job's definition, as the API
  key does, so only the command line gives it; the GitHub Action passes no such option. A file in a `.git` folder,
  named like a credential or in a folder that holds them, or in `/proc`, `/sys` or `/dev` outside the folder the step
  uploads still stops the step, as does one a link leads out of every folder named. The log, as `--dry-run` does,
  lists each file it lets through with the folder that did, and `ci --project` checks a run's record with those files
  in it. Without the option, the message that stops the step names it where a folder it can take could hold the file,
  and a file in `/proc`, `/sys` or `/dev` is refused as one that never goes up, not as one outside the folder with
  advice to commit it there. (#271)
- `submit --include-ignored` uploads the files in `--project-dir` that an ignore rule matches too, such as Verilog a
  build generates from tracked templates: whatever a `.gitignore`, `.git/info/exclude` or the global excludes file
  matches, and, outside a repository or in a folder git ignores, what the folder's top-level `.gitignore` does. Files
  named like credentials or kept in a folder that holds them, `.git` folders, result archives an earlier run saved and
  files whose names aren't UTF-8 still stay out, as does a link that leads out of the folder, and the 100 MB limit
  holds. `--dry-run` marks each file it adds, and the upload's summary says how many of its files an ignore rule
  matches. Without the option, the warnings that name what an ignore rule leaves out, the tracked files it holds back
  and the Verilog sources, name it too. Only the command line turns it on, never an `aristotle.f`, and without
  `--project-dir` it is a usage error. In the SDK, `Project.create_from_directory(include_ignored=True)` does the same.
  (#295)
- `--blackbox <module>`, at any depth of `aristotle.f` and as a repeatable `submit` option, has Aristotle treat a
  module, such as a foundry cell or a behavioural model you don't want to send, as a black box with unconstrained
  outputs; the CLI writes no stub. Each file the filelists name, `-v` files included, that declares the module and no
  other design unit stays out of the upload, and so does such a `.v`, `.sv`, `.vh` or `.svh` file they don't name, in
  a folder that goes up whole, and the prompt gains a line naming every black box after the one naming `aristotle.f`.
  A module no file declares, such as a vendor cell with no source in the tree, gets that line alone. A `.v` file is
  read as Coq, so it is neither left out nor stopped on, when outside Coq's comments and strings it holds
  `Require Import` or `Export`, `Proof.` or `Qed.`, declares no module and ends with a sentence's `.`, and it uses no
  Verilog macro or directive, so no Verilog file is, however its names are spelled. A file that declares a black box
  beside another design unit, a header the design includes that declares one, and a black box that names the top
  module stop the command before anything is sent. `aristotle ci` reads it in the root `aristotle.f` the same way,
  and so does `aristotle review` whenever it reads the block, verifying no file a black box leaves out;
  `aristotle ci setup`, and a review that doesn't read the block, send the checkout's tracked files as before.
  `--dry-run` lists each black box with the files it leaves out, and the prompt's line, and `submit --dry-run --json`
  lists them under `block.blackboxes`. `--ignore-unknown-modules` still stops the command, and its message names
  `--blackbox <module>` as the way to leave a module's source out. (#270, #273, #283, #287)
- An Agent Skill, `skills/aristotle`, teaches coding agents such as Claude Code, Codex and Cursor to drive the CLI.
  With it, an agent submits a project and reads one JSON result, answers Aristotle's questions without a human,
  reads the properties, and keeps `aristotle.toml` in step with the code. Claude Code and Codex install it from
  this repository as the plugin `aristotle@harmonic`. Other agents use a copy of the directory. Both come from the
  latest release's tag, not from the main branch, so the skill describes the CLI that release installs. (#157, #256)
- `docs/agent-setup.md` is a setup guide written for a coding agent. Told to fetch and follow it, the agent
  installs the CLI and the skill, checks access with `aristotle list`, asks the user how to log in when there's no
  key, runs `aristotle login` for them to approve in their browser if they choose that, and reports a line for each
  part, with the steps only the user can take. (#269, #276, #329)
- A refusal for too many runs prints Aristotle's reason, which says how many can run at once and how many more can
  wait in the queue when Aristotle queues runs, and still exits 6. `aristotle ci` and `aristotle review` quote it
  while they wait to submit. Without a reason from Aristotle, the message is the one 2.1.0 printed. (#159)
- A wait that ends while its run is still queued says the run never started, and how to follow or cancel it. That
  covers Ctrl-C during `submit --wait`, `continue --wait` or `show`, and an `aristotle ci` or `aristotle review` time
  budget that runs out first. There the log, the GitHub summary and the JUnit report say so, and each manifest property
  is reported as `run never started`. A wait's progress display shows a queued run as submitted, not started, some
  time ago. (#159)
- A CA bundle for a TLS-inspecting proxy, read from `ARISTOTLE_CA_BUNDLE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`
  or `SSL_CERT_FILE`, whichever first names a file that exists. (#119)
- `ARISTOTLE_WEB_URL` selects the Aristotle origin for API requests, printed links and the stored key. It must be
  HTTPS unless the host is loopback. (#131)
- `submit` prints a link to the project in Aristotle Web (#112), and says how many files it uploaded and how many
  `.gitignore` left out (#123).
- Aristotle's follow-up after a run, an offer to fix a bug or to carry on past the time limit, shows in a wait's
  display as `AGENT_FOLLOW_UP: …` and keeps its type in `--json` and the SDK (`EventType.AGENT_FOLLOW_UP`, `18`), where
  2.1.0 reported it as `UNKNOWN` (`0`). It opens no question flow; reply with `aristotle continue`. (#140)
- SDK: `Project.rename()`, `Project.web_url()`, `Project.get_declarations()`, `Project.resolve_id()`,
  `Project.list_projects(latest_run_status=...)`, `AgentTask.read_only`, `aristotlelib.Question`,
  `aristotlelib.QuestionClosed`, `aristotlelib.AristotleNotFoundError`, `aristotlelib.AmbiguousProjectError`,
  `aristotlelib.ProjectHasNoFilesError` and `aristotlelib.AristotleProjectLimitError`, and `name=` on
  `Project.create()` and `Project.create_from_directory()`, which refuse an empty name with `ValueError`. (#141, #142,
  #145, #207)
- `aristotle ci` and `aristotle review` fail with 1 when Aristotle stopped the run for decisions only you can make,
  unless `--fail-on` is `never` (`review`'s default). They print the run's summary, which lists the open questions, under "Aristotle
  stopped before finishing:", and on GitHub add an error annotation pointing at it. (#147)
- `aristotle ci --project <project>` (or `ARISTOTLE_PROJECT`) reports on a run already started instead of starting
  one, such as a run left going with `--on-timeout leave`. It uploads nothing, waits for the run within the time
  budget, and reports the manifest's properties as a new run would, with the same JUnit report, job summary and exit
  code. A command whose budget runs out on a run left going says how to pick it up again. The skill and the CI guide
  show how a job or an agent that can't wait long starts a run and collects it in later commands. Each run
  `aristotle ci` starts ends its prompt with a record of the commit it started at, a digest of the files it uploads
  and one of the manifest's properties, which `--dry-run` shows. `--project` exits 2 before waiting unless the
  project's only run carries a record that matches the checkout: the same files, uncommitted changes included, and
  the same properties. The message says what differs, and to check out the run's commit, if the run started from a
  clean checkout, or start a fresh run, and gives the `aristotle cancel` command for a run that hasn't ended.
  (#208, #227, #233)
- `expect = "refuted"` on a manifest property marks a deliberate counterexample. `aristotle ci` passes its
  refutation, reported as `refuted, as expected` in the log, the JUnit report and the GitHub job summary, and fails a
  proof of it as `unexpectedly proved: update the manifest`, since the counterexample is gone. Without a checked
  verdict it has none, as any property. `aristotle ci setup` writes it where Aristotle says a property shows its
  counterexample on purpose, unless a checked proof settles it or Aristotle also expects it to be refuted, and names
  each one so you can make sure; a property Aristotle merely expects to be refuted keeps its note. Its warnings give
  one rule for a refuted property: fix the code, correct the property if it doesn't say what you want, or mark a
  deliberate counterexample, but don't drop or weaken a correct property to pass the build. The skill gives the same
  rule. (#206)
- A manifest property can carry `assumptions`: what it is checked under, in plain language, such as an answer to one
  of Aristotle's questions. `aristotle ci` sends them to the run with the property, so an `sva` property, whose
  statement is the assertion's name, can carry answers too, and its reports show them with the verdict, as "holds,
  assuming …" for a pass. `aristotle ci setup` writes the ones a property depends on,
  and when Aristotle stops a run for decisions only you can make, `aristotle ci` says to answer in a statement or in
  `assumptions`. The docs say which assertions `sva` can name: a labelled concurrent assertion
  (`<label>: assert property`), not an immediate or unlabelled one. (#204)

### Changed

- `properties` reads every page whatever its options, as it did with `--status`, `--sort` or `--created-by`, so
  `properties <project-id> --json` lists every property, and the table shows the 30 most recently updated and counts
  the rest. `--limit` takes any number from 1, and `properties` takes no `--pagination-key`: the `pagination_key` in
  its `--json` is always `null`. Without those options, 3.0.0rc10 and earlier showed one of the server's pages, 30
  rows unless `--limit` asked for up to 100, and printed the next page's key. (#310)
- `--api-key` is listed in `aristotle --help`, which takes it before any command and says that `login` checks and
  stores it, and in `aristotle login --help`. The other commands still take it after their name, as 2.1.0 did, but no
  longer list it, and the message for a missing key gives it before the command. `cancel` lists only its argument,
  a task id or a project; `--task-id` and `--project-id` still work. `review --path` and the Action's `paths` input
  say what the globs do under each scope: under `changed`, the default, only the changed files they match are
  verified, and under `paths` every file they match is. `login --no-browser`'s help says it only keeps the page from
  opening, as the page is printed either way. (#298)
- A plain `show` that follows a running task to its end at a terminal exits on how the task ended, as `show --wait`
  does: 0 for `COMPLETE`, 1 for `COMPLETE_WITH_ERRORS` or `OUT_OF_BUDGET`, and 3 for `FAILED` or `CANCELED`. It exited
  0 whatever the outcome. A plain `show` of a task that has ended, or of a running one piped or with `--json`, still
  reports it without judging it. (#299)
- Every request names the client in its `User-Agent` header, as `aristotlelib/<version> (<surface>; python/<x.y>;
  <os>)`, where 2.1.0 sent httpx's `python-httpx/<version>`. The surface is `cli` for a command, `ci` for
  `aristotle ci` and `aristotle review`, `action` for those run by the GitHub Action, and `library` for a Python
  program that imports `aristotlelib`; `<os>` is Python's `sys.platform`, such as `linux`, `darwin` or `win32`. It
  carries nothing else: no arguments, paths, host or user names, and nothing about a project. (#291)
- `questions --json` gives each question's `state`, as the table does: `PENDING`, `ANSWERED`, `IGNORED`, or
  `UNANSWERED` once its run has ended. 3.0.0rc6 and earlier left it out of the JSON. (#245)
- `aristotle list` has a LAST RUN column, how each project's latest run stands or ended, such as `COMPLETE` or
  `FAILED`, since STATUS says only whether a run is going. A project record in `--json` carries it as
  `latest_run_status`. (#245)
- One word for each outcome of a property, in every command, as the web app shows it: proved, refuted (`REFUTED`),
  in progress (`IN_PROGRESS`: no verdict yet), unresolved (`FAILED`, with the reason) and unchecked.
  `show` and a wait count `9 proved · 3 refuted · 0 in progress`, adding `1 unresolved` and
  `1 unchecked` when there are some. `aristotle ci` and `aristotle review` count `9 proved, 3 refuted, 0 in progress`
  in the same words, adding `1 unexpectedly proved`, `1 unresolved`, `1 not registered`, `1 unknown` and
  `2 unchecked (1 refuted and 1 proved)`; in progress counts only `IN_PROGRESS`, so a manifest property the run
  never registered doesn't read as work still going.
  `properties` and `property` show `REFUTED`, `IN PROGRESS` and `UNRESOLVED (out of budget)`, with the evidence kind
  separate from the status. `properties --status` takes `REFUTED` and `UNRESOLVED` beside the statuses. The
  CI reports label a refutation `refuted` and an unresolved property
  `unresolved (out of budget)`, and `aristotle ci setup` notes that Aristotle expects a property to be refuted.
  `--fail-on no-verdict` fails the build on any property without a verdict, in progress, unresolved or unchecked,
  and the log names those properties; `unresolved`, its old name, is still accepted by the flag, `ARISTOTLE_FAIL_ON`
  and the Action's `fail-on` input, so the word means only `FAILED` in what the CLI prints. Under `--json`, `outcome`
  and `property_counts` use the same words, as the hosted MCP does: `in_progress` for `IN_PROGRESS` and `unresolved`
  for `FAILED`. 3.0.0rc1 to rc6 called a disproof `failed` in the CI summary and an unresolved property `failed` in
  `show`, and used `negated`, `counterexample found` and `in progress` too. 3.0.0rc7 to rc10 called `FAILED` `not verified` and `IN_PROGRESS`
  `unresolved`, and their JSON used `unresolved` for `IN_PROGRESS` and `not_verified` for `FAILED`, so a script reading
  `unresolved` from rc10 or earlier now gets the `FAILED` count. `properties --status NOT_VERIFIED` is still accepted,
  and lists the `FAILED` properties. (#245)
- A run the time budget cuts exits `3` whatever `--fail-on` is, `aristotle review`'s default `never` included, as
  it hasn't finished: exit `0` looked like a finished run that found nothing. A run that ran out of its own budget
  still passes under `--fail-on never`. (#249)
- `aristotle ci setup` run from a subfolder writes the manifest `aristotle ci` reads there: the nearest
  `aristotle.toml` up to the top level, which it refuses to replace without `--force` and says so when `--force`
  does, else one in that folder (`--directory .` sets up the folder itself). It wrote a new one in the subfolder,
  which hid the subproject's manifest from `aristotle ci` run there. When the manifest is below the top level, it
  prints the `aristotle ci --directory` command a CI job needs to read it. (#246)
- `aristotle` with no command prints its help, as `aristotle --help` does, instead of 2.x's banner pointing at the
  web app, which kept its color codes even when piped. The help and the package's summary say what it does:
  `Aristotle SDK - formally verify your code from Python or the command line`, where 2.1.0's said it was for
  automated theorem proving with Lean. (#219)
- `submit` and `continue` let Aristotle ask questions without `--wait` too, as the web app does, and it waits up to
  15 minutes for each answer. Without `--wait` they print how to answer, `aristotle questions <project-id>`, and
  `--no-questions` turns questions off for a batch run, for a new task. 2.1.0 and 3.0.0rc1 to rc4 turned them off
  unless the command waited. `aristotle ci` and `aristotle review` still turn them off, and `aristotle ci setup`
  still turns them on. (#187)
- The copy offers Verilog and C on every account, and Rust on accounts where it's enabled. (#135, #346)
- Links point at the current web app pages (`/keys`, `/docs`, `/projects/<id>`) instead of the retired
  `/dashboard` ones. (#122)
- Ctrl-C during a wait exits 130 without a traceback and says the task is still running. (#130, #136)
- `submit --wait` downloads nothing when the wait ends before the task finishes (exit 3), and `--json`'s
  `result_path` is `null`. Get the files with `aristotle download <id>` once the task has finished. A finished task,
  failed or cancelled included, still saves its archive. 2.1.0 saved the files of the run in progress as its
  result. (#146)
- A wait at a terminal (`submit --wait`, `continue --wait`, `show`) no longer clears the screen when it ends. The
  run's final status and its last events, the summary among them, stay above the outcome. `download` and
  `submit --wait` say where they saved the archive. 2.1.0 cleared the screen and saved the archive silently. (#152)
- In the `--json` results of `show`, `submit` and `continue`, `property_counts` gains `unchecked`: the proofs and
  refutations Aristotle never checked because the task that set them ended first, every one if it ran out of budget and
  every proof if it failed or was cancelled, until a later task updates them. A verdict an earlier task checked stays
  checked when a later one that leaves it alone is cancelled. Those no longer count as `proved` or `refuted`,
  and each has `unchecked` set in `properties`, as in the records `properties --json` and
  `property --json` print, whose `status` stays as reported. `show`, `properties`, `property` and `ci setup --from`
  label them unchecked too. `property_counts` is `null` until the task has ended, so a script that gates on it
  can't pass a task still queued, and `properties` leaves out a property Aristotle withdrew, as the counts already
  did. (#205, #231, #239)
- aristotlelib requires httpx below 1.0, whose pre-releases drop what the client uses, where 2.1.0 took any httpx
  from 0.24. It declares `certifi`, `socksio` and `typing_extensions`, which it imports, instead of relying on httpx
  and pydantic to bring them. 3.0.0rc6 also required tomli below 2.4 on Python 3.10 and 3.15 or later; 3.0.0 reads
  manifests with a copy of its own and requires no tomli. (#229, #236)

### Removed

- Ask mode: `continue --mode`, `aristotlelib.FollowUpMode` and `Project.ask(mode=...)`. (#134)
- `aristotle formalize`. (#126)
- `aristotle verilog`. It was only in the separately built Verilog wheel, never in the package on PyPI. Submit the
  design directory with `aristotle submit "<prompt>" --project-dir rtl` instead. (#120)
- `aristotlelib.api_request.BASE_URL`. (#131)
- `aristotlelib.local_file_utils.read_file_safely()`: `Project.create()` now streams the archive. (#201)
- `aristotlelib.progress_utils.create_progress_bar()`: a wait no longer draws a bar. (#282)

### Fixed since 2.1.0

- A `submit` or `continue` that Aristotle refuses for too many runs in progress says that no project was created, or
  that the prompt was not sent, so it is safe to send it again, and under `--json` or `--non-interactive` prints
  `{"outcome": "not_processed", "error"}`, as one refused for too many requests at once does. It still exits 6. It gave
  Aristotle's reason alone, and printed nothing on stdout. (#332)
- A `submit --wait` whose new project's task can't be listed, or isn't listed yet, says that the project was created and
  names the `aristotle show` command that waits for it, then exits 3. Under `--json` or `--non-interactive` it prints
  the project with `"task": null` first, as it also does when interrupted then, exiting 130. It printed the listing's
  error alone, or "Aristotle was unable to wait for completion.", and nothing on stdout. (#332)
- `AgentTask.wait_for_completion()` with its default arguments writes nothing to stdout and reads nothing from stdin,
  like the rest of the SDK: it logs the questions waiting for an answer, and how the task ended, to the `aristotle`
  logger. It drew the CLI's progress display on the program's stdout, clearing the screen before every frame at a
  terminal, which wiped what the program had printed, and stopped there to ask Aristotle's questions on stdin. 2.1.0
  cleared the screen even when stdout was not a terminal. Pass `interactive=True` for the display and the questions, as
  the CLI does. (#330)
- `continue --files` and `Project.ask()` preserve paths relative to an explicit project root (cwd by default), so
  files in different folders with the same name stay distinct. Duplicate upload paths, traversal and files outside
  the root are refused before anything is sent. This supersedes the basename-only upload warning and
  duplicate-basename check introduced in #318.
- When something other than Aristotle answers a request, such as a captive portal's sign-in page, a proxy's page or
  a site at a wrong `ARISTOTLE_WEB_URL`, the command ends with one line that says what came back and where from, and
  exits 3:
  `GET /project after 5 attempts over 23 s: the answer from https://aristotle.harmonic.fun was 200 OK with text/html,
  not Aristotle's: a captive portal or a proxy may have answered instead.` Such an answer, a 2xx one that is text or
  has no content type and isn't JSON, is retried as a server error is, and a wait rides it out as an outage, so a
  maintenance page that answers one refresh no longer ends a wait. A create, a prompt or an answer it answers may have
  reached Aristotle first, so `submit`, `continue` and `questions --answer` exit 7 for it, `submit --json` and
  `continue --json` print `"outcome": "unknown"`, and `aristotle login` treats it as a lost answer. Every command
  crashed with a traceback, and `--json` printed nothing; a download it answered saved the page as the result archive
  or the file asked for, and `aristotle login` stored a key that nothing had checked. In Python,
  `AristotleRequestClient` raises `AristotleAPIError` for such an answer, or `AristotleOutcomeUnknownError` for a
  request that isn't safe to repeat, such as `Project.create()`, `Project.ask()` or `Question.answer()`, where reading
  it raised `json.JSONDecodeError`; its event stream counts an answer that isn't typed `text/event-stream` as a
  dropped connection. (#333)
- `submit --project-dir`, `Project.create_from_directory()`, `aristotle ci`, `aristotle review` and
  `aristotle ci setup` also leave out `_netrc`, the Windows name for `.netrc`; `.pgpass`, `.my.cnf` and `.htpasswd`,
  which hold passwords; `.vault-token`, `.s3cfg`, `.boto` and `kubeconfig`, which hold a Vault token, cloud storage
  keys and a cluster's credentials; Terraform's state files, `*.tfstate` and `*.tfstate.*`, such as
  `terraform.tfstate` and its backups, which hold the secrets of the resources Terraform manages in plain text; and
  Terraform's variables files, `*.tfvars` and `*.tfvars.json`, which often hold credentials. They match in any case
  and at any depth, and the log names each file left out, as `--dry-run` shows. They were uploaded. To upload a
  variables file that holds no secret, rename it. A Verilog block that reaches one of them stops the command with
  exit 2, as one that reaches any other credential file does. (#317)
- An API key with whitespace around it, such as the newline a CI secret can end with, or the carriage return that
  `$(cat key.txt)` keeps from a file with Windows line endings, is sent without it, whether it comes from `--api-key`,
  `aristotlelib.set_api_key()`, `ARISTOTLE_API_KEY` or the credentials file. Every request with it failed before it
  was sent, with an error that printed the whole key on stderr, where a CI log keeps it, and `submit --json`,
  `continue --json` and `questions --answer --json` printed it on stdout too, in their `"outcome"` result; `submit`
  exited 7, as if Aristotle might have acted on it. A key that holds only whitespace, or still holds a space, a
  control character or a non-ASCII character, exits 5 before any request, as a missing or rejected key does, saying
  where the key came from and what is wrong with it, never the key; in Python it raises `ValueError`. A non-ASCII
  character, such as a zero-width space, a no-break space inside the key or an `é`, crashed every command, `aristotle
  login` included, with a traceback, and a space inside the key went to Aristotle, which refused it. `aristotle login`
  refuses such a key the same way, and stores nothing; a blank `login --api-key` exits 5 too, where it was a usage
  error that exited 2. A request that h11, the HTTP library, refuses to send, as it refused that key, is reported by
  the error's class alone, as in `LocalProtocolError on POST /project`, since its text quotes the header it refused,
  and counts as never sent: `submit` exits 3, and with `--json` prints `"outcome": "not_processed"`, where it exited 7
  with `"unknown"`. (#312)
- Each name of a file goes up with the file's content: a hard link in the folder, as `ln a.sv sub/b.sv` makes one,
  and a symbolic link inside it to a file that has a hard link. `submit --project-dir`, `submit -f` and `-F`, `ci`,
  `review` and `ci setup` sent the second name as a link to the first, which Aristotle doesn't keep: the run saw the
  file, but the project's input and every result archive lacked it, so a file the run didn't edit never came back. A
  Verilog block's file reached by two names went up the same way. A file replaced by a link as it's archived stops
  `submit`, `ci` and `review` with `Could not archive <path>: it changed while it was being uploaded. Try again.` and
  exit 3, and so does a block's file, the folder's own `aristotle.f` included, whose path leads elsewhere than when the
  block was read. Until now, a link put in a file's place could send the file it led to, or its start, instead. (#313)
- `show --json` gives `result_path`, as `submit --json` does: where `--destination`, with `--wait` or
  `--non-interactive`, saved the result archive, else `null`. `show` now saves the archive before it prints the result,
  as `submit` does, so an interrupt during the download leaves the result cut short, with the counts null, rather than
  whole. The CLI reference's agent loop passes `--destination` to `show`, as the command a stopped wait prints does, and
  drops its own `aristotle download`, since `show` now saves the archive and names it. A `submit --wait`, or a `show`
  with `--destination`, interrupted after its wait has seen the task finish and before the archive is saved, or during
  the download, says it wasn't saved and names the `aristotle download` command that saves it where the wait would have;
  it exited 130 without a word, apart from `--json`'s result. The `aristotle download` commands a wait prints when it
  can't save the archive, or ends before the task does, keep `--destination` too. A wait interrupted while the run went
  on already named the `show` command that carries it on, with `--destination`. (#306)
- `submit --project-dir`, with or without `--dry-run`, exits 2 before anything is sent for a folder that can be listed
  but not entered, such as one at mode `0600`, with `Nothing to upload: <dir> can't be read (Permission denied).`, as
  for a folder that can't be listed. `Project.create_from_directory()` raises `LeanProjectError` for it. On Python
  3.10 to 3.13 it printed a traceback and exited 3; on 3.14 it exited 3 with `Could not archive <file>: Permission
  denied.`, or 2 calling an empty folder empty, and a folder with no file directly in it went up with just the files
  an `-f` or `-F` named outside it. A file in the folder that can't be read, or one in a folder inside it that can't
  be entered, still exits 3 with that `Could not archive` line. (#304)
- A file in a folder that can't be entered, named by an `aristotle.f`, `-f` or `-F`, stops `submit`, `ci` and `review`
  with `can't read <path>: Permission denied`, exit 2, where they said `no such file`. So does an `aristotle.f` in
  `--project-dir` that links into such a folder: Python 3.10 to 3.13 printed a traceback, and 3.14 exited 3 or uploaded
  the folder without the block it describes. An untracked root `aristotle.f` that links there gets `ci`'s and
  `review`'s warning that git doesn't track it, where Python 3.10 to 3.13 exited 3. A `lean-toolchain`,
  `lake-manifest.json` or `.lake` linked into such a folder outside `--project-dir` is left out as a broken link is,
  where Python 3.10 to 3.13 printed a traceback. `ci` and `review` report a property in a file there at its recorded
  line; on Python 3.10 to 3.13 the JUnit report, and the console report when it located the property, failed with
  `Could not write …`. (#304)
- A task cancelled before its run left the queue reads `CANCELED (submitted 30s ago)`, not
  `CANCELED (started 30s ago)`, in a wait and in `show`: a run cancelled or failed, or in a status this version
  doesn't know, never started when it has no start time, `AgentTask.started_at`, which `--json` task records carry,
  and no progress recorded, which an older server sends instead. A run that Aristotle failed before it started can
  still read `FAILED (started …)`, since Aristotle may record such a failure as a start. `AgentTask.percent_complete`
  is deprecated: current servers send `null`, and it defaults to `None`, so a server may leave it out. (#308, #321)
- A usage error that the arguments alone show prints as every other does, argparse's `usage:` line and then
  `aristotle <command>: error: <message>`, and exits 2: `cancel` given no project or task, or two; `questions` given
  two answers to one question; `review --scope paths` without `--path`, or `--scope all` with one; `ci --dry-run`
  with `--project` or `ARISTOTLE_PROJECT`; `ci`'s options given to `ci setup`; and `review`'s options given to `ci`.
  They printed `ERROR - ` and the message alone. `cancel`'s errors name the project or task, where they named the
  argument's internal name, `project_or_task_id`. (#298)
- Messages that pointed the wrong way say what happened. `submit <file>` suggests `--prompt-file`, where it suggested
  `--project-dir`, as it still does for a folder. `properties <project> <name>` follows the unrecognized arguments
  with the command that shows one property, `aristotle property <project-id> <name>`. `continue <project> --files
  a.lean "<prompt>"` says that `--files`, which takes every argument after it, took the prompt as a file, and to give
  the prompt first, where it said a prompt was required. Piped, `show` of a running task says to wait for it with
  `show --wait`, as every other command that carries on a wait does, where it named `show --non-interactive`.
  `continue` to a running task with a question pending says that Aristotle is waiting for answers, with the `aristotle
  questions` command, where it said to answer if Aristotle asks anything. `review` names "a --blackbox line in
  aristotle.f", as it has no `--blackbox` option. (#298)
- `submit --destination` without `--wait` or `--non-interactive` warns that it is ignored, since only a wait saves the
  result archive, and that 4.0 will refuse it. It was dropped without a word, while `show` refuses it. The CLI
  reference has a table of what each wait saves: `submit` to `--destination`, else into the current folder, `show`
  only to a `--destination`, and `continue` never. (#299)
- `list --status` keeps each status when it is repeated, as in `list --status RUNNING --status IDLE`, as
  `properties --status` does. It kept only the last. (#299)
- A refused request, an HTTP 403, says where the refusal came from, in an `Error:` line and a `Hint:` line. A 403
  without Aristotle's JSON error format, on a request that goes through a proxy, as `HTTPS_PROXY`, `ALL_PROXY` and
  `NO_PROXY` decide, and a proxy's refusal of the tunnel, read
  `Error: request blocked by a proxy (403 Forbidden, via <proxy>)`, which names the proxy by its host alone, with a
  hint to ask the network admin to allow the host, such as `aristotle.harmonic.fun`. A 403 without that format, and
  without a proxy, reads `Error: request refused (403 Forbidden)`, with a hint that a proxy or firewall may be in the
  way and to run with `--verbose`. A 403 in Aristotle's format, where a command doesn't report a project as not found
  or not accessible, reads `Error: this account doesn't have access (403)`, with a hint that the account may be
  awaiting approval or no longer in the key's organization. A proxy's HTML 403 made `aristotle login` say the key's
  account didn't have access yet, other commands print `API request failed with status 403:` and the response's body,
  and a lookup by id say the project was not found or not accessible. A proxy can imitate Aristotle's format, so the
  message is a diagnosis, not proof. Exit codes, retries and the `--json` shapes are unchanged. (#292)
- `continue` and `questions --answer` say that nothing was sent, and with `--json` print
  `{"outcome": "not_processed", "error"}`, when a lookup they make first, such as the project's, fails with no
  response, a server error or too many requests at once. They still exit 3, or 6 for too many requests. They printed
  the lookup's error alone, with nothing on stdout under `--json`. `Project.ask()` with files raises
  `AristotleNotProcessedError` when its check that the project is idle fails that way. (#285)
- `show` refuses a `--pagination-key` that no page of events would use, with exit 2: with `--limit 0`, for a finished
  task without `--limit`, for a project without a task, and, without `--json`, for a running task it follows at a
  terminal or with `--wait`. It ignored the key, however malformed. (#285)
- `continue --files` says which files went up with the prompt, on stderr: their count and total size, as in
  `Uploaded 5 files (48.2 KB) with the prompt:`, then the names of the first 10. `--json`'s result is unchanged. It
  said only `Prompt submitted to project …`, which left no telling whether the files had gone. On a project running a
  task, its refusal says what to do instead: send the prompt without `--files`, which reaches that task's agent right
  away, or wait for the task to finish with `aristotle show <id> --wait`, then send it with `--files`.
  `Project.ask(files=...)` raises a `ValueError` saying the same. Both said only that the project had to be idle. (#290)
- Where name lookups fail or stall, as in Codex's sandbox with the network off, a command exits 3 with its error
  once its retries are spent, within about two minutes, and Ctrl-C or SIGTERM ends it at once.
  It used to go silent after its last retry: in Codex's sandbox for five minutes, or on Python 3.10 and 3.11 until
  something killed it, since the sandbox refuses the system call another thread uses to wake the CLI's event loop;
  and with a stalled lookup until the lookup gave up, since the CLI waited for it. In Codex's sandbox, each failed
  lookup also took 10 seconds to notice, and now takes under one. (#289)
- A wait at a terminal (`submit --wait`, `continue --wait`, `show`) counts the project's properties as `show` does
  after it, such as `Properties: 1 proved · 0 refuted · 5 in progress`, recounted every 30 seconds while the run goes
  on. It drew a bar of the task's `percent_complete`, which a Verilog run reports as 1% until it ends, and the bar's
  empty part, drawn in `░`, looked filled in some terminals. A running task's status reads `IN_PROGRESS`, as the
  others read `QUEUED` or `COMPLETE`, and an event whose line repeats the one above it, such as a Verilog run's next
  command, isn't shown again. `aristotle ci` and `aristotle review` log the run's status alone, such as
  `[05:00] IN_PROGRESS`, at each change of status and every five minutes, where they added the percentage and a line
  at each change of it. (#282)
- `submit --project-dir` without a Verilog filelist says so when an ignore rule leaves out every `.v` and `.sv` file,
  so that no Verilog source goes up, as when the Verilog is generated from templates and ignored. It names up to five
  of them, each with the `.gitignore` rule that matched it where git can say, and says to describe the design with a
  filelist, which uploads every file it names even when an ignore rule matches it. When no `.lean`, `.c` or `.rs` file
  goes up either, `submit` and `submit --dry-run` exit 2 before anything is sent, and
  `Project.create_from_directory()` raises `LeanProjectError`; otherwise they warn. It uploaded the rest, such as the
  templates, and started a run with no design to verify. (#272)
- The package declares its licence, `LicenseRef-Proprietary AND MIT`, so `pip show aristotlelib` prints it with pip
  25 and later, and `LICENSE` links the API terms page that exists, `https://aristotle.harmonic.fun/terms`. The
  package declared no licence, so `pip show` printed an empty `License:`, and `LICENSE` linked a page that
  returned 404. (#262)
- A `submit`, `continue` or answer whose every attempt failed before Aristotle acted on it, because no connection
  opened or Aristotle refused each one as too many requests at once, says that nothing was done and that it is safe
  to send it again, and raises `AristotleNotProcessedError`, a subclass of `AristotleAPIError`. Under `--json`,
  `submit` and `continue` print `{"outcome": "not_processed", "error"}`. It still exits 3, or 6 for the refusals. It
  used to read like any other network error, with nothing on stdout under `--json`, so a script couldn't tell it from
  a request that may have gone through. (#254)
- `submit --project-dir` with nothing to upload exits 2 before anything is sent, the request for Aristotle's Lean
  toolchain included, and says why: the folder is empty or can't be read, a folder in it can't be read, or each of its
  files stays for an ignore rule, one of Aristotle's own exclusions, which it names, or a warning above.
  `Project.create_from_directory()` raises `LeanProjectError` for such a folder. It created a project on an empty
  archive, and so started a run on nothing. (#241)
- `aristotle ci`, `aristotle review` and `aristotle ci setup` leave out every file in a hidden folder named like a
  credential, such as `.env/production.yaml` or `sub/.netrc/inner`, as `submit --project-dir` already did, and name
  each in the log; they uploaded those files. All four also leave out `.envrc` and everything in `.docker/`, which
  holds registry logins. (#242)
- `aristotle ci`, `aristotle review`, `aristotle ci setup` and `submit` also leave out everything in `.ssh/`,
  `.kube/`, `.gnupg/`, `.aws/`, `.azure/`, `.terraform.d/`, `.config/gcloud/`, `.config/gh/` and
  `.config/glab-cli/`, with their Windows homes under `AppData\Roaming`, and any `credentials.toml` or
  `application_default_credentials.json`, counting from the folder they check too, so a repository at `~/.config`
  leaves out `gh/hosts.yml`. They match a credential's name in any case or spelling a disk takes for it, as a
  case-insensitive one takes `id_rſa` for `id_rsa`. They uploaded `.ssh/config`, `.kube/config` and
  `.config/gh/hosts.yml`, which hold hosts and tokens. `aristotle ci` and `aristotle review` also leave out, and
  name, the files of a git directory committed as plain files, such as a vendored bare repository, whose `config` may
  hold a remote's token. (#221)
- `submit` refuses a folder inside a `.git` folder, a git directory or a folder that holds credentials, as
  `--project-dir repo/.git` is, and leaves out a `.git` folder spelled in any case, such as `.Git/`, which a
  case-insensitive disk takes for git's own. It uploaded what they hold, such as `.git/config` with its remote's
  token. (#221)
- A `--pagination-key` Aristotle refuses is a usage error on `list`, `tasks` and `show`:
  "--pagination-key was rejected; pass the key the previous page printed.", with no retries. Where Aristotle still
  answers such a key with a server error, the error after the retries says the key is the likely cause. After a wait,
  `show --non-interactive` still prints the task as it stood, and its warning names the key. (#243)
- On GitHub, an error that ends `aristotle ci` or `aristotle review` before it reports a run, such as an invalid
  option, a missing or rejected API key, a manifest it can't find, a checkout git can't read or a run it couldn't
  start, is also an error annotation, so the run page says why. Only the step log did. An invalid option of any
  command is annotated so on GitHub. (#247)
- A wait that ends with the task failed, cancelled or out of budget names the command that resumes it on the same
  project, `aristotle continue <id> Continue --wait`, where it said "Resume by telling Aristotle to continue" or
  "please try again". The command waits as the wait did: `--wait --json` after a `--json` wait, `--non-interactive`
  after a `--non-interactive` one, and after a wait with `--destination`, which `continue` doesn't take,
  `aristotle continue <id> Continue` and then `aristotle show <id>` with it. In the SDK, `AgentTask.wait_for_completion`
  takes those commands as `resume`, each a list of words. `show` of such a task now says the same after its status,
  where it gave the status alone. For a project someone else owns, or an older task picked with `--task`, it says only
  how the task ended. A failed or cancelled `ci setup` run says to run `ci setup` again or to write the manifest with
  `ci setup --from <id>`. (#244, #261)
- `submit --project-dir` leaves out the result archives an earlier run saved, such as the
  `<project-id>-aristotle.tar.gz` that `submit --wait` saves in the current folder, and names each. A second submit
  from that folder uploaded the first one's result. (#232)
- `submit --project-dir .` and `--project-dir ..` name the project after the folder, as the help says, where they
  named it `project` and `..`. So does `Project.create_from_directory` given such a path. (#248)
- `show` of a running task no longer follows it when stdout is piped, as in `show <id> | tail`: it prints the task as
  it stands and says how to wait for it. Its help says that it follows a running task at a terminal. (#209)
- An interrupted wait, with Ctrl-C or a SIGTERM such as `timeout` sends, says how to follow or cancel the run, and a
  `--json` wait (`submit --wait`, `continue --wait`, `show --non-interactive`) still prints the task as it last stood,
  with `property_counts` null, where it printed nothing, even when only `submit`'s download was cut short. Every
  command now ends on SIGTERM as on Ctrl-C, with exit 130. (#209)
- The live display of `submit`, `continue`, `show` (but `show --task`) and `ci setup` is headed by the project's name
  rather than the first words of the prompt, `continue` names the task its prompt went to as soon as it is sent, and
  `q` on the question screen reads "quit, send nothing" rather than "cancel all", which sounded like cancelling the
  run. (#209)
- A 429 counts as the limit on runs in progress or queued only when it refuses a request that starts a run and
  carries that limit's message. Any other 429, such as an empty or HTML answer from a proxy, is retried like the
  per-second limit. It used to say "You have too many projects in progress" and exit 6 at once, even for a read. (#218)
- `cancel` of a task that has already finished says so, and exits 4, instead of printing `API request failed with
  status 404: Agent task cannot be canceled`. `aristotle ci` and `aristotle review`, cancelling a run that has just
  finished, say it had finished rather than how to cancel it by hand. (#218)
- An event with no text, such as a command run without its line, prints as what Aristotle was doing, in the web
  app's words, such as `Running command`, rather than as a bare `RUNNING_COMMAND:`. (#218, #282)
- Invalid arguments exit 2 before the API key is looked for: `submit ""`, `list --limit 0`, `cancel` without an id,
  `review --scope paths` without a `--path`, and the other checks of a command's arguments. (#219)
- Where stdout and stderr go to one pipe or file, as with `2>&1`, a note follows what was printed before it: `list`,
  `tasks` and `properties` printed the next page's key above their table. `show --json` logs its key after the JSON,
  where it logged it first even at a terminal. (#219)
- `--api-key` given before the command (`aristotle --api-key <key> list`) is used. 2.1.0 ignored it. (#139)
- An option spelled with underscores (`--project_dir`, `--base_ref=main`) is taken as the option. An unknown
  option is reported by the command that rejected it, with that command's usage and the closest option it has
  (`aristotle submit: error: unknown option --projectdir (did you mean --project-dir?)`). 2.1.0 printed the
  top-level usage and `unrecognized arguments:` followed by whatever it could not place, often the prompt. (#161)
- An unknown project or task id is reported in one line instead of a traceback. Ids are sent as a single path
  segment, so an id containing `/` or `..` can no longer reach a different project. (#133)
- `show --task` takes the project id in any spelling (upper case, or without dashes), as every other command does,
  instead of reporting that the task belongs to another project. So does the new `questions --task`. (#141)
- `--project-dir` uploads a symlinked file whose target is also in the upload, as a copy of that file, instead of
  dropping it. Any other symlinked file is left out and named in a warning, and FIFOs and sockets are skipped.
  Symlinked directories are still not followed. (#139)
- An empty prompt is rejected before anything is sent. (#118)
- `submit` sends a prompt too long to be a file path, such as a 300-character sentence, instead of crashing with
  `OSError: [Errno 36] File name too long` on Python 3.10 to 3.13. 2.1.0 crashed too. (#165)
- `continue --files`, and `ci setup` with `--manifest` or `--directory`, refuse a path too long for the file system
  or inside a folder they can't enter with exit 2, instead of crashing with an `OSError` on Python 3.10 to 3.13.
  `continue --files` also refuses a file it can't read before sending anything, and `ci setup` a manifest it couldn't
  write, or a `--directory` it can't read, before the setup run. (#194)
- `submit --project-dir` and `Project.create_from_directory()` leave out every file named like a credential that
  `aristotle ci` leaves out (`.netrc`, `.npmrc`, `.aws/credentials`, `id_ecdsa`, `*.pem`, `*.ppk`, `credentials.json`
  and the rest), in any case, and warn which files they left out. 2.1.0 left out only `.env`, `.env.*` and
  `*.setup.json`, silently. (#167)
- In a git repository, `submit --project-dir` and `Project.create_from_directory()` leave out whatever an ignore rule
  matches, nested `.gitignore` files and the repository's own (when you submit a subfolder) among the rules, except
  `.lake` and Lean's build files, which always go up. A tracked file is left out only for a rule in one of the
  repository's `.gitignore` files. 2.1.0 read only the folder's top-level `.gitignore`, which is still what applies
  outside a repository, and, with a warning, when git isn't installed or refuses the repository. (#167, #223)
- `--project-dir ""`, which is what an unset variable expands to, exits 2 instead of uploading the current directory.
  So does a `--project-dir` whose name is too long or whose parent can't be read, instead of a traceback. (#167)
- Output the console cannot encode, such as Lean's `∀` on a Windows code page, is escaped instead of crashing
  the command. (#129)
- A wait whose event stream ends before the task finishes says so instead of exiting silently. (#136)
- A wait (`--wait`, `--non-interactive`, `show`) keeps following a run when the event stream disconnects, and
  resumes where it left off, instead of giving up after about three quiet minutes. A connection that dies without
  closing is reopened after 90 s of silence instead of hanging the wait. While the run is live, it also rides out a
  network outage, retrying the stream every 30 s at most for 10 minutes; it then says so, and that the task goes on.
  It says once that the connection failed and once that it is back, its progress display shows the outage while it
  lasts, and its refreshes meanwhile log their retries only at debug level. `aristotle ci setup`'s wait for its setup
  run does the same. (#144, #253, #261)
- Without a terminal, the CLI no longer reads stdin to answer a question. (#130)
- When stdout is not a terminal, the progress display writes no clear-screen codes, so `show` or `submit --wait`
  piped to a file or run in a CI job leaves plain text. (#152)
- `download --destination` and `submit --wait --destination` make missing directories, and save into a directory
  under the archive's own name, instead of failing with a traceback (exit 3). Both check the destination first
  (`submit` before it creates the project) and exit 2 if the archive couldn't be written there. If saving still
  fails after the run, `submit --wait` prints the `--json` result with `result_path` `null`, says how to download
  the files, and exits 3. In the archive's default name, `/`, `\`, `:` and other characters a file name can't hold
  become `_`. The SDK's `Project.get_files` makes missing directories, saves into a directory and uses the same
  safe name, so a file name the server sends can no longer place the archive outside the destination. A
  `--destination` starting with `~` is expanded to the home directory. (#166)
- A save that fails partway, such as on a full disk, leaves no partial archive and keeps the file it would have
  replaced: `submit --wait`, `download` and `Project.get_files` write beside the destination, then move the archive
  into place once it's whole, with the old file's permissions and, on Linux, its access control list, which the file
  beside it has from the start. Where that isn't allowed, as in a folder that takes no new file, and for a
  destination that isn't a regular file, such as `/dev/stdout` or a symlink, the archive is still written in place.
  A read-only file still can't be replaced, as in 2.1.0. A default archive name that Windows takes for a device,
  such as `CON.tar.gz` or one from a project named `nul.v`, gets a leading `_`. (#195, #212)
- A request refused by the per-second rate limit, or failed by a server error, a timeout or a dropped connection, is
  retried with jittered backoff, up to five attempts with none starting more than 90 s after the first, so commands
  run many at a time no longer fail on them. A failure names its type and the request, such as
  `ReadTimeout on GET /project/<id>`, instead of a bare `Request failed:`. `submit`, `continue` and answers
  are sent again only when Aristotle cannot have acted on them; otherwise they raise `AristotleOutcomeUnknownError`,
  and `submit` and `continue` say how to check before sending again. (#163)
- A process starts at most five requests a second, so a script that polls through the SDK, and the bursts of
  requests one command makes, stay under Aristotle's per-second limit. A retry, and an event stream's reconnect, wait
  at least as long as the response's `Retry-After` asks; a request gives up when that would pass its 90-second
  deadline, and a stream when it asks for more than 30 seconds. Without one, the backoff is as before. (#189)
- A `cancel` is safe to repeat: one whose reply is lost is sent again, and a task already cancelled counts as
  cancelled, so `aristotle cancel` exits 0 for it and `aristotle ci` no longer reports a run it cancelled as not
  cancelled. An interrupt while `aristotle ci` or `aristotle review` names the run or finds its task cancels the run,
  unless `--on-timeout leave` is set, and prints its URL. 2.1.0's `cancel` printed a traceback when its reply was
  lost, or when the task had already ended. (#181)
- Text from a repository or from Aristotle can no longer act on the terminal or the CI log. Everything the CLI prints
  outside `--json` has its control characters escaped (an escape sequence, a carriage return and the rest; tabs and
  newlines stay), Azure Pipelines and TeamCity commands in it are broken up, and on GitHub Actions no line it prints
  is a workflow command: a backslash goes before a line's leading `::` and after every `##[` that could name a
  command, which an SVA delay range such as `##[1:3]` can't. A file name with a newline in it could put `::error` or
  `::stop-commands::` lines in `aristotle ci`'s log, and the second silenced the CLI's own annotations. 2.1.0 printed
  Aristotle's text as it came, control characters included. A carriage return in a description no longer breaks its
  row of the GitHub job summary. (#169)
- Lean's build files (`lakefile.lean`, `lakefile.toml`, `lake-manifest.json` and `lean-toolchain`, in any folder) go
  up even when an ignore rule matches them: `submit --project-dir` always uploads them, as it does `.lake`, and
  `aristotle ci` and `aristotle review` upload them whenever git tracks them. Any other tracked file a rule in one of
  the repository's `.gitignore` files matches is still held back, and `submit`, `ci` and `review` name every one, with
  the rule that matched it and how to upload it. 2.1.0's `submit` left out a `lake-manifest.json` that the folder's
  top-level `.gitignore` matched, tracked or not. (#170, #223)
- A request whose response was lost after Aristotle may have acted on it exits `7`, not `3`, so a script that retries
  on `3` doesn't send a `submit`, `continue` or answer twice. Under `--json`, `submit` and `continue` print
  `{"outcome": "unknown", "error"}`. When it was the run's create, `aristotle ci` and `aristotle review` look for the
  run among the newest projects, by the name its archive was uploaded under, and follow it as usual; they exit `7`
  only if they find no such run that started. That archive name is the checkout's name with a random suffix, and the
  project keeps it if renaming fails. 2.1.0's `submit` and `continue` printed a traceback and exited `1`. (#171)
- A wait that the event stream breaks off by answering 503 or 429 reports the task as it last stood, as it does when
  the connection drops: `submit --wait`, `continue --wait` and `show --non-interactive` exit 3, and print the task
  with `--json`. 2.1.0 crashed with an `httpx.HTTPStatusError` traceback. A failed request after the wait doesn't
  lose that JSON: `submit --wait` prints it with `"result_path": null` when the project can't be refreshed, and
  `show --non-interactive` prints it without the events it couldn't fetch. (#178)
- `submit --project-dir` and `Project.create_from_directory()` leave out a file whose name isn't UTF-8, and name it,
  as `ci` and `review` do, instead of uploading it to a run that Aristotle then fails within a minute. 2.1.0 uploaded
  it too. A `--project-dir` folder whose own name isn't UTF-8 goes up under that name with each such byte as `\xNN`,
  instead of stopping with a traceback. (#188)
- `continue --files` with a file whose name isn't UTF-8 exits 2 before anything is sent, naming the file with each
  such byte as `\xNN`, instead of stopping with a traceback. `Project.ask()` sends such a name escaped so, and a UTF-8
  name the locale can't decode as the text it spells, instead of raising. `submit --name` and `rename` send a name
  that isn't UTF-8 with each such byte as `\xNN`, as a folder's name goes up, instead of stopping with a traceback,
  which for `submit` came after the project was created. `ci setup --name` sends it so too, where it left the project
  unnamed. (#196)
- An upload that takes longer than 30 seconds to send goes up: `submit --project-dir`, `aristotle ci` and
  `aristotle ci setup` stream their archive in chunks, each within the write timeout, instead of sending it in one
  write that a slow link timed out. A write that still times out exits 7, since the whole request may have
  reached Aristotle. `submit` checks the 100 MB limit on the compressed archive before it says what it uploads, and
  over it says how large the archive is and what to do (`The upload is 101.1 MB compressed, over the 100 MB limit.
  Exclude large files with .gitignore or submit a smaller folder.`), instead of naming a temporary file and its size in
  bytes. (#201)
- An upload on a slow link no longer times out as an unknown outcome while its last bytes are still on their way. The
  reply to an upload by `submit`, `continue`, `aristotle ci`, `aristotle review` or `ci setup` may take 30 seconds plus
  the upload's size at 16 KB/s, up to 10 minutes; it had 30 seconds from the last write. Megabytes of an upload can wait
  in this machine's and a proxy's buffers past that: through a proxy that forwarded 60 KB/s, a 3 MB `submit` exited 7
  after 34 seconds, and the project was made anyway once the rest arrived. A lost reply's message now says the project,
  or for `continue` the task, may take a few minutes to show up, and with `submit --name` also prints the command that
  finds it by its name, `aristotle show -- <name>`, and how to tell it from an older project of that name. A `submit`
  interrupted by Ctrl-C or SIGTERM once its create was sent says the same, and how to cancel the project, and one
  interrupted before, that nothing was sent. It used to exit 130 without a word, leaving a run going that the user
  didn't know of. (#238)
- On Python 3.13 and later, a certificate authority you name in `ARISTOTLE_CA_BUNDLE` (or `REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE`, `SSL_CERT_FILE` or `SSL_CERT_DIR`) is trusted for the server's certificate without the keyUsage
  extension, which many company authorities lack, as curl trusts it and Python 3.12 did. An `https://` proxy's own
  certificate is still checked with Python's own rules. A server certificate no authority you named vouches for says
  so, naming the file or folder, where the error told you to set `ARISTOTLE_CA_BUNDLE`. A certificate refused for its
  dates or its name gets no advice about authorities, and one an `https://` proxy presents for itself is named as the
  proxy's. (#237)
- A SOCKS proxy in `ALL_PROXY` or `HTTPS_PROXY` works: aristotlelib now depends on `httpx[socks]` 0.28 or later, so a
  `socks5://` or `socks5h://` proxy carries the CLI's requests. Every command that contacts Aristotle stopped with
  `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed`, even when `NO_PROXY` covered
  `aristotle.harmonic.fun`; 2.1.0 did too. Proxy settings the client can't use, such as a `socks4://` proxy, a
  malformed URL or an IPv6 range in `NO_PROXY`, are refused with a message that doesn't quote them, instead of a
  traceback. (#173)
- A request whose tunnel a proxy refuses with a 5xx status, such as `502 Bad Gateway`, is retried, a `submit`
  included, since none of it was sent; a `407` still fails at once. (#175)
- Three things a script runs into no longer end in a traceback and exit 3. A command that needs a key, run with none
  and with stdin closed, says how to get one and exits 5, and `aristotle login` with stdin closed, against a server
  that can't approve a login in the browser, says no key was entered, exit 5. On Linux and macOS, output to a pipe
  that closes early, as with `| head`, stops quietly with 141,
  `aristotle ci`'s included. A key that `aristotle login` checked but couldn't store, because the home directory
  can't be written, is one line naming the file it was meant for, exit 3. (#179)

### Fixed during the release candidates

For beta users: these entries fix problems only a 3.0.0 release candidate had, which a user of 2.1.0 never met.

- `aristotle ci` and `aristotle review` name Verilog or C as a cause of a run that verified nothing only when Aristotle
  says the account doesn't have it. Both are on for every account, so they no longer name one as a possibility when
  Aristotle doesn't answer. (#346)
- `aristotle ci`, `aristotle review` and `submit --project-dir` run git from the top of the checkout, the nearest folder
  at or above the one they read that holds a `.git`, with `safe.bareRepository=explicit` and with `core.fsmonitor` off.
  They ran git in that folder, so a committed folder that git reads as a bare repository, one holding `HEAD`,
  `objects/` and `refs/`, was the repository git used, with its `config`'s settings: `--directory` on it failed with
  `this operation must be run in a work tree` or found no tracked files, and `submit` applied only that folder's
  `.gitignore`. (#340)
- Following the skill or the setup guide, a coding agent with no key asks the user which way to log in before it
  starts a login, so a headless session, such as `claude -p` or `codex exec`, ends with the question. It started a
  login in the background, showed the code in a message that `claude -p` doesn't print, and waited 10 minutes for the
  code to expire. Both also have the agent report an error that says `Error: request refused` or
  `Error: request blocked by a proxy` as a refusal from a proxy or a firewall, whatever the exit code, where the
  guide read its exit 4 as a problem with the account and the skill's table as a wrong id. (#329)
- `submit --help` and the Verilog guide say that `--top` and `--blackbox` need a filelist that names a source file:
  the `--project-dir` folder's `aristotle.f`, or one `-f` or `-F` names. They said either worked on a folder with no
  filelist, where `submit` exits 2. That refusal said to list the files in an `aristotle.f` in `--project-dir`. It
  now says to pass such a filelist with `-f` or `-F`, or, when none is given, to name the top module and any black
  box in the prompt of a `--project-dir` upload, though a black box's model in the folder then still goes up. The
  guide shows a filelist kept outside the folder, which leaves the model out of the upload without writing into the
  repository. (#331)
- `aristotle ci` and `aristotle review` don't write the JUnit report through a symbolic link a checkout could hold.
  In the checkout, the report is a new file moved over whatever is at its path, a link included, so the file a link
  pointed to stays as it was and a later step finds a real report; a link at a folder on its way leaves the report
  unwritten, with a warning that names the link. The exit code stands either way. A link committed in a pull request
  made the step overwrite the file it pointed to outside the checkout, or create one where it dangled, wherever a job
  runs a pull request's checkout with the key, as a Jenkins multibranch pipeline or a GitHub `pull_request_target`
  workflow can; a Jenkins docker agent that mounts `JENKINS_HOME` let it reach Jenkins's own configuration. The
  checkout covers the folder the run checks, the top level of its repository and of the current folder's, and the CI
  system's workspace, however the report's path reaches them, through `..` or links. A link outside them, such as
  macOS's `/tmp` or Linux's `/dev/stdout`, still takes the report. (#327)
- A property left unresolved because its run finished without a verdict on it, or was cancelled, says so:
  `aristotle properties` and `aristotle property` show `UNRESOLVED (incomplete)` or `UNRESOLVED (cancelled)`,
  `aristotle ci` and `review` report `unresolved (incomplete)` or `unresolved (cancelled)`, and `--json` gives its
  `failure_reason` as `4`, named `INCOMPLETE`, or `5`, named `CANCELED`, as `DeclarationFailureReason` does in Python.
  The CLI didn't know these two reasons Aristotle records, so it showed `UNRESOLVED (unknown)`, the reports said
  `reason not reported`, `--json` gave `0`, named `UNKNOWN`, and each such property logged a warning. (#325)
- `submit`, `ci` and `review` no longer say an `aristotle.f` goes up "as committed", or a generated one "in place of the
  committed one": a dry run says one that goes up unchanged goes up as it is, and a run says a generated one goes up in
  place of the folder's own. The old words were wrong for an `aristotle.f` git doesn't track or outside a repository,
  which `submit` reads, and for one with uncommitted changes, which `ci` and `review` upload as it is on disk.
  `submit --help` says that outside a git repository, or in a folder git ignores, `--include-ignored` uploads what the
  folder's top-level `.gitignore` matches, and that `--dry-run --json` doesn't mark the files it adds; `ci --help` says
  that `--on-timeout` defaults to `leave` with `--project`. (#334)
- The agent skill says how to stop a run an agent started by mistake: `aristotle cancel <project-id>`, which exits 4
  if the run had already finished, and 0 if it was already cancelled. (#334)
- Following the skill, a coding agent asked to prove a property names the top module in the prompt, leaves the
  repository as it was, and offers to add an `aristotle.toml` so CI checks the property. It wrote an `aristotle.f`
  into the repository to name the top module, then wrote an `aristotle.toml` with `aristotle ci setup --from` and
  staged both in git, unasked. The skill now has an agent create, modify or stage a file in the user's repository
  only when the user asked for that change, and write or change a manifest only where one already covers the code
  or the user asked for Aristotle in CI. The plugin's description no longer mentions `aristotle.toml`. (#315)
- Following the skill, a coding agent asked to send a follow-up that only asks about a project's results, such as a
  summary of its refuted properties, reads them with `show`, `properties`, `property` or `download` instead, says why,
  and sends it only if the user still wants it. It sent the follow-up, which on an idle project starts a new run that
  can spend hours checking the proofs again before it answers. The skill and the README now say to read results rather
  than ask for them with `continue`. (#326)
- The CI image has `ps` and `kill`, which Jenkins runs in the container to stop a step, so aborting a Jenkins build, by
  hand or when a newer build supersedes it, interrupts `aristotle ci` or `aristotle review`, which cancels its run as
  the CI guide says. Jenkins logged `java.io.IOException: failed to run ps` and removed the container with the step
  still in it, so the run went on spending budget and, on an account that runs one project at a time, held up the
  next pipeline. (#316)
- A job cancelled while `aristotle ci`, `review` or `ci setup` uploads cancels the run its create started. The run
  went on under the upload's name, and the log said nothing. Once the create is sent, an interrupt looks for the
  project by its upload name for up to 10 seconds and cancels the run once it starts, or with `--on-timeout leave`
  names it and leaves it; if it can't find the project, or the run hasn't started, it says how to find and cancel it.
  A second interrupt ends the search, or the wait for a cancel's answer, and the log still says what was found and how
  to cancel the run. A cancel, after an interrupt or when the time budget runs out, waits up to 5 seconds for
  Aristotle's answer, then checks the run once, and a run that reads `CANCELED` is reported cancelled. Aristotle takes
  about 95 seconds to answer a cancel, so every interrupted job said "Could not cancel the run" though the cancel
  worked, and a run cut by the time budget overran `--timeout` by that long. (#319)
- A wildcard with many `*` takes time in proportion to the name it is matched against, where each further `*` made
  the match about ten times slower: in the paths inside a filelist, which `submit`, `ci` and `review` read, and in
  `review`'s `--path` globs, which `ARISTOTLE_PATHS` and the Action's `paths` input also give. Against a 60-character
  name, `*a*a*a*a*a*a*ab` took 6 seconds, and a dozen `*` would take hours, so an `aristotle.f` edited in a pull
  request could hold `aristotle ci` until the job timed out. Every pattern matches the same names as before. (#302)
- The GitHub job summary keeps the code spans in Aristotle's summary and in a property's text as written when the
  text also holds a link or a URL, so SVA or C copied from them parses. A link or a URL anywhere outside code gave
  every code span a zero-width space after each `<` again. A link, reference or URL that could take in a backtick,
  and with it the start of a code span, is left as text instead, as is any link or URL around one, so that no
  address carries the zero-width space: in ``See https://example.com/`a <= b`.`` the address isn't linked and
  `a <= b` stays code. (#307)
- `submit --project-dir`, `ci` and `review` read the `aristotle.f` they find by its path, and `submit` reads each
  filelist its `-f` and `-F` name by the path given: a `*`, `?` or `...` there is part of a name, not a wildcard.
  Such a path used to be matched as a pattern, as the paths inside a filelist still are, the way slang matches them.
  From a folder named `a?b`, a sibling folder `axb`'s `aristotle.f` was read too and the sources it names went up, or
  `ci` and `review` stopped on it unless `--allow-untracked` named `axb`; from a folder named `...b`, a folder `b`'s
  was read in place of the folder's own. A pattern given to `-f` or `-F`, such as `-F 'rtl/*.f'`, now stops the
  command with `rtl/*.f: no such file`: name each filelist with an `-F` of its own. A path that ends in a slash, such
  as `-F rtl/`, stops it with `rtl is not a file`, where every file in the folder was read as a filelist. (#300)
- `submit`'s `-f` and `-F` take a value with a comma in it as one filelist's path, as written: `-F 'a,b.f'` reads
  `a,b.f`. The comma split it into two filelists, as a comma still does in a filelist's line, so the command stopped
  with `the command line: a: no such file`. (#303)
- A file named with a trailing slash, as in `-F ip.f/`, by `-f`, `-F`, `-v` or a source line, stops the command with
  `ip.f/: ip.f is a file; drop the trailing slash`, where the message said `ip.f is not a file`. A folder that `-f` or
  `-F` names on `submit`'s command line, as in `-F rtl/`, still stops it with `rtl is not a file`. (#303)
- A generated `aristotle.f` names no path that Aristotle reads as a pattern. Aristotle matches `*` and `?` in a path
  there, and `...` at the start of a folder's or file's name, as slang does, so the line `rtl*/fifo.v` also read
  `rtl_old/fifo.v` when that went up too, and `...b/fifo.v` read `b/fifo.v` in its place. A source, `-v` file or `-I`
  folder that it would name by such a path now stops `submit`, `ci` and `review` before anything is sent, naming it,
  and so does an `-I` folder whose path holds a comma, which slang splits there. (#303)
- An `aristotle.toml` with an integer past Python's digit limit, 4,300 digits by default, or with arrays, inline
  tables or dotted keys nested deeper than the TOML reader can follow, makes `aristotle ci` exit 2 with `<path> is not
  valid TOML: …`, as any other TOML error does. It exited 3 with `Aristotle could not complete the run: …`, which
  didn't name the file. A `kind` holding such an integer, as a hexadecimal one can, or tables nested too deep to
  print, and a `max_cost_dollars` past a float's range, are named under their keys as any other invalid value is,
  where they also exited 3. A decimal character reference in a property's description with more digits than that
  limit, such as `&#999…9;`, or `&#65;` padded with zeros, decodes as its value does, U+FFFD past U+10FFFF, where
  reading the property failed. `ci setup` reads a Verilog line with many `(*` and no `*)`, and `submit`, `ci` and
  `review` one with many `` `include < `` and no `>`, in linear time: a long enough line took minutes. (#301)
- `aristotle ci` and `aristotle review` say when the root `aristotle.f` exists but git doesn't track it, as when it
  hasn't been committed yet: that they don't read it or upload it, and to commit it. They said only that no Verilog
  filelist was uploaded. An `aristotle.f` that `submit`, `ci` or `review` can't open is named on its own, as in
  `Can't read aristotle.f: Permission denied`, where the message said it came from the command line. (#297)
- The GitHub Action's `on-timeout` input is empty by default, as `fail-on` and `timeout` are, which leaves it to the
  command: `ci` and `review` cancel their run when the budget runs out or the job is cancelled, and `ci --project`,
  set on the step with `ARISTOTLE_PROJECT`, leaves the run it reports on going. The default, `cancel`, overrode that.
  (#299)
- `show --wait --non-interactive` waits as `show --non-interactive` does, as `submit` and `continue` take the pair. It
  exited 2. (#299)
- `aristotle review --scope changed` no longer passes a change whose only source files to verify are ones an ignore
  rule holds back, though git tracks them. When one of them is Verilog, the review reads the block the root
  `aristotle.f` describes and verifies those it takes; otherwise, or when it takes none, the review exits 2, whatever
  `--fail-on` is, and names them, with `--path` as the way to keep a file out on purpose. It said "Nothing to do" and
  exited 0, so a CI job read as clean though nothing was checked. A held-back file the block takes is also verified
  beside the rest of a change, or of what `--scope paths` matches, where it went up unverified. (#286)
- At a terminal, the review before answers to Aristotle's questions are sent shows each question on one line, cut to
  the terminal's width, above the answer chosen for it, and after a single question, which is still on screen, only
  the answer. It used to print every question again in full, so a long question was shown twice. (#281)
- A lookup by name or id prefix asks for its pages as fast as any request goes, five a second, instead of two, and
  slows to two a second for the rest of the lookup once Aristotle's per-second limit refuses one of them, so five
  lookups at once still fit within it. It still reads every page in the window, since only the whole window shows
  whether a match is unique: on an account with about 1,650 projects, a lookup takes about 3.5 s instead of 8. At a
  terminal, the note that it is searching your 2,000 most recent projects comes only once the lookup has run 1.5 s;
  with `--json`, `--non-interactive` or `--answer`, or when stderr isn't a terminal, it comes as the second page
  starts, as before. `tasks`, `download`, `cancel`, `questions`, `ci --project` and `ci setup --from` use the project
  as the lookup listed it, rather than fetching it again. (#288)
- A network path (`\\host\share\…` or `//host/share/…`), a Windows device path (`\\?\…`, `\\.\…` or `\??\…`) or a
  drive-relative one (`C:x.v`) in a filelist, an `` `include`` or `--allow-untracked` stops the command before
  anything looks it up, on every platform, and the message says where it is. Such a path used to be looked up like
  any other, and on Windows, looking up a network path connects to the computer it names and signs in to it, so a
  change that named one in a committed `aristotle.f` or an `` `include`` could make `aristotle ci` on a Windows
  runner sign in to a computer of the change's choosing with the runner's credentials. (#275)
- `aristotle ci`, `aristotle review`, `aristotle ci setup` and `submit --project-dir` look for a folder's repository
  no higher than git does, stopping at `GIT_CEILING_DIRECTORIES`, so they agree with git about whether a folder is
  in one. A repository above a ceiling, such as one in the home folder, made them take a folder outside every
  repository for one inside it, and say git refused it. (#257)
- `show --json`, `submit --wait --json` and `continue --wait --json` print `property_counts`, `requested_counts` and
  `properties` as `null` when the project's tasks can't be listed, after saving any files they would, and say how to get them later:
  `show --json` exits as for the error (3, or 6 when rate limited), and the `--wait` results exit 3. They used to
  judge the verdicts by the tasks they did list, so a proof that a cancelled run left unchecked could count as
  checked. `continue --json` without `--wait` prints them `null` too, and still exits 0. `ci setup --from` writes
  nothing then, and the text output warns that a proof or refutation may count as checked. (#252)
- `aristotle properties --status` repeats, and lists the properties with any of the statuses given, so
  `--status PROVED --status REFUTED` lists both outcomes; it kept only the last. `--status ALL` with
  another status exits 2. (#241)
- A manifest property a run had registered nothing under when it ended early, by running out of budget or time,
  failing or being cancelled, is labelled `not registered, run cut short`, not `not registered`, which reads as
  Aristotle finding nothing under the id. (#249)
- `aristotle ci setup --from` sizes the time budget from the run that last updated the properties it copies, the
  longest if several did, and names that run's task in the manifest's comment. It took the project's newest task,
  so a short follow-up after a long run cut the budget to the follow-up's length. (#246)
- `aristotle ci setup` leaves out the `Suggested:` and `Claim that` lead-ins of Aristotle's report from the
  statements it writes, as it leaves out a prompt's list numbers. (#246)
- On GitHub, the job summary of a run the time budget left going (`--on-timeout leave`, or `--project`) says that
  it goes on and how to report on it later, as the log does. (#247)
- An invalid `--timeout` (`ARISTOTLE_TIMEOUT`) or `--name` (`ARISTOTLE_NAME`) says why, as in `'99999' is not
  valid (from the flag or ARISTOTLE_TIMEOUT): the timeout must be a positive number of minutes, at most 10080`. (#247)
- A misspelled `--base-ref` or `ARISTOTLE_BASE_REF` says origin has no such branch, where it blamed the clone's depth
  and sent the user to `fetch-depth: 0`, which doesn't fix a typo. A base branch that can't be fetched for another
  reason gives git's error without the hints it adds after it, and keeps the fetch-depth advice, the fix when the job
  has no credentials to fetch with, except in a checkout with no origin. (#240)
- A run whose directory holds tracked files that differ from HEAD, staged or not, is named
  `@ <branch> <commit>+changes`, and the log says it checks uncommitted changes. A fix checked before committing it
  shared the commit's name with the run of the commit itself. (#250)
- `aristotle ci`'s prompt tells Aristotle that nobody can reply to a CI run and that answers go in `aristotle.toml`,
  and the log of a run it stopped for questions says a CI run takes no reply. Its summary invited one, which
  started a paid follow-up the gate doesn't use. (#251)
- `aristotle ci` without a manifest names both ways to make one, writing it or `ci setup --from`, as plain
  `ci setup` needs a terminal; outside a git repository it says that instead. After a lost create, it no longer
  repeats `submit`'s advice to check `aristotle list` before looking the project up itself, and `ci setup`'s
  comments no longer join a sentence to the next with `.;`. (#251)
- At `aristotle questions`' "Answer it now? [Y/n]", a reply other than yes or no, such as an answer typed straight
  away, says "Nothing was sent. Type y to answer, n to leave it." and asks again, where the command ended silently;
  after that only y starts answering, so a blank line in a pasted answer doesn't. A no says nothing was sent and how
  to answer later. (#234)
- `aristotle ci setup` writes the manifest's time budget as half as long again as the run it is written from took,
  and at least an hour, with a comment saying so. It always wrote an hour, even from a run that took 214 minutes, so
  `aristotle ci` cut the next such run short. (#227)
- On Windows, the commands the CLI prints to paste keep their paths and names as printed in cmd.exe and PowerShell,
  quoted for both: those `ci setup` prints after writing the manifest and to write it again, the `aristotle rename`
  command `submit` prints when naming the project fails, and the command `submit --non-interactive` and `show` print
  to resume a wait. `ci setup` double-quoted its paths, so PowerShell expanded `$work`, as cmd.exe expands `%NAME%`,
  and a pasted `git -C` could act on another folder; the rename command was single-quoted for POSIX shells. A word
  with `$`, a backtick or a curly double quote gets a command for each shell, and one with `%`, `!`, a straight double
  quote, a trailing backslash or a line break PowerShell's alone: cmd.exe ends a command at a line break, quotes or
  not. Any other word that needs quoting goes once in double quotes, where the resume command gave two. (#228, #284)
- Outside Windows, the commands the CLI prints to paste keep their words as printed in zsh and pwsh too: a word that
  starts with `=`, which zsh replaces with a program's path, is quoted. Where `PSModulePath` is set, as under pwsh and
  in every step on GitHub's Ubuntu runners, a command whose PowerShell quoting differs from its POSIX one is printed
  for both shells, each labelled: pwsh split the POSIX-quoted rename of `Bob's proj` in three. (#228)
- `aristotle ci` and `aristotle review` report a run that finished just as the time budget ran out as it ended, with
  its verdicts. When the cancel that ends the budget found the run already finished, they used to report its results
  as unchecked and exit 3. (#224)
- `aristotle ci` and `aristotle review` say a property a finished run left without a verdict went unchecked, where they
  said it was "still in progress when the run ended", and print Aristotle's summary, which usually says why, whenever a
  property has no verdict, not only when one fails, unless the time budget cut the run. When the build passes with
  properties without a verdict under `--fail-on failure`, `aristotle ci` warns, naming them, and says `--fail-on no-verdict`
  fails it on them. A run cut short by its budget or the time budget, failed, cancelled or never started is an error in
  the JUnit report, with its reason, whatever `--fail-on` is, as a run Aristotle stopped for questions was, and the log
  and the report say when such a run stopped for a manifest property it had nothing under. (#235)
- `aristotle ci` no longer passes a run that ran out of budget, or hit its time budget, on the proofs it had
  reported: Aristotle checks a run's results when the run finishes, so those proofs were never checked. Such a run
  exits 3, unless it ran out of budget and `--fail-on never` is set, and every report labels what it reached as
  unchecked: a proof or refutation it reported has no verdict, as `proved, unchecked` or `refuted, unchecked`, and the
  summary line counts them (`0 proved, 0 refuted, 0 in progress, 2 unchecked (1 refuted and 1 proved)`).
  `aristotle review` reports such a run the same way, and exits 3 under `--fail-on failure` or `no-verdict`, or
  whatever `--fail-on` is when the time budget cut it. (#164, #249)
- A run that failed or was cancelled elsewhere also ends before Aristotle checks its results, so `aristotle ci` and
  `aristotle review` report a proof it had reported as `proved, unchecked` instead of `PASS`. Such a run still exits
  3, or 1 when a refutation it reported fails the build, since a refutation keeps its verdict. `aristotle ci setup`
  notes as unchecked any verdict from an out-of-budget run, which it used to note as proved or as a refutation that
  fails CI, and a proof from a failed or cancelled one; an unchecked refutation draws no warning unless Aristotle also
  expects the property to be refuted, or calls it a deliberate counterexample. (#193)
- `aristotle ci`'s annotation and JUnit entry for a property still in progress lead with `in progress` and the reason,
  such as that Aristotle stopped the run for questions, where they gave only the property's statement, which read as
  though it held. A run Aristotle stopped is an error in the JUnit report, which showed nothing wrong. The summary
  line counts unchecked refutations and proofs apart, and an unchecked refutation's annotation comes ahead of those of the
  other properties without a verdict, so the limit of 10 per step drops those first. (#205)
- When `aristotle ci` or `aristotle review` can't cancel a run, it says why, that the cancel didn't finish within 5
  seconds when it ran out of time, and prints `aristotle cancel <project-id>` to cancel it by hand. An interrupt
  whose cancel outlasted 5 seconds logged `Could not cancel the run: ` with nothing after it. (#198)
- On GitHub Actions, an SVA delay range with a parameter, such as `##[1:LATENCY]`, or a leading space, such as
  `##[ 1 : 3 ]`, prints as written instead of with a backslash after `##[`: a `##[` whose first word holds a colon, or
  that has none, names no workflow command. One with a space before its colon, such as `##[1 : 3]`, keeps the
  backslash. (#198)
- `aristotle ci`, `aristotle review` and `submit` hold back a tracked file only for a rule in one of the repository's
  `.gitignore` files, never `.git/info/exclude` or a global excludes file, which are one machine's alone; an untracked
  file still stays out for any rule. `submit` says what it left out matches an ignore rule, not that `.gitignore`
  excludes it, and `ci` and `review` say no Verilog filelist is uploaded, rather than committed. (#223)
- The credential, non-UTF-8 and not-a-file warnings name every path, not the first five. Every candidate Verilog
  filelist is offered, not the first ten, and the prompt names only those the upload holds, so one it left out, such
  as a broken link, is no longer named. (#223)
- `aristotle ci`, `aristotle review` and `aristotle ci setup` no longer stop at a tracked file whose name isn't UTF-8,
  such as `café.v` in Latin-1. They leave it out of the upload, since Aristotle fails a run on such a name, and name it
  with each byte that isn't UTF-8 as `\xNN`. A `review` of a change to only such source files says so, and fails under
  `--fail-on no-verdict`. They used to stop with `'utf-8' codec can't decode byte`, exit 3, and `ci setup` printed a
  traceback. (#182)
- `aristotle ci setup` in a repository whose folder name isn't UTF-8 uploads under the folder's name in ASCII with a
  random suffix, as `ci` and `review` do, instead of stopping with a traceback. (#191, #319)
- A faulty SOCKS proxy no longer hangs or crashes a command. An attempt whose connection isn't open within 25 seconds,
  as through a proxy that never answers the handshake, fails as a timeout and is retried, so a request gives up within
  about two minutes; `show` and `aristotle ci --timeout` used to wait forever. Through an HTTP proxy
  that never answers the tunnel request, a `submit` or `continue` is retried as unsent instead of exiting 7. A proxy
  that drops the handshake, or refuses it with a failure a later try may pass, such as `General SOCKS server failure` or
  `Host unreachable`, is retried like a failed connection and then exits 3 with one line, where 3.0.0rc5 printed a
  `socksio` traceback or gave up at once. A server certificate this machine doesn't trust fails at once with an error
  that names `ARISTOTLE_CA_BUNDLE`, where the pre-releases retried it for about 45 seconds. (#203)
- `aristotle review --scope changed` stops with exit 2 when CI reports the commit before a push and it can't be fetched,
  as for a base branch with no merge base. It used to compare with the last commit alone, so it could pass a push it
  never checked, even under `--fail-on no-verdict`. (#182)
- After a refuted property, `aristotle ci` and `aristotle review` print the run's summary, where Aristotle usually
  describes the counterexample: whole in the log, quoted in the GitHub job summary, and with each JUnit failure, the
  last two cut at 4000 characters. A refuted property's annotation points at it. Aristotle records no counterexample with the
  property yet, so the failing row still shows the property, and the CI guide no longer promises the counterexample
  there. (#185)
- SVA or C copied from a code span or fenced block in the GitHub job summary parses: code spans and fenced blocks from
  the run's summary are quoted as written, without the zero-width space after each `<` they got before. HTML outside
  them stays inert. Where Markdown could read a backtick otherwise, its code spans get the zero-width space again,
  while fenced blocks at the start of a line stay as written: in a summary with a table that splits a code span at its
  `|`, or a backtick unpaired on its line, after a backslash or in a run of more than 16. An indented fence makes the
  whole summary inert, fences included. (#198)
- `aristotle ci` and `aristotle review`, waiting for a run slot, upload the checkout again only once one of your
  projects in progress finishes and no other run starts in its place. They used to upload it again whenever one of
  them finished, so on a busy account another pipeline's run could take the slot first and each upload was refused
  in turn: 15 in half an hour. Where the time budget ends the step, they now say that waiting for a free slot counts
  toward `--timeout`, as its help does. (#214)
- The table in `aristotle ci`'s GitHub job summary no longer carries live HTML from a property's text. A description,
  and the assumptions under it, keep their code spans while everything else is inert, as in Aristotle's summary;
  `Holds <img src=x>.` used to render its image. A backtick in a property's name or file path, which a repository file
  name can hold, no longer ends its code span early. A Markdown image, in a description or in Aristotle's summary, is
  now a plain link, so opening the summary fetches nothing from elsewhere and shows no picture of a result. (#211)
- `aristotle ci` and `aristotle review` exit 6 for a run still queued when the time budget runs out, whatever
  `--fail-on` is, as they do when Aristotle refuses a run and no slot frees up in time. `ci` used to exit 1, as for a
  run that verified nothing, or 0 under `--fail-on never`; `review` exited 0, or 1 under `--fail-on no-verdict`.
  (#184)
- `aristotle ci --dry-run` and `aristotle review --dry-run` build the upload a run would send, so they leave out what
  a run leaves out (a symbolic link whose target isn't uploaded, a submodule, a file missing from a sparse checkout)
  and print its file count and compressed size. They used to count every tracked file but credentials. An upload over
  100 MB stops a run, a dry run or `aristotle ci setup` with exit 2, and the message names the repository; a run used
  to exit 3 naming its temporary archive. A run with nothing to verify in its upload exits 2 too, where it exited 3,
  and so does a dry run, which passed. (#183)
- When a run ends without results, `aristotle ci` and `aristotle review` give a reason the run supports. After a run
  that verified nothing, or one cut short with no property under the manifest's ids, they ask Aristotle whether
  Verilog and C are enabled for the account, for the languages Aristotle finds in the checkout that the run is
  about. They name a language as a cause only when it is off, or as a possibility where Aristotle can't say, as for
  Rust. Before, the causes always began with the account's languages, even for a run that registered its properties
  and left them open. A cut-short run with no property under the manifest's ids says so, instead of calling its
  results unchecked. With `--on-timeout leave`, a run the time budget ran out on says it goes on, with the
  `aristotle show` and `aristotle cancel` commands for it. (#177)
- When `aristotle ci` and `aristotle review` name Rust as a possible cause, they say Aristotle gives no way to check
  whether the account can verify it, rather than that it may not be enabled. A `review` run that registered no
  properties prints its summary, which says why. (#223)
- `aristotle review --scope changed` checks a change to a Lean file instead of passing it as touching no source file,
  even under `--fail-on no-verdict`. `.lean` files are source files, and a change to one of Lean's build files
  (`lakefile.lean`, `lakefile.toml`, `lake-manifest.json`, `lean-toolchain`) counts as a change to every Lean file in
  its folder and below, though the build files themselves are not verified. (#180)
- `aristotle ci setup` writes a manifest that stands without hand-editing. A statement loses the list number a prompt
  put before it, as in `(1) No overflow: …`, and ends with the file it is about and its Verilog module when it doesn't
  name them. A property on a labelled SystemVerilog assertion that says it is that assertion, by its name or by a
  description opening "The assertion <label> holds", is written as `kind = "sva"`, and one Aristotle expects to fail
  says so in its note, with a warning. The next step it suggests is `aristotle ci --dry-run`, not a run. (#186)
- `aristotle ci setup --from` copies a project's statements as they are, and flags one that may depend on that
  project's run with a comment and a warning: a statement or `assumptions` that mentions "the input contract", which
  may come from its conversation, or fixes a parameter to a number the code sets only to others. Setup says a `--manifest` folder doesn't exist, rather than that it is
  not a directory, and gives no git commands for a manifest outside any repository. A run that ends without completing
  its request says its summary tells what is left, instead of telling you to clarify a prompt that `aristotle ci
  setup` wrote. (#204)
- `aristotle ci` finds its manifest from a subfolder: outside CI it reads the nearest `aristotle.toml` from the
  working directory up to the repository's top level, where it read only the working directory, and says where it
  looked when there is none; with `--manifest` and no `--directory`, it checks the repository from its top level.
  `aristotle review` checks the whole repository from a subfolder, with `--path` patterns relative to its top level.
  `WORKSPACE` names the checkout only under Jenkins, since outside it any tool may set it. (#216)
- `aristotle ci`, `aristotle ci setup` and `aristotle review` say when git is not installed, and pass on git's reason
  when it refuses a checkout, such as another user's, with the `safe.directory` command it gives. They said the folder
  was not a git repository. So did `aristotle review`, and `aristotle ci` with `--manifest`, for a `--directory` that
  isn't a folder or can't be read, which now says so. In a checkout git can't read, a run from a subfolder finds the
  manifest at the top level, where it said there was none, and `aristotle ci setup --from` gives the git commands to
  commit the manifest it writes, where it said the manifest was outside any repository. (#230)
- A manifest's error names every problem: an id that isn't valid no longer hides its entry's others, nor a missing or
  unknown `kind` the statement's. An `aristotle.toml` with no `requested_properties`, as another tool's file of that
  name would be, gets a hint to pass `--manifest`. (#216)
- On Python 3.10 and 3.15 or later, `aristotle ci` rejects a TOML 1.1 manifest, as the image's Python does: it reads
  manifests with its own copy of tomli 2.3 on every Python, since tomli 2.4 and Python 3.15's tomllib read TOML 1.1.
  (#216, #236)
- Four messages say what to do next. A `questions --answer` whose reply was lost says how to check before answering
  again, as `submit` and `continue` do: `aristotle questions <id> --json` shows an answered question's `response`. A
  request refused by the per-second limit whose `Retry-After` passes the retry deadline says how long Aristotle asked
  to wait, instead of "a few seconds". A slow name or id-prefix lookup says it is searching your 2,000 most recent
  projects. After a refuted property, `aristotle ci` and `aristotle review` give the
  `aristotle download` command for the run's files, where a reproducer its summary names would be. (#202)
