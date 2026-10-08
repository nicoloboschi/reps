"""Run: python3 test_reps.py  — end to end with a fake agent, no real agent or launchd needed."""
import json, os, subprocess, sys, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
repo, jobs, home = tmp / "repo", tmp / "jobs", tmp / "home"
sh = lambda cmd, cwd=None: subprocess.run(cmd, shell=True, cwd=cwd, check=True, capture_output=True, text=True).stdout
sh(f"git init -q -b main {repo} && git -C {repo} commit -q --allow-empty -m init")

# fake agent: prompt arrives as $0; commits a file, writes the summary, echoes whether it saw a previous summary
agent = "sh -c 'echo \"$0\" | grep -c \"last run\"; date +%s%N > f; git add f; git commit -qm run; echo ok > $REPS_RUN_DIR/summary.md'"
(jobs / "demo").mkdir(parents=True)
(jobs / "demo" / "JOB.md").write_text(f"---\nrepo: {repo}\nevery: 1h\nagent: {agent}\ntimeout: 1m\n---\nDo it.\n")

env = {**os.environ, "REPS_HOME": str(home), "REPS_JOBS": str(jobs)}
reps = lambda *a: subprocess.run([sys.executable, Path(__file__).with_name("reps.py"), *a], env=env, capture_output=True, text=True)

assert reps("run", "demo").returncode == 0
wt = home / "worktrees" / "demo"
assert sh("git branch --show-current", wt).strip() == "reps/demo"

sh("git commit -q --allow-empty -m upstream", repo)  # base moves on
assert reps("run", "demo").returncode == 0
runs = sorted((home / "runs" / "demo").iterdir())
first, second = (json.loads((r / "meta.json").read_text()) for r in runs)
assert first["sync"] == "created on main", first
assert second["sync"] == "rebased on main", second
assert (runs[0] / "output.log").read_text().strip() == "0"  # no previous summary on run 1
assert (runs[1] / "output.log").read_text().strip() == "1"  # saw it on run 2
assert "upstream" in sh("git log --format=%s", wt)  # worktree picked up the base change
assert sh("git log --format=%s", wt).count("run") == 2  # and kept its own commits

last_meta = lambda: json.loads(sorted((home / "runs" / "demo").iterdir())[-1].joinpath("meta.json").read_text())
(wt / "junk").write_text("x")  # uncommitted leftovers are never touched
(jobs / "demo" / "JOB.md").write_text(f"---\nrepo: {repo}\nagent: true\n---\n")
reps("run", "demo")
assert last_meta()["sync"].startswith("dirty")

(jobs / "demo" / "JOB.md").write_text(f"---\nrepo: {repo}\nagent: sh -c 'sleep 30'\ntimeout: 1s\n---\n")
reps("run", "demo")
assert last_meta().get("timed_out")

(jobs / "broken").mkdir()
(jobs / "broken" / "JOB.md").write_text(f"---\nrepo: {tmp}/nope\nagent: true\n---\n")
assert reps("run", "broken").returncode == 1
assert "error" in json.loads(next((home / "runs" / "broken").glob("*/meta.json")).read_text())

(jobs / "demo" / "JOB.md").write_text(f"---\nrepo: {repo}\nagent: no-such-agent-xyz\n# every: 1h\n---\n")
assert reps("run", "demo").returncode != 0
assert "error" in last_meta()
demo_line = next(l for l in reps("list").stdout.splitlines() if l.startswith("demo"))
assert "every -" in demo_line, demo_line  # commented-out "# every" is ignored
assert "exit -1" in demo_line, demo_line  # missing agent is recorded, not stuck "running"

assert "demo" in reps("list").stdout
print("ok")
