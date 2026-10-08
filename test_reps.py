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
demo_line = next(l for l in reps("list").stdout.splitlines() if l.startswith("demo")).split()
assert demo_line[1] == "-", demo_line  # commented-out "# every" is ignored
assert demo_line[-1] == "error", demo_line  # missing agent is recorded, not stuck "running"

(jobs / "norepo").mkdir()
(jobs / "norepo" / "JOB.md").write_text("---\nagent: sh -c 'pwd > out'\n---\n")
assert reps("run", "norepo").returncode == 0
assert (jobs / "norepo" / "out").read_text().strip().endswith("norepo")  # ran in the job folder

listing = {l.split()[0]: l.split() for l in reps("list").stdout.splitlines()[1:]}
assert listing["demo"][3:6] == ["5", "3", "2"], listing["demo"]  # runs, ok, failed (timeout + missing agent)
runs_out = reps("runs", "demo").stdout
assert "timeout" in runs_out and "new commits" in runs_out, runs_out
(jobs / "k").mkdir()
(jobs / "k" / "JOB.md").write_text("---\nagent: sh -c 'kill -TERM $$'\n---\n")
reps("run", "k")
assert "killed" in reps("runs", "k").stdout  # stopped from outside, not a plain failure

event = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "cleaning up"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "df -h"}}]}})
(jobs / "k" / "ev.json").write_text(event + "\n" + json.dumps({"type": "system", "subtype": "hook"}) + "\n")
(jobs / "k" / "JOB.md").write_text("---\nagent: sh -c 'cat ev.json'\n---\n")
reps("run", "k")
log = reps("logs", "k").stdout
assert '"job": "k",\n' in log, log  # other files keep their line breaks
assert "cleaning up" in log and '> Bash {"command": "df -h"}' in log and "hook" not in log, log

for i in range(205):  # old runs are pruned to the newest 200 (+ the new one)
    (home / "runs" / "k" / f"00000000-{i:06d}.000").mkdir()
reps("run", "k")
assert len(list((home / "runs" / "k").iterdir())) == 201

# --json: the same facts, for other tools (sheepit reads these)
(jobs / "bad").mkdir()
(jobs / "bad" / "JOB.md").write_text("no frontmatter\n")
jobs_json = {j["name"]: j for j in json.loads(reps("list", "--json").stdout)}
assert "error" in jobs_json["bad"], jobs_json["bad"]  # one broken job doesn't break the list
d = jobs_json["demo"]
assert (d["runs"], d["ok"], d["failed"]) == (5, 3, 2), d
assert d["worktree"] == str(wt) and d["repo"] == str(repo) and d["last"]["status"] == "error", d
assert jobs_json["norepo"]["worktree"] is None and jobs_json["norepo"]["prompt"] == ""
runs_json = json.loads(reps("runs", "demo", "--json").stdout)
assert [r["status"] for r in runs_json][:2] == ["ok", "ok"] and runs_json[0]["new_commits"], runs_json
first_id = runs_json[0]["id"]
one = json.loads(reps("logs", "demo", "--run", first_id, "--json").stdout)
assert one["id"] == first_id and one["summary"].strip() == "ok" and one["output"].strip() == "0", one
assert "cleaning up" in json.loads(reps("logs", "k", "--json").stdout)["output"]

print("ok")
