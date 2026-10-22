# Windows PowerShell

The skill's commands are for POSIX shells: bash, zsh, Git Bash, WSL. In PowerShell, `$?` is only true or false, and
there is no `mktemp` and no `timeout -s INT`. Use these instead:

- Make the private directory with
  `(New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName()))).FullName`,
  and use the path it prints as `<out>`.
- Capture the JSON and the exit code like this:
  `aristotle show <project-id> --json | Out-File -Encoding ascii "<out>\run.json"; "exit $LASTEXITCODE"`.
  `Out-File` empties the file even when the command prints nothing, as it does on an error. The JSON is ASCII, so
  nothing is lost.
- Wait for a run the way [Long waits](../SKILL.md#long-waits) describes for a shell without `timeout`, with
  `Start-Sleep -Seconds 100` in place of `sleep 100`, and save the archive with
  `aristotle download <project-id> --destination "<out>\result.tar.gz"`.
- Never stop a running `aristotle ci` with `Stop-Process`: that kills it without cancelling its run, which goes on
  with no report.
- To log in, ask the user to run `aristotle login` in their own terminal.
