# reps

Run coding agents (Claude Code, Codex, anything with a headless mode) on a schedule, on your own machine.

- A **job** is a folder `jobs/<name>/` with a `JOB.md`: settings on top, instructions below. Put helper files next to it.
- Each job gets **one stable git worktree** (`~/.reps/worktrees/<name>`, branch `reps/<name>`). reps creates it and rebases it on the base branch before every run. If the last run left uncommitted changes or the rebase conflicts, it leaves the worktree alone and notes why.
- Every run is **recorded outside the repo** in `~/.reps/runs/<name>/<time>/`: `output.log`, `meta.json`, and a `summary.md` the agent writes. The next run is pointed at the last summary, so the agent remembers what it did.
- **Scheduling** is done by macOS launchd. No daemon. Runs missed while the laptop slept fire once on wake.

Python 3.9+, standard library only.

## Job file

```markdown
---
repo: ~/dev/myrepo
every: 6h                    # 30m, 6h, 1d. Leave out to only run by hand
agent: codex exec --full-auto
timeout: 45m                 # default 1h; kills the agent and its children
base: origin/main            # default: origin/HEAD, else the repo's current branch
---
What the agent should do.
```

The agent command gets the prompt as its last argument and no stdin. It also sees `REPS_JOB`, `REPS_JOB_DIR`, `REPS_RUN_DIR`.

Some agent flags take many values (`claude --allowedTools a b`) and would swallow the prompt: write them as `--allowedTools=a,b`.

## Commands

```sh
./reps.py run <job>     # run now (skips if already running)
./reps.py install       # sync launchd with jobs/ (adds, updates, removes)
./reps.py list          # jobs and their last result
./reps.py logs <job>    # last run: meta, summary, output
python3 test_reps.py    # end-to-end self-check with a fake agent
```

`REPS_HOME` (default `~/.reps`) and `REPS_JOBS` (default `./jobs`) override the paths.

Not done yet: Linux scheduling. reps never pushes; say so in the job file if you want it.
