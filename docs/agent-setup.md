# Set up Aristotle for your user

This guide is for a coding agent, such as Claude Code, Codex or Cursor, whose user asked it to set up Aristotle. Run
each command yourself, except the steps marked **User step**, which only the user can do. Every command is safe to run
again.

At a User step, ask the user, then carry on with the next step, and list every User step still open in the report at
the end. The steps marked **User step, then wait** are the exceptions, because the steps after them need what they set
up: ask, and wait for the user's reply. If the CLI still isn't 3.x after that, skip the version comparison in step 2
and the check in step 3, and report the install and the login as open.

When you finish, the user has:

- the `aristotle` command-line tool, from the Python package `aristotlelib`;
- the Aristotle skill, which teaches you to drive the CLI: the `aristotle@harmonic` plugin in Claude Code and Codex, or
  a copy of its folder for other agents;
- an API key that the CLI has stored.

## 1. Install the CLI

```bash
aristotle --version
```

If it prints `aristotlelib 3.` followed by the rest of the version, the CLI is installed: go on to step 2. Otherwise,
install it. The CLI needs Python 3.10 or later and [uv](https://docs.astral.sh/uv/). If `uv --version` fails,
**User step, then wait:** the user installs uv as its documentation says. Its installer can change their shell's
startup files, so leave that to them. That change reaches only new shells: if `uv` still isn't found afterwards, run
it as `~/.local/bin/uv`, where the installer puts it, here and in the full path below.

```bash
uv tool install "aristotlelib>=3"
"$(uv tool dir --bin)/aristotle" --version
```

- aristotlelib 3.0.0 isn't on PyPI yet, so that install finds no version to install. Harmonic gives each beta user an
  exact release candidate, as a wheel file or on a private package index. **User step, then wait:** ask the user to
  install it as Harmonic told them: `uv tool install --force ./aristotlelib-<version>-py3-none-any.whl` from the
  folder that holds the file, or `uv tool install --force --index <their index URL> "aristotlelib==<version>"`. Don't
  ask for the index URL in the conversation, since it can carry a credential.
- If uv says an executable named `aristotle` already exists, another program has that name. Ask the user before
  replacing it.
- If the bare `aristotle --version` still doesn't print `aristotlelib 3.` after the install, but
  `"$(uv tool dir --bin)/aristotle" --version` does, use that full path for the rest of this guide, the login
  command included. That covers a bare command that isn't found, another program named `aristotle`,
  and an older aristotlelib installed outside uv that comes first on `PATH`. Leave the user's shell startup files
  alone: in the report, ask the user to run `uv tool update-shell` and open a new terminal, or to fix `PATH`.

## 2. Install the skill

The harmonic-ai/aristotle repository isn't public yet, so these commands work only for GitHub accounts that have been
granted access to it. They fetch it over HTTPS. **User step,** for an account that reaches GitHub only over SSH: the
user runs `git config --global url."git@github.com:harmonic-ai/".insteadOf https://github.com/harmonic-ai/`, which
changes their global git configuration.

**Claude Code:**

```bash
claude plugin marketplace add harmonic-ai/aristotle
claude plugin install aristotle@harmonic
claude plugin list
```

`claude plugin list` shows `aristotle@harmonic` as enabled, with its version. If it's disabled, run
`claude plugin enable aristotle@harmonic`. If its version differs from the one `aristotle --version` prints, bring the
older side up to date:

- an older skill: `claude plugin marketplace update harmonic`, then `claude plugin update aristotle@harmonic`;
- an older CLI: `uv tool install "aristotlelib>=<the skill's version>"`. aristotlelib 3.0.0 isn't on PyPI yet, so
  until then the user installs the newer release candidate Harmonic gave them, as in step 1.

**User step:** run `/reload-plugins`, so that the skill loads in this session. After `claude plugin update`, Claude Code
needs a restart instead, which ends this session: list it in the report.

**Codex:**

```bash
codex plugin marketplace add harmonic-ai/aristotle
codex plugin add aristotle@harmonic
codex plugin list
```

`codex plugin list` shows `aristotle@harmonic` as installed and enabled, with its version. If the skill is older than
the CLI, run `codex plugin marketplace upgrade harmonic`, then `codex plugin add aristotle@harmonic` again. If the CLI
is older, update it as for Claude Code above.

Codex's sandbox blocks the network without asking, so `aristotle` retries and then fails, which looks like a hang.
**User step:** set `network_access = true` under `[sandbox_workspace_write]` in `~/.codex/config.toml`, and for
`codex exec` also `sandbox_mode = "workspace-write"` at the top of that file, above the table.

**Cursor, and other agents that read skills from `~/.agents/skills`,** need a copy of the skill's folder from the
release:

```bash
src=$(mktemp -d) && git clone --depth 1 --branch v3.0.0rc13 https://github.com/harmonic-ai/aristotle.git "$src" &&
  mkdir -p ~/.agents/skills && rm -rf ~/.agents/skills/aristotle &&
  cp -R "$src/skills/aristotle" ~/.agents/skills/
```

In PowerShell on Windows:

```powershell
$src = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
git clone --depth 1 --branch v3.0.0rc13 https://github.com/harmonic-ai/aristotle.git $src
if (Test-Path "$src\skills\aristotle") {
  New-Item -ItemType Directory -Force "$HOME\.agents\skills" | Out-Null
  Remove-Item -Recurse -Force "$HOME\.agents\skills\aristotle" -ErrorAction SilentlyContinue
  Copy-Item -Recurse "$src\skills\aristotle" "$HOME\.agents\skills\"
}
```

The copy matches the release those commands name. If `aristotle --version` prints an older version, update the CLI as
for Claude Code above. An agent that keeps its skills somewhere else takes the same folder there.

## 3. Check access, and log in

```bash
aristotle list --limit 1 --json
```

- **Exit 0, with a JSON object,** even one with no projects in it: access works.
- **An error that says `Error: request refused` or `Error: request blocked by a proxy`,** whatever the exit code:
  a proxy or a firewall may have refused the request before Aristotle saw it. If your tool reports that its own
  sandbox blocked the request, as Claude Code does, allow the host it names the way it says, or ask the user to, then
  run the check again. Otherwise, report the error and its `Hint:` line. Don't log in, and don't ask the user to check
  their account.
- **Exit 5:** there is no key, or Aristotle rejected it, or it can't be sent.
  - If the error says "The API key in ARISTOTLE_API_KEY is not valid", or that the key in the ARISTOTLE_API_KEY
    environment variable is blank or contains a space, a control character or a non-ASCII character, the user's
    environment sets a key that Aristotle rejects or that can't be sent. **User step:** the user unsets that variable,
    or replaces its value where their shell sets it. Never print its value. A missing key mentions `ARISTOTLE_API_KEY`
    too, as one way to set a key: that case is the next one.
  - Otherwise the user has to log in. **User step, then wait:** unless your shell is Windows PowerShell, which the
    command below isn't written for, ask the user which way they want before you start a login: you log in for them
    here, which needs them to see your messages as you work, or they run `aristotle login`, or the full path from
    step 1, in their own terminal. Ask with your tool for asking the user, such as Claude Code's AskUserQuestion, or
    else end your turn with the question, in your report (step 4), and start a login only when they answer that they
    want one. If you run headless, as `claude -p` and `codex exec` do, the session ends with that question, as it
    should: the user would see a login's code only once the session ended, which stops the login.
  - To log in for the user: they approve this machine in their browser, and the key goes straight to the CLI. Make a
    private directory with `mktemp -d`, outside the user's project, and write the path it prints in place of
    `<out>`. Start the login so that it keeps running while you talk to the user: in Claude Code, run this without
    the trailing `&`, with `run_in_background`; elsewhere, as it stands. Use the full path from step 1 in place of
    `aristotle` if you're using it.

    ```bash
    { aristotle login --json > <out>/login.json 2> <out>/login.log; echo "exit $?" > <out>/login.exit; } > /dev/null 2>&1 &
    ```

    Within a few seconds `<out>/login.json` holds `{"verification_uri", "user_code", "expires_in"}`.
    **User step, then wait:** tell the user at once, in a message, to open `verification_uri` where they're signed in
    to Aristotle, type `user_code`, check that the page names this machine (the name `hostname` prints), and
    approve. The code expires after `expires_in` seconds, 10 minutes. Then wait for `<out>/login.exit`, checking every
    20 seconds or so:
    - `exit 0`: the key is stored. Run the check once more.
    - `exit 5`: the user denied the login or the code expired, as `<out>/login.log` says. Ask before starting
      another.
    - Any other code: report what `<out>/login.log` says, where to revoke a key the login may have made included.
    - No file 11 minutes after the start: the login was stopped.
  - **User step, then wait:** ask the user to run `aristotle login` in their own terminal instead, or the full path
    from step 1, when they choose that, and in these cases. Then run the check once more, and if it still exits 5,
    report that rather than asking again.
    - Your shell is Windows PowerShell, which the command above isn't written for.
    - `<out>/login.log` says `Create an API key at`: the server takes only a pasted key. In their terminal,
      `aristotle login` opens [the API keys page](https://aristotle.harmonic.fun/keys), where they create a key, and
      reads it without echoing it.
    - `<out>/login.log` says `unknown option --json`: the CLI is older than this guide.
    - The login was stopped.
- **Exit 3:** Aristotle couldn't be reached. In Codex, that's the network setting in step 2. Elsewhere, report the
  error, since a proxy or a firewall may be in the way.
- **Exit 4:** Aristotle refused the account's request. Ask the user to check their account.
- **Exit 6:** too many requests. Wait a minute, then run the check again.
- **Anything else:** report the error.

Never ask for the key in the conversation, and never print, log or commit it. In CI and other scripts, the key comes
from the `ARISTOTLE_API_KEY` environment variable instead. If it isn't set there, stop and say so.

## 4. Report

Print one line for each part. Follow every line that isn't ✓ with the next action:

```text
✓ Aristotle CLI: aristotlelib 3.0.0
✓ Aristotle skill: aristotle@harmonic 3.0.0
⚠ Not logged in: run `aristotle login` in a terminal
```

✓ means checked, ⚠ means the user has something to do, and ✗ means a step failed. List every User step that is still
open, such as `/reload-plugins` or `uv tool update-shell`. Then tell the user what they can ask you for, such as "prove
that this FIFO never overflows".
