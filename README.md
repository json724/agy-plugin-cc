# agy plugin for Claude Code

Delegate work from Claude Code to the [Antigravity CLI](https://antigravity.google) (`agy`) so it runs on your Antigravity quota instead of Claude tokens. Modeled on OpenAI's `codex-plugin-cc`.

## Commands

| Command | What it does |
| --- | --- |
| `/agy:rescue [flags] <task>` | Hands a bug, investigation or implementation to agy. Edits your repo unless `--read-only`. |
| `/agy:review [--base <ref>] [focus]` | Read-only review of uncommitted changes, or of the branch against `<ref>`. |
| `/agy:adversarial-review [--base <ref>] [focus]` | Read-only critique of design, assumptions and tradeoffs. |
| `/agy:status [job]` | Lists jobs for this repo. |
| `/agy:result [job]` | Prints the output of the latest (or given) finished job. |
| `/agy:cancel [job]` | Stops the running (or given) job. |
| `/agy:setup` | Checks that `agy` is installed and signed in, and lists models. |

The `agy-rescue` subagent lets Claude delegate on its own when you tell it to send a kind of work to agy.

Shared flags: `--background` (return a job id immediately), `--model <id>` (see `agy models`), `--effort low|medium|high|max`, `--wait <seconds>` (foreground limit, default 540). Rescue-only flags: `--read-only`, `--resume` (continue the last agy task conversation in this repo), `--allow-shell`.

Set `AGY_COMPANION_MODEL` to change the default model.

## Install

```bash
claude plugin marketplace add json724/agy-plugin-cc
claude plugin install agy@agy-plugin-cc
```

Or from inside Claude Code: `/plugin marketplace add json724/agy-plugin-cc`, then `/plugin install agy@agy-plugin-cc`.

Requires `agy` on PATH, signed in, and `python3` (standard library only).

### Updates

The plugin pins an explicit `version`, so you only receive a release when that version changes. Auto-update is off by default for third-party marketplaces. Either turn it on in `/plugin` → **Marketplaces** → `agy-plugin-cc` → **Enable auto-update**, or update by hand:

```bash
claude plugin update agy@agy-plugin-cc
```

Each release is tagged `agy--v<version>`.

## How it behaves, and why

These are properties of `agy -p` 1.2.x that the plugin was built around. All of them were observed while building it.

- **Shell commands are denied in headless mode, and a single denial ends agy's turn.** The prompt tells agy to use its built-in file tools (`view_file`, `grep_search`, `replace_file_content`, …) instead. Consequence: by default agy cannot run your tests; it lists the commands you should run.
  To let it run specific commands, allow-list them in `~/.gemini/antigravity-cli/settings.json` and pass `--allow-shell`:
  ```json
  { "permissions": { "allow": ["command(uv run pytest)", "command(git diff)"] } }
  ```
  `--allow-shell` only changes the instructions agy receives. It never passes `--dangerously-skip-permissions`.
- **File writes are not blocked in headless mode, not even with `--mode plan`.** Reviews and `--read-only` tasks therefore run in a detached git worktree under `~/.cache/agy-companion/worktrees`, with your uncommitted changes and untracked files copied in. The worktree is deleted when the job ends.
- **`status: "SUCCESS"` is not trustworthy on its own.** A job counts as failed when agy returns an empty response. Denied tools are surfaced as a `WARNING` line.
- **Large context goes through a file.** A single argv entry is capped at 128 KiB on Linux and `agy -p` does not read the prompt from stdin, so the review diff is written to `.agy-context/CONTEXT.md` inside the worktree and agy reads it from there.

Job records and logs live in `$CLAUDE_PLUGIN_DATA` (falls back to `~/.claude/agy-companion`).

## Risks and limits

- **It depends on undocumented agy behavior.** Everything under "How it behaves" was observed on agy 1.2.12, not taken from a spec. If a later agy changes the JSON output or the headless permission rules, the first symptom is jobs ending as `failed`. Run `/agy:setup` to check.
- **`/agy:rescue` edits your working tree directly and without approval.** Headless agy does not ask before writing files. Commit or stash first if you want an easy way back, or use `--read-only`.
- **By default agy cannot run tests.** It lists the commands for you to run instead. See `--allow-shell` above.
- **Each call has a fixed start-up cost.** Around 20 s of wall time before the model starts, measured on one WSL2 machine.
- **It spends your Antigravity quota, not zero tokens.** Each job used roughly 50k–70k agy tokens on small test repositories. Real repositories will likely use more; that was not measured.
- **Output quality is agy's, not Claude's.** Claude relays the report; it does not re-verify it unless you ask.

## Not included (yet)

- The review gate from `codex-plugin-cc` (a Stop hook that makes the external agent review every Claude turn). It would spend agy quota on every turn, so it is left out until there is a clear need.

## License

MIT. See [LICENSE](LICENSE).
