---
name: reps
description: Schedule recurring coding-agent work on this machine with reps — a job runs Claude Code, Codex, Gemini or any headless agent every N hours in its own stable git worktree, and records each run outside the repo. Use when the user wants something done "every X hours/days", a recurring or background agent task, a cron/scheduled job for a repo, or asks about reps jobs, runs or logs.
---

# reps

reps runs an agent on a repo on a schedule. Each job gets one git worktree that is kept between runs
(`~/.reps/worktrees/<job>`, branch `reps/<job>`), rebased on the base branch before each run.
Every run is saved in `~/.reps/runs/<job>/<time>/` (`output.log`, `meta.json`, `summary.md`).
macOS launchd does the scheduling.

## 1. Make sure it is installed and current

```sh
reps version
```

- `command not found` → install: `curl -fsSL https://raw.githubusercontent.com/nicoloboschi/reps/main/install.sh | sh`
  (needs git and python3; puts `reps` in `~/.local/bin`; if that is not on PATH, use `~/.local/bin/reps`).
- Installed → run `reps update` only if the user asks to update, or a command below is missing.

## 2. Create a job

Write `~/.reps/jobs/<name>/JOB.md` (name: lowercase, dashes). Settings on top, instructions below:

```markdown
---
repo: ~/dev/myrepo           # required: the repo to work on
agent: claude -p --permission-mode acceptEdits --allowedTools=Read,Edit,Write,Bash(git:*)
every: 6h                    # 30m, 6h, 1d — leave out for manual-only
timeout: 45m                 # optional, default 1h
base: origin/main            # optional, default origin/HEAD or the repo's current branch
---
What to do each run, written for an agent that has never seen this conversation.
Say what "done" looks like, and whether to push or open a PR (reps never pushes on its own).
```

Helper files (scripts, checklists) can go next to `JOB.md`; the agent is told where the folder is.

Each run, the agent is told to read `JOB.md` and its previous `summary.md`, and to write a new summary.
So the instructions do not need to repeat that — just describe the work.

### The `agent` line

It runs with no terminal and no stdin, and the prompt is added as the last argument. It must never wait for approval:

| Agent | `agent:` |
|---|---|
| Claude Code | `claude -p --permission-mode acceptEdits --allowedTools=Read,Edit,Write,Bash(git:*),Bash(npm test:*)` |
| Codex | `codex exec --full-auto` |
| Gemini CLI | `gemini --yolo -p` |
| opencode | `opencode run` |

Use the agent the user asked for; default to the one you are. Flags that take a list must use `=` (`--allowedTools=a,b`), or they swallow the prompt.

## 3. Try it, then schedule it

```sh
reps run <name>      # run once now, in the foreground
reps logs <name>     # check meta.json exit_code, summary.md, output.log
reps install         # schedule every job that has `every:` (also removes deleted jobs)
reps list            # all jobs and their last result
```

Always do one `reps run` and read `reps logs` before `reps install`, and show the user the result.

To change a schedule, edit `every:` and run `reps install` again. To remove a job, delete its folder and run `reps install`.

## Notes

- `meta.json` `sync` says what happened to the worktree: `created`, `rebased`, or why it was left alone
  (uncommitted changes, rebase conflict). Fix those in `~/.reps/worktrees/<job>` if a job is stuck.
- A run is skipped if the previous one is still going. Missed runs while the Mac slept fire once on wake.
- Only macOS scheduling is supported. On Linux, `reps run <name>` works; schedule it with cron yourself.
