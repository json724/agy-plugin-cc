---
description: Check that the agy CLI is installed and signed in
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py:*), Bash(agy models:*)
---

Run with the Bash tool, timeout 240000 ms: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py setup`

Then run `agy models` and show the available model ids, so the user can pass one with --model or set AGY_COMPANION_MODEL as the default. If setup failed, tell the user to run `! agy` once to sign in interactively.
