---
name: adversarial-review
description: Have Antigravity (agy) challenge the design, assumptions and tradeoffs of your changes
argument-hint: "[--base <ref>] [--background] [--model <id>] [--effort <level>] [focus]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py *)
---

Run a read-only adversarial review with agy. It works in a throwaway git worktree, so your files are never modified.

User input: $ARGUMENTS

Run with the Bash tool, timeout 600000 ms:
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py adversarial-review $ARGUMENTS`

Present agy's critique verbatim (with any WARNING — EVIDENCE CHECK line first), then add one line with your own take on whether its strongest point holds, citing path:line. Do not change code unless the user asks.
