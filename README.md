# Aristotle

Aristotle formally verifies your code. Point it at a project, state the property that must hold, and it returns a
proof or a counterexample. It verifies Verilog and C.

This package is the `aristotle` command-line tool and the Python library for the Aristotle API. To sign up and read
the full documentation, go to [aristotle.harmonic.fun](https://aristotle.harmonic.fun). Working in Claude, ChatGPT,
or another MCP client? Connect the [hosted MCP server](#mcp-server) instead.

## Quick start

```bash
uv tool install "aristotlelib>=3"
aristotle submit "Prove this FIFO never overflows" --project-dir ./my-design --wait
```

The first command that needs a key offers to log you in. It shows a code to approve in your browser and keeps the
key for next time. Aristotle detects the language from the files you upload.

Each run spends credits from your account and takes tens of minutes, even for a small design. `--wait` follows the
run and asks you Aristotle's questions as they come. It ends with a count such as
`Properties: 2 proved · 1 refuted · 0 in progress`, then saves the result archive in the current folder. To see the
status of each property:

```bash
aristotle properties <project-id>
```

## Install

The CLI needs Python 3.10 or later. Install it with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "aristotlelib>=3"
```

To update it, run `uv tool upgrade aristotlelib`. To install without uv, or to use the Python library in your own
project, run `pip install "aristotlelib>=3"` in a virtual environment.

## Log in

```bash
aristotle login
```

The command prints a link and a code, and opens the link in your browser when it can. Open the link where you're
signed in to Aristotle, enter the code, check that the page names this machine, and approve.

Aristotle then creates a key named after this machine. The CLI stores it for every later command, readable only by
you, in `~/.config/aristotle/credentials.json` (under `$XDG_CONFIG_HOME` if that's set), or in
`%APPDATA%\aristotle` on Windows.

Over SSH or in a container, open the link on any device where you're signed in. A coding agent can run
`aristotle login` for you and pass on the link and the code, so the key never appears in the conversation.

`aristotle logout` removes the stored key. The key stays valid until you revoke it on the
[API keys](https://aristotle.harmonic.fun/keys) page.

In CI and scripts, set `ARISTOTLE_API_KEY` instead. It takes precedence over a stored key. For the other ways to
give a key, see [Logging in](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md#logging-in) in
the CLI reference.

## Verify a project

Describe the property precisely: the module or function, the signals or variables, the assumptions (such as reset
and input ranges), and what must hold. Ask for one property, or a few, per run.

```bash
aristotle submit "Prove that module fifo in fifo.sv never accepts a write while full, from any state after reset" \
  --project-dir ./rtl --wait
```

`--project-dir` uploads the folder as one compressed archive of up to 100 MB. It leaves out `.git`, build output,
anything your `.gitignore` excludes, files named like credentials (such as `.env`, `*.pem`, and `.ssh/`), and
result archives saved by an earlier run. The credential check looks only at file names, so keep secrets out of the
folder.

To upload ignored files too, such as sources a build generates, add `--include-ignored`. To see exactly what would go
up and what's left out, replace `--wait` with `--dry-run`. `submit` then sends nothing and needs no key.

Wherever a command takes a project, you can give its id, the first 8 or more characters of the id, or its name:

| Command | What it does |
|---|---|
| `aristotle list` | Lists your recent projects, and whether each one is running. |
| `aristotle show <project>` | Shows the status of the latest run. At a terminal, it follows a running one. |
| `aristotle continue <project> "<prompt>" --wait` | Sends a project another instruction. The run in progress picks it up, or a new run starts. |
| `aristotle questions <project>` | Lists Aristotle's questions, and lets you answer the pending ones. |
| `aristotle properties <project>` | Lists each property with its status. |
| `aristotle property <project> <name>` | Shows one property in full, with any files Aristotle linked to it, such as a counterexample's trace. |
| `aristotle download <project>` | Saves the result archive, or with `--artifact`, selected files from the project. |
| `aristotle tasks <project>` | Lists a project's runs. |
| `aristotle cancel <project>` | Cancels the run in progress. |
| `aristotle rename <project> "<name>"` | Renames a project. |

During a run, Aristotle may ask you questions, and it waits up to 15 minutes for each answer. With `--wait`, the CLI
shows each question as it comes, with suggested answers to pick from. Otherwise, answer with
`aristotle questions <project>` or in the web app. For a batch run that nobody will answer, pass `--no-questions`.
Aristotle then leaves anything that needs your decision unverified, and names it in its summary.

To upload selected files with a new instruction, or to upload files you downloaded, see
[What goes up](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md#what-goes-up) in the CLI
reference.

## Read the results

`aristotle show <project>` reports how the run ended, with a line counting the properties.
`aristotle properties <project>` lists each property with one of these statuses:

| Status | Meaning |
|---|---|
| `PROVED` | Aristotle proved it. |
| `REFUTED` | Aristotle refuted it. The `EVIDENCE` column says whether by a counterexample, a proof of the negation, or both. |
| `IN PROGRESS` | No verdict yet. |
| `UNRESOLVED` | The run stopped without a verdict on it, or its proof no longer checks. The reason follows. |
| `(unchecked)` after a status | A proof or refutation that Aristotle never checked, because the run ended first. It isn't a verdict yet. |

To summarize or look up results, use `show`, `properties`, `property`, or `download`. None of them starts a run.
Don't send a `continue` only to ask about results: on an idle project, it starts a new run, which can spend hours
checking the proofs again before it answers.

A wait can end before the run does. The run can also fail, be cancelled, or run out of budget. In each case, the CLI
prints the command that picks up where it left off, such as
`aristotle show <project-id> --task <task-id> --wait` or `aristotle continue <project-id> Continue --wait`. Run it.

The [CLI reference](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md) covers the rest: what
goes up, how a project name is looked up, how waits and questions work, a property's files and evidence, and what
each status means.

## Verilog designs

Some designs need their top module, defines, or include folders spelled out. Put them in an `aristotle.f` file at the
root of the folder you submit. For `aristotle ci`, commit it at the root of the repository:

```text
--top soc_top
-I rtl/include
-D DATA_WIDTH=32
--blackbox sram_1p
-F rtl/soc.f
```

Aristotle reads it the way a simulator reads a filelist, so the one your flow already writes usually works as it is.
Every file it names or includes is uploaded.

`--blackbox` tells Aristotle to treat a module, such as a foundry cell, as a black box with unconstrained outputs. A
file that declares only black-boxed modules stays out of the upload, whether the filelists name it or it's a `.v`,
`.sv`, `.vh`, or `.svh` file they don't name. The CLI can't read anything inside a model that's encrypted as a whole,
so remove such a model from the filelists, and from the folder or checkout you upload, yourself.

`aristotle ci setup` doesn't read `aristotle.f`, and neither does an `aristotle review` that checks no Verilog files.
Both upload the whole checkout, so add a `.gitignore` rule for any model that must not be uploaded.

To list every file in the design and where it comes from, run `aristotle submit --dry-run`. The
[Verilog guide](https://github.com/harmonic-ai/aristotle/blob/main/docs/verilog.md) has the details.

## Coding agents

To set up Aristotle in Claude Code, Codex, or Cursor, paste this line into your agent:

```text
Set up Aristotle for me: fetch and follow https://aristotle.harmonic.fun/agent-setup.md
```

Your agent installs the CLI and the Aristotle skill. If you don't have a key yet, it asks whether to log you in
itself, which you approve in your browser, or whether you'd rather run `aristotle login` in your own terminal. It
then checks that it can reach Aristotle, and reports what it set up and anything left for you to do.

The skill, [`skills/aristotle`](https://github.com/harmonic-ai/aristotle/tree/main/skills/aristotle), is an
[Agent Skill](https://agentskills.io) that teaches your agent to use the CLI. With it, an agent submits projects,
answers Aristotle's questions from your code and docs, reads which properties hold, and keeps `aristotle.toml` in
step with the code it changes. To install the skill yourself in Claude Code:

```bash
claude plugin marketplace add harmonic-ai/aristotle
claude plugin install aristotle@harmonic
```

The line you paste points to the
[agent setup guide](https://github.com/harmonic-ai/aristotle/blob/main/docs/agent-setup.md), which has every step,
including for Codex, Cursor, and other agents.

## MCP server

Aristotle runs a hosted MCP server at `https://aristotle.harmonic.fun/mcp`. Connect any MCP client that signs in with
OAuth, such as claude.ai, Claude Desktop, ChatGPT in developer mode, Cursor, Codex, or VS Code, and sign in with your
Aristotle account.

In Claude Code, add the server with this command. Then run `/mcp` in a session, pick `aristotle`, and choose
Authenticate:

```bash
claude mcp add --transport http --scope user aristotle https://aristotle.harmonic.fun/mcp
```

## Scripts and automation

Scripts, CI jobs, and coding agents use the same commands. `--json` prints a command's result to stdout as JSON, and
progress, warnings, and errors go to stderr.

In a script, or with `--json` or `--non-interactive`, a command that has no key exits 5 instead of offering to log
in. `aristotle login --json` prints the link and the code for a person to approve as soon as it starts, then waits
for them. `--prompt-file` reads a long prompt from a file, or from a pipe with `-`.

On `submit`, `continue`, and `show`, `--non-interactive` waits without ever prompting. When Aristotle asks
something, the command prints the questions as JSON and exits 10. Answer them, then keep waiting with the command it
prints on stderr:

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
| 4 | No such project, run, property, or question for your key, or Aristotle refused the action on it. |
| 5 | No API key, Aristotle rejected the key, or a login was denied in the browser or its code expired. |
| 6 | Rate limited: too many runs in progress, or too many requests at once. Or your organization doesn't have the credits to start a run. |
| 7 | The request may have gone through. Check before you send it again. |
| 10 | A `--non-interactive` wait stopped for Aristotle's questions. |
| 130 | Interrupted by Ctrl-C or SIGTERM. A run you were following keeps going, unless `aristotle ci` or `aristotle review` started it. |
| 141 | stdout or stderr closed early, for example by `\| head`, on Linux and macOS. |

The CLI retries server errors, dropped connections, and too many requests at once, and gives up within about two
minutes. A wait survives an outage of up to 10 minutes. The CLI sends a `submit`, `continue`, or answer again only
when Aristotle can't have acted on it.

When a request may have gone through, the CLI exits 7. Check for it (`aristotle list`, `aristotle tasks <project>`,
or `aristotle questions <project> --json`) before you send it again. When the CLI says nothing was done or no run was
started (exit 3, or 6 when rate limited or out of credits), it's safe to run the same command again, once an
organization admin has bought credits if it was refused for them.

The [CLI reference](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md) has every `--json`
shape and field, each exit code in full, and a script that answers questions.

## Run it in CI

`aristotle ci` checks the properties listed in `aristotle.toml`, and reports each one the way your CI already reports
tests: as a JUnit report, and on GitHub as annotations and a job summary. It detects GitHub Actions, GitLab CI,
Jenkins, Buildkite, CircleCI, and Azure Pipelines on its own. It verifies Verilog and C sources.

Each property has an id, and is either stated in plain language or names an assertion or a C function in your code:

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

The [CI guide](https://github.com/harmonic-ai/aristotle/blob/main/docs/aristotle-in-ci.md) has the manifest format
and recipes for GitLab CI, Jenkins, and other CI systems. It also covers what fails the build, `aristotle review`,
time and cost, and what gets uploaded.

## Python library

The CLI is built on the `aristotlelib` package, which your programs can use directly. It finds the key the same way
the CLI does, or you can set one with `aristotlelib.set_api_key()`:

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

The library uploads a folder the way `submit --project-dir` does, but it doesn't read `aristotle.f`. It neither
leaves out black-boxed files nor points Aristotle at the filelist, so send such a design with `aristotle submit`.
The library logs to the `aristotle` logger, which prints nothing until your program configures logging.

To upload selected files to an idle project, pass them to `Project.ask()` with the root they share, as
`continue --project-dir` takes it (by default, the current directory):

```python
await project.ask("Use these proofs", files=["downloaded/Proofs/Foo.lean"], project_dir="downloaded")
```

Files outside the root, including through symlinks, paths containing `..`, and duplicate upload paths are refused.

## Proxies and private networks

The CLI honors `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY`. Behind a TLS-inspecting proxy, point
`ARISTOTLE_CA_BUNDLE` at your company's CA bundle. The only host the CLI needs is `aristotle.harmonic.fun`, on port
443. When a proxy or firewall refuses a request, the CLI says the refusal may come from it, and what to do next.
[Proxies and private networks](https://github.com/harmonic-ai/aristotle/blob/main/docs/cli-reference.md#proxies-and-private-networks)
in the CLI reference lists the messages, what `--verbose` adds, the other variables the CLI reads, and what each
request's `User-Agent` header carries.

## Help

`aristotle --help` lists the commands, and each command has its own `--help`. For guides and the full
documentation, go to [aristotle.harmonic.fun](https://aristotle.harmonic.fun).

## Glossary

- **Project:** what you submit: your files and the conversation about them.
- **Run:** one piece of work on a project. `submit` starts one, and so does a `continue` to an idle project. The CLI
  calls a run a *task*.
- **Property:** a claim Aristotle proves or refutes. In JSON, a property is a *declaration*.
- **Verdict:** proved or refuted. A property without one is in progress, unresolved, or unchecked.
- **Manifest:** `aristotle.toml`, which lists the properties `aristotle ci` checks.

## License

aristotlelib is licensed under the terms in the `LICENSE` file that comes with it, which refer to the
[Aristotle API Terms of Use](https://aristotle.harmonic.fun/terms). It includes a copy of tomli, under the MIT
license.
