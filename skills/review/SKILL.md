---
name: review
description: Read-only code review of your changes by Antigravity (agy)
argument-hint: "[--base <ref>] [--background] [--model <id>] [--effort <level>] [focus]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py *)
---

Run a read-only agy review. agy works in a throwaway git worktree, so your files are never modified.

User input: $ARGUMENTS

Run with the Bash tool, timeout 600000 ms:
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py review $ARGUMENTS`

Without `--base` it reviews uncommitted changes; with `--base main` it reviews the whole branch. Present agy's findings verbatim. Do not fix anything unless the user asks.
