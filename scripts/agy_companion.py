#!/usr/bin/env python3
"""Companion runtime for the agy Claude Code plugin.

Delegates work to the Antigravity CLI (`agy`) in headless print mode and
tracks each delegation as a job, so long tasks can run in the background.

Facts about `agy -p` (v1.2.x) this script is built around:
  * `--output-format json` sometimes emits raw newlines inside `response`,
    so the payload must be decoded with strict=False.
  * Shell commands are auto-denied in headless mode unless allow-listed in
    ~/.gemini/antigravity-cli/settings.json; `status` stays "SUCCESS" and the
    denial only shows up in `denied_actions`.
  * File writes are NOT blocked in headless mode, not even with `--mode plan`.
    Read-only jobs therefore run inside a throwaway git worktree.
  * The prompt cannot be piped through stdin in text mode, and a single argv
    entry is capped at 128 KiB on Linux, so large context goes into a file the
    agent reads with its native view_file tool.
  * `--output-format stream-json` emits one event per tool step (tool name and
    parameters), which gives an objective record of what agy actually read.
  * agy sometimes fills gaps with plausible-looking identifiers (dict keys,
    parameter names) that do not exist in the code. Every response is checked:
    backticked identifiers must appear, as whole words, in the files it cites.
  * Reading outside the workspace is denied in headless mode ("read_file"),
    including through a symlink whose target is outside. `--add-dir` makes a
    directory readable; writing into it is still denied ("write_file").
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

DATA_DIR = Path(
    os.environ.get("CLAUDE_PLUGIN_DATA")
    or Path.home() / ".claude" / "agy-companion"
)
JOBS_DIR = DATA_DIR / "jobs"
# Worktrees live under $HOME rather than /tmp so they survive tmp cleaners
# while a long job is running.
WORKTREES_DIR = Path(
    os.environ.get("AGY_COMPANION_WORKTREES")
    or Path.home() / ".cache" / "agy-companion" / "worktrees"
)
CONTEXT_DIRNAME = ".agy-context"
DEFAULT_WAIT_SECONDS = 540  # stays under Claude Code's 10-minute Bash cap
TERMINAL = {"completed", "failed", "cancelled"}

READ_ONLY_RULES = """\
Operating rules for this session:
- You are running inside a disposable copy of the repository. Treat it as READ-ONLY: do not create, edit or delete files.
- Do NOT use run_command or any terminal tool. In this headless session shell commands are denied, and a single denial aborts your whole turn. Read code with view_file, grep_search, list_dir and find_by_name.
- Report back in plain Markdown. Cite code as path:line."""

WRITE_RULES = """\
Operating rules for this session:
- You are working directly in the user's repository. Edit files as needed to complete the task.
- Do NOT use run_command or any terminal tool. In this headless session shell commands are denied, and a single denial aborts your whole turn. Read with view_file, grep_search, list_dir, find_by_name; edit with replace_file_content, multi_replace_file_content, write_to_file.
- You cannot run tests. Instead, list the exact commands the user should run to verify your change.
- Finish with a short report: what you changed (path:line), how you checked it, and what remains unverified."""

WRITE_RULES_WITH_SHELL = """\
Operating rules for this session:
- You are working directly in the user's repository. Edit files as needed to complete the task.
- Only the shell commands the user allow-listed will run; any other command is denied and a denial aborts your whole turn. Prefer your file tools (view_file, grep_search, replace_file_content, write_to_file) and use run_command only for build/test commands you were told are allowed.
- Finish with a short report: what you changed (path:line), what you verified and how, and what remains unverified."""

EVIDENCE_RULES = """\
Evidence rules (the user will machine-check your answer against the code):
- Every concrete claim about code (a name, dict key, parameter, type, default, enum, constraint, precondition, return shape) must come from lines you opened in this session. Cite them as path:line and copy identifiers character-for-character from the code, in backticks.
- Docstrings, comments and naming conventions are not the code. If a docstring and the code disagree, report what the code does and point out the mismatch.
- Separate constraints enforced by the signature or schema (type annotations, Field(...), enums) from checks done at runtime inside the function body. Say which one each constraint is.
- If you did not read the lines that establish a claim, read them. If you still cannot confirm it, mark it "(inferred)". Never fill a gap with a name that merely sounds right.
- A shorter answer with only verified items is better than a complete-looking answer with guesses."""

REVIEW_PROMPT = """\
You are a senior code reviewer. Review the change described in {context_file}.

Start by reading {context_file} with view_file. Then read the surrounding code in the repository to judge the change in context.

Focus on defects that matter: correctness bugs, broken edge cases, data loss, security issues, concurrency problems, and regressions against existing callers. Ignore pure style nits.

For every finding give: severity (critical/high/medium/low), path:line, what is wrong, a concrete failure scenario, and the suggested fix. Order findings by severity. If you find nothing significant, say so plainly and state what you checked.
{focus}
{rules}"""

ADVERSARIAL_PROMPT = """\
You are an adversarial design reviewer. Your job is to find the strongest reasons this change is the wrong approach, not to polish it.

Start by reading {context_file} with view_file, then read the surrounding code in the repository.

Challenge: the assumptions the change relies on, the chosen design versus simpler alternatives, failure modes under load, bad input, or partial failure, hidden coupling, and anything that will be expensive to undo later. For each point give the evidence (path:line), why it matters, and what you would do instead. End with a verdict: ship / ship with changes / rethink.
{focus}
{rules}"""


# --------------------------------------------------------------------------
# small helpers


def die(msg: str, code: int = 1) -> None:
    print(f"agy-companion: {msg}", file=sys.stderr)
    sys.exit(code)


def now() -> float:
    return time.time()


def fmt_ts(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def git(*args: str, cwd: str | Path, check: bool = True, input: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, input=input, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout.decode(errors="replace")


def repo_root(cwd: str) -> str | None:
    try:
        return git("rev-parse", "--show-toplevel", cwd=cwd).strip() or None
    except RuntimeError:
        return None


def workspace_key(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def require_agy() -> str:
    exe = shutil.which("agy")
    if not exe:
        die("the `agy` CLI is not on PATH. Install Antigravity CLI and sign in (run `agy` once interactively).")
    return exe


# --------------------------------------------------------------------------
# job store


def job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def save_job(job: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = job_path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2))
    tmp.replace(job_path(job["id"]))


def load_job(job_id: str) -> dict:
    p = job_path(job_id)
    if not p.exists():
        matches = sorted(JOBS_DIR.glob(f"{job_id}*.json")) if JOBS_DIR.exists() else []
        if len(matches) != 1:
            die(f"no job matches {job_id!r}")
        p = matches[0]
    return json.loads(p.read_text())


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def refresh(job: dict) -> dict:
    """Mark a job failed if its runner died without recording a result."""
    if job["status"] == "queued" and now() - job["created_at"] < 60:
        return job  # runner not started yet
    if job["status"] in ("queued", "running") and not pid_alive(job.get("runner_pid")):
        job = json.loads(job_path(job["id"]).read_text())  # re-read: may have just finished
        if job["status"] in ("queued", "running"):
            job["status"] = "failed"
            job["error"] = "runner process exited without recording a result"
            job["finished_at"] = now()
            save_job(job)
    return job


def jobs_for(workspace: str) -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    out = []
    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if job.get("workspace") == workspace:
            out.append(refresh(job))
    return sorted(out, key=lambda j: j["created_at"], reverse=True)


# --------------------------------------------------------------------------
# read-only sandbox: a detached worktree carrying the user's uncommitted work


def make_worktree(root: str, job_id: str) -> Path:
    wt = WORKTREES_DIR / workspace_key(root) / job_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", str(wt), "HEAD", cwd=root)
    patch = subprocess.run(
        ["git", "diff", "HEAD", "--binary"], cwd=root, capture_output=True, check=True
    ).stdout
    if patch.strip():
        git("apply", "--binary", "--whitespace=nowarn", "-", cwd=wt, input=patch)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", cwd=root)
    for rel in filter(None, untracked.split("\0")):
        src, dst = Path(root) / rel, wt / rel
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return wt


def remove_worktree(root: str, wt: str) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", wt], cwd=root, capture_output=True
    )
    shutil.rmtree(wt, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)


def write_context(wt: Path, text: str) -> str:
    d = wt / CONTEXT_DIRNAME
    d.mkdir(exist_ok=True)
    (d / "CONTEXT.md").write_text(text)
    return f"{CONTEXT_DIRNAME}/CONTEXT.md"


def review_context(root: str, base: str | None) -> tuple[str, bool]:
    """Build the Markdown the reviewer reads. Returns (text, has_changes)."""
    parts = ["# Review context\n"]
    if base:
        merge_base = git("merge-base", base, "HEAD", cwd=root).strip()
        log = git("log", "--oneline", f"{merge_base}..HEAD", cwd=root)
        diff = git("diff", merge_base, cwd=root)  # committed branch work + uncommitted
        parts.append(f"Scope: all changes on this branch relative to `{base}` (merge-base {merge_base[:10]}), including uncommitted edits.\n")
        parts.append(f"## Commits\n```\n{log or '(none)'}\n```\n")
    else:
        diff = git("diff", "HEAD", cwd=root)
        parts.append("Scope: uncommitted changes (staged and unstaged) against HEAD.\n")
    status = git("status", "--short", cwd=root)
    parts.append(f"## git status --short\n```\n{status or '(clean)'}\n```\n")
    untracked = [
        f for f in git("ls-files", "--others", "--exclude-standard", "-z", cwd=root).split("\0") if f
    ]
    if untracked:
        parts.append(
            "## New untracked files (read them directly; they are not in the diff)\n"
            + "\n".join(f"- {f}" for f in untracked)
            + "\n"
        )
    parts.append(f"## Diff\n```diff\n{diff or '(empty)'}\n```\n")
    return "\n".join(parts), bool(diff.strip() or untracked)


# --------------------------------------------------------------------------
# agy invocation


def iter_json_objects(stdout: str):
    """Yield every top-level JSON object in agy output (json or stream-json)."""
    decoder = json.JSONDecoder(strict=False)
    pos = stdout.find("{")
    while pos != -1:
        try:
            obj, end = decoder.raw_decode(stdout, pos)
        except json.JSONDecodeError:
            pos = stdout.find("{", pos + 1)
            continue
        if isinstance(obj, dict):
            yield obj
        pos = stdout.find("{", end)


def parse_agy_output(stdout: str) -> dict | None:
    for obj in iter_json_objects(stdout):
        if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            return obj["result"]
        if "conversation_id" in obj and "status" in obj:
            return obj
    return None


def tool_trace(stdout: str) -> list[dict]:
    """Finished tool steps from a stream-json run: name, parameters, and any error.

    A step agy was not allowed to run ends in state ERROR ("permission check
    failed"); it is kept so the trace shows what was denied.
    """
    steps = []
    for obj in iter_json_objects(stdout):
        step = obj.get("step_update") or {}
        if step.get("step_type") == "tool" and step.get("state") in ("DONE", "ERROR"):
            info = step.get("tool_info") or {}
            error = (info.get("error") or {}).get("message") if step.get("state") == "ERROR" else None
            steps.append({
                "tool": info.get("name") or step.get("tool_name"),
                "params": info.get("parameters") or {},
                "error": error or ("failed" if step.get("state") == "ERROR" else None),
            })
    return steps


def rel_to(path: str, prefixes: list[str]) -> str:
    for prefix in prefixes:
        if prefix and path.startswith(prefix.rstrip("/") + "/"):
            return path[len(prefix.rstrip("/")) + 1:]
    return path


def opened_files(steps: list[dict], prefixes: list[str]) -> list[str]:
    """Repo-relative paths of files agy opened with a file-viewing tool."""
    out = []
    for step in steps:
        if "view" not in (step["tool"] or "") or step.get("error"):
            continue
        path = next((v for k, v in step["params"].items() if "path" in k.lower() and isinstance(v, str)), "")
        if path and CONTEXT_DIRNAME not in path:
            rel = rel_to(path, prefixes)
            if rel not in out:
                out.append(rel)
    return out


def files_read(steps: list[dict], prefixes: list[str], outside: str | None = None) -> list[str]:
    """Human-readable list of what agy opened or searched, relative to the repo.

    `outside` is the user's real repo in a read-only job: agy should only touch
    the snapshot, so paths under the real repo are shown in full and flagged.
    """
    def rel(path: str) -> str:
        if outside and path.startswith(outside.rstrip("/") + "/"):
            return f"{path} [original repo, outside the read-only snapshot]"
        return rel_to(path, prefixes)

    out: list[str] = []
    for step in steps:
        params = step["params"]
        path = next((v for k, v in params.items() if "path" in k.lower() and isinstance(v, str)), "")
        lines = [f"{v}" for k, v in params.items() if "line" in k.lower()]
        query = next((v for k, v in params.items() if k.lower() in ("query", "pattern") and isinstance(v, str)), "")
        label = step["tool"] or "?"
        if CONTEXT_DIRNAME in path:
            continue
        entry = f"{label} {rel(path)}".strip()
        if lines:
            entry += f" (lines {'-'.join(lines)})"
        if query:
            entry += f" for {query!r}"
        if step.get("error"):
            entry = f"DENIED/FAILED {entry} ({step['error'][:80]})"
        if entry not in out:
            out.append(entry)
    return out


CITATION_RE = re.compile(r"(?:file://)?([\w./-]+\.\w{1,5})(?:#L|:)\d+")
# Accepts `name`, `"name"` and `'name'`: agy usually quotes dict keys.
IDENT_RE = re.compile(r"`[\"']?([A-Za-z_][\w.]*)[\"']?`")


def check_citations(root: str, text: str, opened: list[str] | None = None, extra_roots: list[str] | None = None) -> dict:
    """Check that backticked identifiers exist, as whole words, in the cited files.

    Only identifiers containing "_" or "." are checked: those are the dict keys,
    parameters and attribute paths agy tends to invent. Misses are split in two:
    `uncited` names exist in a file agy opened but did not cite; `missing` names
    are in no cited or opened file, which is the signature of an invented name.
    """
    root_path = Path(root)
    cited = set()
    for match in CITATION_RE.finditer(text):
        path = match.group(1)
        if path.startswith(root.rstrip("/") + "/"):
            path = path[len(root.rstrip("/")) + 1:]
        if path.startswith("/"):
            # absolute citations count only inside an --add-dir
            if any(path.startswith(d.rstrip("/") + "/") for d in extra_roots or []) and Path(path).is_file():
                cited.add(path)
        elif (root_path / path).is_file():
            cited.add(path)
    if not cited:
        return {"cited_files": [], "checked": 0, "missing": [], "uncited": []}

    def read_all(paths) -> str:
        return "\n".join((root_path / p).read_text(errors="replace") for p in sorted(paths) if (root_path / p).is_file())

    def found(name: str, corpus: str) -> bool:
        return re.search(rf"(?<!\w){re.escape(name.split('.')[-1])}(?!\w)", corpus) is not None

    corpus = read_all(cited)
    names = sorted({
        t for t in IDENT_RE.findall(text)
        if ("_" in t or "." in t) and not re.search(r"\.(py|md|json|ts|js|toml|ya?ml|txt|sh|html)$", t)
    })
    misses = [t for t in names if not found(t, corpus)]
    opened_corpus = read_all(set(opened or []) - cited)
    uncited = [t for t in misses if found(t, opened_corpus)]
    missing = [t for t in misses if t not in uncited]
    return {"cited_files": sorted(cited), "checked": len(names), "missing": missing, "uncited": uncited}


UNSAFE_ADVICE_RE = re.compile(r"\s*Alternatively, re-run with --dangerously-skip-permissions[^.]*\.", re.I)


def scrub_unsafe_advice(text: str) -> str:
    """Drop agy's own suggestion to bypass permissions from error text.

    agy's denial message ends with "re-run with --dangerously-skip-permissions";
    relayed verbatim, it invites the calling agent to do exactly that.
    """
    return UNSAFE_ADVICE_RE.sub("", text)


def point_at_snapshot(text: str, root: str, wt: Path) -> str:
    """Rewrite mentions of the real repo path to the read-only snapshot path.

    In a read-only job agy's workspace is the snapshot; the real repo is outside
    it, and reading there is denied (unless it sits somewhere agy may read
    anyway, such as /tmp). Word-boundary match so /repo does not hit /repo-2.
    """
    return re.sub(re.escape(root.rstrip("/")) + r"(?![\w-])(?!\.\w)", str(wt), text)


def snapshot_note(root: str, wt: Path) -> str:
    return (
        f"\n\nWorkspace: the repository {root} is available to you as a read-only snapshot at {wt}, "
        "with the user's uncommitted changes included. Read it through paths under that snapshot "
        f"(or paths relative to it). Do not use paths under {root}: that location is outside your workspace "
        "and reading it is denied."
    )


def add_dirs_note(job: dict) -> str:
    dirs = job.get("add_dirs") or []
    if not dirs:
        return ""
    listed = "\n".join(f"- {d}" for d in dirs)
    return (
        "\n\nExtra directories you may READ (reference only):\n"
        f"{listed}\n"
        "Read them with view_file/grep_search using absolute paths and cite them as absolute path:line. "
        "Never create, edit or delete files there: writes outside the workspace are denied, and a denial aborts your turn."
    )


def build_agy_cmd(job: dict, prompt: str) -> list[str]:
    cmd = [require_agy(), "--output-format", "stream-json"]
    for extra in job.get("add_dirs") or []:
        cmd += ["--add-dir", extra]
    if job.get("model"):
        cmd += ["--model", job["model"]]
    if job.get("effort"):
        cmd += ["--effort", job["effort"]]
    if job.get("resume_conversation"):
        cmd += ["--conversation", job["resume_conversation"]]
    cmd += [f"--print={prompt}"]  # attached form: -p would swallow the next flag
    return cmd


def run_job(job_id: str) -> None:
    """Runner body. Executes in a detached process group."""
    job = load_job(job_id)
    job.update(status="running", started_at=now(), runner_pid=os.getpid())
    save_job(job)
    root, wt = job["workspace"], None
    try:
        if job["read_only"]:
            wt = make_worktree(root, job_id)
            job["worktree"] = str(wt)
            save_job(job)
        cwd = str(wt or root)

        if job["kind"] in ("review", "adversarial-review"):
            ctx_text, has_changes = review_context(root, job.get("base"))
            if not has_changes:
                job.update(status="completed", response="Nothing to review: no changes found in the requested scope.")
                return
            ctx_file = write_context(wt, ctx_text)
            template = REVIEW_PROMPT if job["kind"] == "review" else ADVERSARIAL_PROMPT
            user_focus = point_at_snapshot(job["prompt"], root, wt) if job["prompt"] else ""
            focus = f"\nExtra focus requested by the user: {user_focus}\n" if user_focus else ""
            prompt = template.format(
                context_file=ctx_file, focus=focus,
                rules=f"{READ_ONLY_RULES}\n\n{EVIDENCE_RULES}{add_dirs_note(job)}{snapshot_note(root, wt)}",
            )
        else:
            if job["read_only"]:
                rules = READ_ONLY_RULES
            else:
                rules = WRITE_RULES_WITH_SHELL if job.get("allow_shell") else WRITE_RULES
            user_prompt = point_at_snapshot(job["prompt"], root, wt) if wt else job["prompt"]
            sandbox = snapshot_note(root, wt) if wt else ""
            prompt = f"{user_prompt}\n\n{rules}\n\n{EVIDENCE_RULES}{add_dirs_note(job)}{sandbox}"

        stdout_f = DATA_DIR / "logs" / f"{job_id}.stdout"
        stderr_f = DATA_DIR / "logs" / f"{job_id}.stderr"
        stdout_f.parent.mkdir(parents=True, exist_ok=True)
        with open(stdout_f, "w") as out, open(stderr_f, "w") as err:
            proc = subprocess.run(
                build_agy_cmd(job, prompt), cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL
            )
        stdout, stderr = stdout_f.read_text(), stderr_f.read_text()
        result = parse_agy_output(stdout)
        job["exit_code"] = proc.returncode
        if result is None:
            job.update(status="failed", error=(stderr or stdout or "agy produced no output").strip()[-4000:])
            return
        job["conversation_id"] = result.get("conversation_id")
        job["response"] = result.get("response", "")
        if wt:  # links should point at the user's repo, not the deleted worktree
            job["response"] = job["response"].replace(str(wt), root)
        job["usage"] = result.get("usage")
        job["agy_duration_seconds"] = result.get("duration_seconds")
        job["denied_actions"] = result.get("denied_actions") or []
        steps, prefixes = tool_trace(stdout), [str(wt) if wt else "", root]
        job["files_read"] = files_read(steps, [str(wt)] if wt else [root], outside=root if wt else None)
        if job["response"].strip():
            job["citation_check"] = check_citations(
                root, job["response"], opened_files(steps, prefixes), job.get("add_dirs")
            )
        ok = result.get("status") == "SUCCESS" and proc.returncode == 0
        if ok and not job["response"].strip():
            ok = False
            job["error"] = (stderr.strip() or "agy returned an empty response")[-4000:]
        elif not ok:
            job["error"] = (result.get("error") or stderr.strip() or f"agy exited {proc.returncode}")[-4000:]
        job["status"] = "completed" if ok else "failed"
    except Exception as exc:  # noqa: BLE001 - runner must always record an outcome
        job.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if job.get("error"):
            job["error"] = scrub_unsafe_advice(job["error"])
        if wt:
            remove_worktree(root, str(wt))
        if job["status"] not in TERMINAL:
            job["status"] = "failed"
        job["finished_at"] = now()
        save_job(job)


def spawn(job: dict) -> None:
    save_job(job)
    log = DATA_DIR / "logs" / f"{job['id']}.runner"
    log.parent.mkdir(parents=True, exist_ok=True)
    # The runner records its own pid; re-saving here would race with it.
    with open(log, "w") as fh:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_run", job["id"]],
            stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, start_new_session=True,
        )


# --------------------------------------------------------------------------
# rendering


def render_result(job: dict) -> str:
    lines = [f"agy job {job['id']} ({job['kind']}) — {job['status']}"]
    meta = []
    if job.get("model"):
        meta.append(f"model={job['model']}")
    if job.get("usage"):
        meta.append(f"tokens={job['usage'].get('total_tokens')}")
    if job.get("agy_duration_seconds"):
        meta.append(f"agy_time={job['agy_duration_seconds']:.0f}s")
    if job.get("conversation_id"):
        meta.append(f"conversation={job['conversation_id']}")
    if meta:
        lines.append(" ".join(meta))
    if job.get("denied_actions"):
        actions = {a.get("action", "?") for a in job["denied_actions"]}
        names = ", ".join(sorted({a.get("display_name") or a.get("action", "?") for a in job["denied_actions"]}))
        hints = []
        if "command" in actions:
            hints.append(
                "for shell commands, allow specific ones in ~/.gemini/antigravity-cli/settings.json "
                "under permissions.allow, e.g. \"command(uv run pytest)\", and pass --allow-shell"
            )
        if "read_file" in actions and any("[original repo" in e for e in job.get("files_read") or []):
            hints.append(
                "agy tried to read the real repo instead of its read-only snapshot; this is a plugin issue, "
                "report it (do not add the repo as --add-dir)"
            )
        elif "read_file" in actions:
            hints.append(
                "for reads outside the repo, pass --add-dir <dir> (a symlink whose target is outside "
                "the workspace is denied too: use a real copy or add the target's directory)"
            )
        if "write_file" in actions:
            hints.append("agy tried to write outside the workspace (e.g. into an --add-dir), which headless mode denies")
        lines.append(
            f"WARNING: agy was denied these tools in headless mode: {names}. A denial ends agy's turn, so its answer "
            f"is likely incomplete. Do not retry with --dangerously-skip-permissions; report this to the user. "
            + ("Fix: " + "; ".join(hints) + "." if hints else "")
        )
    if job.get("error"):
        lines.append(f"ERROR: {job['error']}")
    check = job.get("citation_check")
    if check is not None:
        if not check["cited_files"]:
            lines.append("EVIDENCE CHECK: the answer cites no path:line in this repo, so none of its names could be verified.")
        elif check["missing"] or check.get("uncited"):
            if check["missing"]:
                lines.append(
                    f"WARNING — EVIDENCE CHECK: {len(check['missing'])} of {check['checked']} identifiers in the answer "
                    f"appear in no file agy cited or opened: {', '.join(check['missing'])}. "
                    "Treat them as possibly invented until checked against the code."
                )
            if check.get("uncited"):
                lines.append(
                    f"Evidence note: {', '.join(check['uncited'])} exist in files agy opened but did not cite; "
                    "the citation next to them may point to the wrong file."
                )
            ok_count = check["checked"] - len(check["missing"]) - len(check.get("uncited", []))
            lines.append(f"Evidence check: {ok_count} of {check['checked']} identifiers found in the cited files.")
        elif check["checked"] == 0:
            lines.append("Evidence check: the answer names no identifiers with '_' or '.' to verify.")
        else:
            lines.append(
                f"Evidence check: all {check['checked']} identifiers found in the cited files "
                f"({', '.join(check['cited_files'])})."
            )
    if job.get("response"):
        lines.append("")
        lines.append(job["response"].rstrip())
    if job.get("files_read"):
        shown = job["files_read"][:25]
        lines.append("")
        lines.append(f"Files agy actually opened or searched (from its tool trace, {len(job['files_read'])} steps):")
        lines += [f"- {entry}" for entry in shown]
        if len(job["files_read"]) > len(shown):
            lines.append(f"- … {len(job['files_read']) - len(shown)} more")
    return "\n".join(lines)


def render_row(job: dict) -> str:
    prompt = (job.get("prompt") or "").replace("\n", " ")
    prompt = (prompt[:60] + "…") if len(prompt) > 60 else prompt
    return f"{job['id']}  {job['status']:<9}  {job['kind']:<18}  {fmt_ts(job['created_at'])}  {prompt}"


def wait_for(job_id: str, seconds: int) -> dict:
    deadline = now() + seconds
    while True:
        job = refresh(load_job(job_id))
        if job["status"] in TERMINAL or now() >= deadline:
            return job
        time.sleep(2)


def finish(job: dict, background: bool, wait: int) -> None:
    spawn(job)
    if background:
        print(f"Started agy job {job['id']} ({job['kind']}) in the background.")
        print("Check it with /agy:status, fetch it with /agy:result, stop it with /agy:cancel.")
        return
    job = wait_for(job["id"], wait)
    if job["status"] in TERMINAL:
        print(render_result(job))
        sys.exit(0 if job["status"] == "completed" else 1)
    print(f"agy job {job['id']} is still running after {wait}s; it continues in the background.")
    print(f"Fetch it later with /agy:result {job['id']}.")


# --------------------------------------------------------------------------
# commands


def resolve_add_dirs(dirs: list[str] | None) -> list[str]:
    out = []
    for d in dirs or []:
        path = os.path.realpath(os.path.expanduser(d))
        if not os.path.isdir(path):
            die(f"--add-dir {d}: not a directory")
        if path not in out:
            out.append(path)
    return out


def new_job(kind: str, args: argparse.Namespace, workspace: str, prompt: str, read_only: bool) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "status": "queued",
        "workspace": workspace,
        "prompt": prompt,
        "read_only": read_only,
        "model": args.model or os.environ.get("AGY_COMPANION_MODEL") or None,
        "effort": args.effort,
        "base": getattr(args, "base", None),
        "add_dirs": resolve_add_dirs(getattr(args, "add_dir", None)),
        "created_at": now(),
    }


def cmd_task(args: argparse.Namespace) -> None:
    require_agy()
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        die("task needs a prompt describing what agy should do")
    cwd = os.getcwd()
    root = repo_root(cwd)
    if args.read_only and not root:
        die("--read-only needs a git repository (it runs agy in a throwaway worktree)")
    workspace = root or cwd
    job = new_job("task", args, workspace, prompt, args.read_only)
    job["allow_shell"] = args.allow_shell
    if args.resume:
        prev = next(
            (j for j in jobs_for(workspace) if j["kind"] == "task" and j.get("conversation_id")),
            None,
        )
        if not prev:
            die("--resume: no previous agy task with a conversation in this workspace")
        job["resume_conversation"] = prev["conversation_id"]
    finish(job, args.background, args.wait)


def cmd_review(args: argparse.Namespace, kind: str) -> None:
    require_agy()
    root = repo_root(os.getcwd())
    if not root:
        die("reviews need a git repository")
    job = new_job(kind, args, root, " ".join(args.focus).strip(), read_only=True)
    finish(job, args.background, args.wait)


def cmd_status(args: argparse.Namespace) -> None:
    if args.job:
        job = refresh(load_job(args.job))
        print(render_row(job))
        print(f"started {fmt_ts(job.get('started_at'))}  finished {fmt_ts(job.get('finished_at'))}")
        return
    root = repo_root(os.getcwd()) or os.getcwd()
    jobs = jobs_for(root)[: args.limit]
    if not jobs:
        print("No agy jobs for this workspace.")
        return
    print(f"{'id':<8}  {'status':<9}  {'kind':<18}  {'created':<19}  prompt")
    for job in jobs:
        print(render_row(job))


def latest_finished(root: str) -> dict:
    job = next((j for j in jobs_for(root) if j["status"] in TERMINAL), None)
    if not job:
        die("no finished agy job in this workspace")
    return job


def cmd_result(args: argparse.Namespace) -> None:
    root = repo_root(os.getcwd()) or os.getcwd()
    job = refresh(load_job(args.job)) if args.job else latest_finished(root)
    if job["status"] not in TERMINAL:
        print(f"agy job {job['id']} is still {job['status']}.")
        return
    print(render_result(job))


def cmd_cancel(args: argparse.Namespace) -> None:
    root = repo_root(os.getcwd()) or os.getcwd()
    if args.job:
        job = refresh(load_job(args.job))
    else:
        job = next((j for j in jobs_for(root) if j["status"] in ("queued", "running")), None)
        if not job:
            die("no running agy job in this workspace")
    if job["status"] in TERMINAL:
        print(f"agy job {job['id']} already {job['status']}.")
        return
    pid = job.get("runner_pid")
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, TypeError):
        pass
    time.sleep(1)
    job = load_job(job["id"])
    if job.get("worktree") and Path(job["worktree"]).exists():
        remove_worktree(job["workspace"], job["worktree"])
    job.update(status="cancelled", finished_at=now())
    save_job(job)
    print(f"Cancelled agy job {job['id']}.")


def cmd_setup(_args: argparse.Namespace) -> None:
    exe = shutil.which("agy")
    if not exe:
        print("agy: NOT FOUND on PATH. Install the Antigravity CLI, then run `agy` once to sign in.")
        sys.exit(1)
    version = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"agy: {exe} (version {version or 'unknown'})")
    print("Checking sign-in with a one-line prompt (takes ~20s)…")
    proc = subprocess.run(
        [exe, "--output-format", "json", "--print=Reply with exactly: PONG"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
    )
    result = parse_agy_output(proc.stdout)
    if result and "PONG" in (result.get("response") or ""):
        print(f"auth: OK (default model answered; {result['usage'].get('total_tokens')} tokens)")
    else:
        print("auth: FAILED — run `agy` interactively to sign in.")
        print((proc.stderr or proc.stdout).strip()[-1500:])
        sys.exit(1)
    print(f"jobs dir: {JOBS_DIR}")
    print(f"read-only worktrees: {WORKTREES_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="agy_companion")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_run_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", help="agy model id (see `agy models`)")
        p.add_argument("--effort", choices=["low", "medium", "high", "max"])
        p.add_argument("--background", action="store_true", help="return immediately with a job id")
        p.add_argument("--wait", type=int, default=DEFAULT_WAIT_SECONDS, help="max seconds to wait in the foreground")
        p.add_argument(
            "--add-dir", action="append", metavar="DIR",
            help="extra directory agy may read (repeatable); headless agy denies writes there",
        )

    p = sub.add_parser("task")
    add_run_flags(p)
    p.add_argument("--read-only", action="store_true", help="run in a throwaway worktree; your files are never touched")
    p.add_argument("--resume", action="store_true", help="continue the latest agy task conversation in this workspace")
    p.add_argument(
        "--allow-shell", action="store_true",
        help="tell agy it may use the shell commands you allow-listed in agy's settings.json (does NOT bypass agy permissions)",
    )
    p.add_argument("prompt", nargs=argparse.REMAINDER)

    for name in ("review", "adversarial-review"):
        p = sub.add_parser(name)
        add_run_flags(p)
        p.add_argument("--base", help="review the branch against this ref instead of uncommitted changes")
        p.add_argument("focus", nargs=argparse.REMAINDER)

    p = sub.add_parser("status")
    p.add_argument("job", nargs="?")
    p.add_argument("--limit", type=int, default=10)
    for name in ("result", "cancel"):
        sub.add_parser(name).add_argument("job", nargs="?")
    sub.add_parser("setup")
    sub.add_parser("_run").add_argument("job")

    args = parser.parse_args()
    if args.cmd == "_run":
        run_job(args.job)
    elif args.cmd == "task":
        cmd_task(args)
    elif args.cmd in ("review", "adversarial-review"):
        cmd_review(args, args.cmd)
    else:
        {"status": cmd_status, "result": cmd_result, "cancel": cmd_cancel, "setup": cmd_setup}[args.cmd](args)


if __name__ == "__main__":
    main()
