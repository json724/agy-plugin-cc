---
name: rescue
description: Delegate a task (bug, investigation, implementation) to Antigravity (agy)
argument-hint: "[--background] [--read-only] [--resume] [--allow-shell] [--model <id>] [--effort low|medium|high|max] [--add-dir <dir>]... <task>"
disable-model-invocation: true
allowed-tools:
  - Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py *)
  - Bash(git diff *)
  - Bash(git status *)
---

Delegate this task to the Antigravity CLI through the companion script.

User input: $ARGUMENTS

1. Split the input into flags (the ones listed in the argument hint) and the task text. If the task text is vague, rewrite it into a self-contained instruction for an agent that has not seen this conversation: name the files, the expected behavior, and the constraints you already know. Do not add new scope.
2. Run with the Bash tool, timeout 600000 ms, flags first and the task as ONE single-quoted argument:
   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/agy_companion.py task [flags] '<task>'`
3. Relay agy's report to the user without rewriting it. If the output contains an ERROR, a WARNING about denied tools, or a WARNING — EVIDENCE CHECK, say so in the first line. Names listed by the evidence check do not appear in the files agy cited: never present them as fact; offer to read the cited lines to confirm them.
4. If agy edited files (not --read-only), run `git status --short` and `git diff --stat` and list what changed. Do not re-implement or deeply re-review agy's work unless the user asks; the point is to save Claude tokens.
5. Never call `agy` directly and never add `--dangerously-skip-permissions`. If agy was denied a tool, show the warning and its suggested fix to the user and let them decide; do not work around it.
6. If the task needs files outside this repo, pass `--add-dir <dir>` for each such directory (agy can read there; headless agy denies writes there).
7. If the job is still running when the wait ends, tell the user the job id and that /agy:result will fetch it.
