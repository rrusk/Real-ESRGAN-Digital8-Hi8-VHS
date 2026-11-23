# Bash script conventions

These conventions make repository tools behave predictably without requiring
the user to learn a different command-line style for every script.

## Structure

- Put executable work in `main()` and finish with a guarded call followed
  immediately by `exit $?`:

  ```bash
  if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
      main "$@"
      exit $?
  fi
  ```

  The function makes Bash parse the body as one unit before execution, and the
  immediate exit prevents it from resuming at a stale file offset after a
  successful long-running command if the script was edited in place.
- Keep reusable functions focused, quote pathnames, use arrays for commands,
  and pass `--` before pathname operands when the called tool supports it.
- Use `set -euo pipefail`, but handle expected nonzero statuses explicitly.
- Git is the changelog. Put enduring explanations beside the relevant code,
  option, warning, or workflow documentation instead of keeping revision logs.
- **Comments explain current behavior and constraints.** Describe why a value or
  operation is needed, without narrating unpublished development history.
  For example, explain how a tolerance accounts for codec padding and frame
  duration. Keep historical provenance in Git.

  ```bash
  # Allow one AAC block of encoder padding plus one picture of timing drift.
  # Keep these budgets separate so padding cannot consume the drift allowance.
  ```

  Explain deliberate exclusions when they help a reader understand a constraint:
  for example, "Do not concatenate per-clip AAC tracks: each adds its own encoder
  priming and padding; encode one track from the concatenated source audio."

## Command line

- Accept `-h` and `--help`; print help to stdout and exit 0 before dependency
  checks or other work.
- If a script reports a maintained version, accept `-V` and `--version`.
- Put options before positional arguments in examples. Parsers should accept
  options in either position where doing so is unambiguous.
- Use consistent names: `--output`, `--output-dir`, `--start`, `--end`,
  `--duration`, `--dry-run`, `--force`, `--quiet`, and `--verbose`.
- Prefer positional arguments for primary inputs. Use named input options only
  when several roles would otherwise be ambiguous.
- Use exit 2 for command-line errors and print
  `Try 'script --help' for more information.` Do not print the full help text.
- Use exit 1 for processing failures and 0 for success.
- Use exit 3 when the user declines a confirmation prompt. It is neither
  success nor failure, and a driver keys on it: a declined run is removed,
  a failed one keeps its log.
- Do not retain obsolete aliases indefinitely. When an interface must change,
  update known callers and report the replacement clearly.

## Help layout

Use these sections as applicable, in this order:

1. `Usage`
2. `Description`
3. `Arguments`
4. `Options`
5. `Controls` for interactive tools
6. `Examples`
7. `Dependencies`

Keep workflow explanations in the relevant README rather than making `--help`
difficult to scan.

## Runtime behavior

- Check required dependencies before lengthy work. Name the missing command;
  do not silently substitute reduced functionality.
- Report detected and effective media settings before a long encode, including
  frame rate, scan/deinterlace behavior, dimensions, SAR/DAR, audio handling,
  output path, and resume position when relevant.
- Use `[INFO]`, `[WARNING]`, and `[ERROR]` consistently. Warnings and errors go
  to stderr. Summaries must identify items needing review, not only count them.
- Do not overwrite output without explicit authorization. Use `--force` where
  replacement is a legitimate operation and `--dry-run` where a useful plan can
  be produced without side effects.
- Prompt only with a controlling terminal. Noninteractive execution must either
  have an explicit option such as `--yes` or fail with actionable guidance.
- Use private `mktemp` work directories and traps that preserve the original
  exit status. Preserve useful diagnostics on failure when appropriate.
- A `mktemp` file that becomes a real output needs its mode set before the
  rename, because `mktemp` creates it 0600. Restore a replaced file's mode
  with `chmod --reference`, and give a newly created output `chmod 0644`.
  Private scratch files and state no other user reads may stay 0600.
- Long interactive workflows should save enough state to resume safely and
  state the precedence of explicit options, saved state, detected values, and
  defaults.

## Media and interactive terminology

- Identify views as left and right and report corresponding timestamps. Avoid
  unexplained relative terms such as “leading” and “lagging.”
- Distinguish stored dimensions, display dimensions, SAR, DAR, frame rate,
  field rate, container timestamps, and stream PTS.
- Accept seconds, `MM:SS`, and `HH:MM:SS.mmm` consistently where times are used,
  and state which timeline they reference.
- mpv workflow scripts disable mpv defaults and the user's input configuration,
  then implement the shared contract documented by `mpv-keybindings.md` in the
  video-preservation-tools repository. AI-side callers should discover those
  installed tools through `PATH`, not assume that repository is nested here.

## Minimum checks

For each changed script, test:

- `bash -n`
- `--help`, and `--version` where supported
- an unknown option and a missing required argument
- pathnames containing spaces, `&`, Unicode, and leading hyphens where relevant
- existing-output collision and interruption/cleanup paths where relevant
- ShellCheck, documenting intentional exceptions

Search repository callers, documentation, generated resume commands, symlinks,
and staged copies before changing an established interface.

## Python command-line tools

Apply the same conventions to Python tools: help layout, option names,
positional arguments, exit statuses, dependency errors, logging, overwrite
safety, temporary files, interruption behavior and pathname handling.
