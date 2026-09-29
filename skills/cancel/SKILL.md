---
name: cancel
description: Cancel the running (or a given) agy job
argument-hint: "[job-id]"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py *)
---

Run with the Bash tool: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py cancel $ARGUMENTS`

Show the output to the user as-is.
