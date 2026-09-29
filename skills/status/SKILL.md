---
name: status
description: List agy (Antigravity) jobs for this repo, or show one job. Use to check on an agy job started with --background.
argument-hint: "[job-id]"
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py *)
---

Run with the Bash tool: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py status $ARGUMENTS`

Show the output to the user as-is.
