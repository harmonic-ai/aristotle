# Aristotle

Aristotle formally verifies your code. Point it at a project and state the property you need to hold, and it returns
a proof or a counterexample rather than a test run. Lean is available on every account; Verilog, C and Rust are
available on accounts where they're enabled. This package is the Python library and the `aristotle` command-line tool
for the Aristotle API.

[Sign up for access and view the complete documentation at aristotle.harmonic.fun](https://aristotle.harmonic.fun)

## Quick start

```bash
uv tool install aristotlelib
aristotle submit "Prove this FIFO never overflows" --project-dir ./my-design --wait
```

The first command that needs a key offers to log in: it shows a code to approve in your browser, and keeps the key
for next time. Aristotle works out the language from the files you upload. Each run spends your account's quota and
takes tens of minutes, even for a small design. `--wait` follows the run, asks you Aristotle's questions as they come,
and ends with a count such as `Properties: 2 proved · 1 refuted · 0 in progress`. Then it saves the result archive
in the current folder. To see what each property came to:

```bash
aristotle properties <project-id>
```

## Install

The CLI needs Python 3.10 or later. Install it with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install aristotlelib
```

`uv tool upgrade aristotlelib` updates it. Without uv, or to use the Python library in a project, run
`pip install aristotlelib` in a virtual environment.

## Log in

```bash
aristotle login
```

This prints a link and a code, and opens the link in your browser when it can. Open the link where you're signed in
to Aristotle, enter the code, check that the page names this machine, and approve. Aristotle makes a key named after
this machine, and the CLI keeps it for every later command, readable only by you. It goes in
`~/.config/aristotle/credentials.json`, under `$XDG_CONFIG_HOME` if that's set, or in `%APPDATA%\aristotle` on
Windows.

Over SSH or in a container, open the link on any device where you're signed in. A coding agent can run
`aristotle login` for you and pass on the link and the code, so the key never passes through the conversation.
`aristotle logout` removes the stored key. The key stays valid until you revoke it on
[API keys](https://aristotle.harmonic.fun/keys).

In CI and scripts, set `ARISTOTLE_API_KEY` instead. It takes precedence over a stored key.
[The CLI reference](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md#logging-in) has the
other ways to give a key.

## Glossary

- **Project:** what you submit, with its files and conversation.
- **Run:** one piece of work on a project. `submit` starts one, and a `continue` to an idle project another. The CLI
  calls a run a *task*.
- **Property:** a claim Aristotle proves or refutes. The JSON calls it a *declaration*.
- **Verdict:** proved or refuted. A property without one is in progress, unresolved or unchecked.
- **Manifest:** `aristotle.toml`, the properties `aristotle ci` checks.

## Verify a project

State the property precisely: the module or function, the signals or variables, the assumptions, such as reset and
input ranges, and what must hold. Ask for one property, or a few, per run.

```bash
aristotle submit "Prove that module fifo in fifo.sv never accepts a write while full, from any state after reset" \
  --project-dir ./rtl --wait
```

`--project-dir` uploads the folder as one compressed archive of at most 100 MB. It leaves out `.git`, build output,
what your `.gitignore` excludes, files named like credentials (`.env`, `*.pem`, `.ssh/` and the like) and result
archives an earlier run saved. The credential check goes by name only, so keep secrets out of the folder.
`--include-ignored` uploads the ignored files too, such as sources a build generates. With `--dry-run` in place of
`--wait`, `submit` lists exactly what would go up, and what's left out: it sends nothing and needs no key.

Wherever a command takes a project, give its id, the first 8 or more characters of it, or its name:

| Command | What it does |
|---|---|
| `aristotle list` | Lists your recent projects, and whether each is running. |
| `aristotle show <project>` | Says how the latest run stands or ended. At a terminal, it follows a running one. |
| `aristotle continue <project> "<prompt>" --wait` | Gives a project another instruction: the run in progress takes it, or a new run starts. |
| `aristotle questions <project>` | Lists Aristotle's questions, and answers the pending ones. |
| `aristotle properties <project>` | Lists what each property came to. |
| `aristotle property <project> <name>` | Shows one property in full, with any files Aristotle linked to it, such as a counterexample's trace. |
| `aristotle download <project>` | Saves the result archive, or with `--artifact`, some of the project's files. |
| `aristotle tasks <project>` | Lists a project's runs. |
| `aristotle cancel <project>` | Cancels the run in progress. |
| `aristotle rename <project> "<name>"` | Renames a project. |

Aristotle may ask you something during a run, and waits up to 15 minutes for each answer. With `--wait`, the CLI shows
each question as it comes, with suggested answers to pick from. Otherwise, answer with `aristotle questions <project>`
or in the web app. For a batch run nobody will answer, pass `--no-questions`. Aristotle then leaves what needs your
decision unverified, and names it in its summary.

To upload selected files with a new instruction, give their local paths and the root they share. The paths inside
that root are preserved, so files in different folders can have the same name. The root defaults to the current
directory; selecting fewer files never changes it. For example:

```bash
aristotle continue <project> "Use these proofs" --project-dir downloaded \
  --files downloaded/Proofs/Foo.lean downloaded/Other/Foo.lean
```

`download --artifact` preserves paths beneath its destination folder; use that folder as `--project-dir` when
uploading again. Plain `download` saves the archive unchanged: extract it first and choose its project root.

## Read the results

`aristotle show <project>` says how the run ended, with a line counting the properties.
`aristotle properties <project>` lists each one:

| Status | Meaning |
|---|---|
| `PROVED` | Aristotle proved it. |
| `REFUTED` | The `EVIDENCE` column distinguishes a counterexample, a proof of the negation, or both. |
| `IN PROGRESS` | No verdict yet. |
| `UNRESOLVED` | The run stopped short of it, or its proof no longer checks. The reason follows. |
| `(unchecked)` after a status | A proof or refutation Aristotle never checked, because the run ended first. It isn't a verdict yet. |

Every refutation has `evidence`: `COUNTEREXAMPLE` (1), `NEGATION_PROVED` (2) or `BOTH` (3), or `UNKNOWN` (0) for a
kind added after this version. `aristotle property` shows the kind separately and the agent's `evidence_message`
before its artifacts. JSON preserves that message, including inline Markdown. Each refuted property counts once,
including one with both kinds of evidence. Responses from older API deployments are normalized to this shape;
inferred evidence does not create a message.

To summarize or look up results, read them with `show`, `properties`, `property` or `download`, which start no run.
Don't send a `continue` just to ask about them: on an idle project it starts a new run, which can spend hours checking
the proofs again before it answers.

A wait can end before the run does, or the run can fail, be cancelled or run out of Aristotle's budget. Either way,
the CLI prints the command that carries on, such as `aristotle show <project-id> --task <task-id> --wait` or
`aristotle continue <project-id> Continue --wait`. Run it.

[The CLI reference](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md) has the rest of
the detail: what goes up, how a project name is looked up, how waits and questions work, a property's files, and what
each status means.

## Verilog designs

Some designs need their top module, defines or include folders spelled out. Put them in an `aristotle.f` at the root
of the folder you submit, or commit it at the root of the repository `aristotle ci` checks:

```
--top soc_top
-I rtl/include
-D DATA_WIDTH=32
--blackbox sram_1p
-F rtl/soc.f
```

It's read as a simulator reads a filelist, so the one your flow writes usually works as it is. Every file it names or
includes goes up. `--blackbox` has Aristotle treat a module, such as a foundry cell, as a black box with unconstrained
outputs. A file that declares nothing else stays out of the upload: one the filelists name, or a `.v`, `.sv`, `.vh`
or `.svh` file they don't. A model encrypted whole declares nothing the CLI can read, so take it out of the filelists,
and out of the folder or checkout that goes up, yourself. `aristotle ci setup`, and an `aristotle review` that checks
no Verilog file, don't read `aristotle.f` and send the whole checkout: match a model that mustn't be sent with a
`.gitignore` rule.
`aristotle submit --dry-run` lists every file of the design, with where it comes from.
[docs/verilog.md](https://github.com/harmonic-ai/aristotle/blob/main/docs/verilog.md) has the details.

## Coding agents

The [harmonic-ai/aristotle repository](https://github.com/harmonic-ai/aristotle) ships an
[Agent Skill](https://agentskills.io), `skills/aristotle`, which teaches coding agents such as Claude Code, Codex and
Cursor to use the CLI. With it, an agent submits a project, answers Aristotle's questions from
your code and docs, reads which properties hold, and keeps `aristotle.toml` in step with the code it changes. To set
it up, ask your agent to follow
[docs/agent-setup.md](https://github.com/harmonic-ai/aristotle/blob/main/docs/agent-setup.md). In Claude Code,
you can also install it yourself:

```bash
claude plugin marketplace add harmonic-ai/aristotle
claude plugin install aristotle@harmonic
```

The harmonic-ai/aristotle repository is not public yet, so these work only for accounts that have been granted access
to it.

## Scripts and automation

Scripts, CI jobs and coding agents get the same commands. `--json` prints a command's result to stdout as JSON, and
progress, warnings and errors go to stderr. In a script, or with `--json` or `--non-interactive`, a command with no
key exits 5 rather than offering to log in. `aristotle login --json` prints the link and the code for a person to
approve as soon as it starts, then waits for them. `--prompt-file` reads a long prompt from a file, or from a pipe
with `-`.

`--non-interactive`, on `submit`, `continue` and `show`, waits without ever prompting. When Aristotle asks something,
it prints the questions as JSON and exits 10. Answer them, then carry on waiting with the command it prints on stderr:

```bash
aristotle submit "Prove the FIFO never overflows" --project-dir rtl --non-interactive > run.json
aristotle questions <project-id> --answer <question-id>="Active low"
aristotle show <project-id> --non-interactive > run.json
```

Every command exits with one of these codes, so a script can tell a failed run from a mistyped id:

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | The run finished without completing the request, or `aristotle ci`'s verdicts failed the build. |
| 2 | A usage error: a missing or invalid argument, input Aristotle can't take, or a name that matches several projects. |
| 3 | The command couldn't complete: the run failed or was cancelled, a wait ended before the run did, or an error outlasted the retries. |
| 4 | No such project, run, property or question for your key, or Aristotle refused the action on it. |
| 5 | No API key, Aristotle rejected it, or a login was denied in the browser or its code expired. |
| 6 | Rate limited: too many runs in progress, or too many requests at once. |
| 7 | The request may have gone through. Check before you send it again. |
| 10 | A `--non-interactive` wait stopped for Aristotle's questions. |
| 130 | Interrupted by Ctrl-C or SIGTERM. A run you were following keeps going, unless `aristotle ci` or `aristotle review` started it. |
| 141 | stdout or stderr closed early, as by `\| head`, on Linux and macOS. |

The CLI retries server errors, dropped connections and too many requests at once, and gives up within about two
minutes. A wait rides out an outage of up to 10 minutes. It sends a `submit`, `continue` or answer again only when
Aristotle can't have acted on it. When one may have gone through, it exits 7: look for it (`aristotle list`,
`aristotle tasks <project>`, `aristotle questions <project> --json`) before you send it again. When it says nothing
was done (exit 3, or 6 when rate limited), it's safe to run the same command again.
[The CLI reference](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md) has every `--json`
shape and field, each exit code in full, and a script that answers questions.

## Run it in CI

`aristotle ci` checks the properties listed in `aristotle.toml`. It reports each one the way your CI already reports
tests: a JUnit report, and on GitHub annotations and a job summary. It detects GitHub Actions, GitLab CI, Jenkins,
Buildkite, CircleCI and Azure Pipelines on its own. It verifies Verilog, C and Rust sources, which are available on
accounts where they're enabled. Each property has an id, and is stated in plain language or names
an assertion or a C function in the code:

```toml
[requested_properties.fifo_no_overflow]
kind = "natural_language"
property = "The FIFO never accepts a write while it is full."

[requested_properties.arbiter_one_grant]
kind = "sva"
identifier = "arbiter.a_one_grant"
```

`aristotle ci setup` has Aristotle propose a manifest for you to review and commit. Then add a step to your pipeline,
as in this GitHub Actions workflow:

```yaml
on:
  pull_request:
    paths:              # every run proves every property, so run only when they could change
      - "rtl/**"
      - "aristotle.toml"

concurrency:            # a new push cancels the job, and its run, for the commit before it
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: harmonic-ai/aristotle@v3
        with:
          api-key: ${{ secrets.ARISTOTLE_API_KEY }}
```

The harmonic-ai/aristotle repository is not public yet, so the action resolves only in workflows that have been
granted access to it. `@v3` doesn't exist until 3.0.0 is released: until then, use the tag of the release candidate you
were given.
[docs/aristotle-in-ci.md](https://github.com/harmonic-ai/aristotle/blob/main/docs/aristotle-in-ci.md)
has the manifest's format and recipes for GitLab CI, Jenkins and other CI systems. It also covers what fails the
build, `aristotle review`, time and cost, and what gets uploaded.

## Python library

The CLI is built on the `aristotlelib` package, which a program can use directly. It finds the key as the CLI does, or
takes one from `aristotlelib.set_api_key()`:

```python
import asyncio

from aristotlelib import AgentQuestionsSetting, Project


async def main() -> None:
    project = await Project.create_from_directory(
        "Prove this FIFO never overflows",
        "./my-design",
        agent_questions_setting=AgentQuestionsSetting.DISABLED,  # nobody is there to answer
    )
    tasks, _ = await project.get_tasks(limit=1)
    await tasks[0].wait_for_completion(interactive=False)
    print(tasks[0].status.name, tasks[0].output_summary)
    print("Saved", await project.get_files())


asyncio.run(main())
```

The library uploads a folder as `submit --project-dir` does, but doesn't read its `aristotle.f`: it neither leaves out
a black box's files nor points Aristotle at the filelist. Send such a design with `aristotle submit`. The library
logs to the `aristotle` logger, which prints nothing until the program configures logging.

To upload selected files to an idle project, `Project.ask()` takes the same local root as `continue --project-dir`
(default: the current directory):

```python
await project.ask("Use these proofs", files=["downloaded/Proofs/Foo.lean"], project_dir="downloaded")
```

Files outside the root, including through symlinks, paths containing `..`, and duplicate upload paths are refused.

## Proxies and private networks

The CLI honors `HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY`. Behind a TLS-inspecting proxy, point it at your company's
CA bundle with `ARISTOTLE_CA_BUNDLE`. The only host it needs is `aristotle.harmonic.fun`, on port 443. When a proxy
or a firewall refuses a request, the CLI says the refusal may come from it, and what to do next.
[The CLI reference](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md#proxies-and-private-networks)
has the messages, what `--verbose` adds, the other variables the CLI reads, and what each request's `User-Agent`
header carries.

## Help

`aristotle --help` lists the commands, and every command has its own `--help`. More at
[aristotle.harmonic.fun](https://aristotle.harmonic.fun).

## License

aristotlelib is licensed under the terms in the `LICENSE` file that comes with it, which refer to the
[Aristotle API Terms of Use](https://aristotle.harmonic.fun/terms). It includes a copy of tomli, under the MIT
license.
