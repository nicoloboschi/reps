#!/usr/bin/env python3
"""reps: run coding agents on a schedule, each job in its own stable git worktree.

A job is a folder ~/.reps/jobs/<name>/ with a JOB.md:

    ---
    repo: ~/dev/myrepo    # optional; without it the agent runs in the job folder
    every: 6h
    agent: claude -p --permission-mode acceptEdits
    timeout: 45m          # optional, default 1h
    base: origin/main     # optional, default origin/HEAD or the repo's current branch
    ---
    What the agent should do, in plain markdown.

Usage: reps run <job> | install | list | runs <job> | logs <job> [--run <id>] | version | update
       list, runs and logs take --json for scripts and other tools.
"""
import fcntl, json, os, plistlib, re, shlex, shutil, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("REPS_HOME", Path.home() / ".reps"))
JOBS = Path(os.environ.get("REPS_JOBS", HOME / "jobs"))
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
KEEP_RUNS = 200  # per job; an hourly job keeps about 8 days


def seconds(text):
    m = re.fullmatch(r"(\d+)([smhd])", text.strip())
    if not m:
        sys.exit(f"bad duration {text!r}, use e.g. 30m, 6h, 1d")
    return int(m[1]) * UNITS[m[2]]


def load(name):
    md = JOBS / name / "JOB.md"
    if not md.exists():
        sys.exit(f"no job at {md}")
    m = re.match(r"---\n(.*?)\n---\n", md.read_text(), re.S)
    if not m:
        sys.exit(f"{md}: missing --- frontmatter ---")
    job = {"name": name, "md": md}
    for line in m[1].splitlines():
        line = line.split(" #")[0].strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition(":")
            job[k.strip()] = v.strip()
    if not job.get("agent"):
        sys.exit(f"{md}: 'agent' is required")
    if job.get("repo"):
        job["repo"] = Path(job["repo"]).expanduser()
    return job


def all_jobs():
    return sorted(p.parent.name for p in JOBS.glob("*/JOB.md"))


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def prepare_worktree(job):
    """Create the job's worktree once, then keep it on top of base. Returns (path, note)."""
    if not job.get("repo"):
        return job["md"].parent, "no repo, runs in the job folder"
    wt, branch = HOME / "worktrees" / job["name"], f"reps/{job['name']}"
    repo = job["repo"]
    git(repo, "fetch", "--quiet")  # ok to fail: no remote, offline
    origin = git(repo, "rev-parse", "--abbrev-ref", "origin/HEAD")
    base = job.get("base") or (origin.stdout.strip() if origin.returncode == 0
                               else git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip())
    if not wt.exists():
        git(repo, "worktree", "prune")
        has_branch = git(repo, "rev-parse", "--verify", "--quiet", branch).returncode == 0
        args = [str(wt), branch] if has_branch else ["-b", branch, str(wt), base]
        r = git(repo, "worktree", "add", "--quiet", *args)
        if r.returncode:
            raise RuntimeError(f"worktree add failed: {r.stderr.strip()}")
        return wt, f"created on {base}"
    if git(wt, "status", "--porcelain").stdout.strip():
        return wt, f"dirty, not synced with {base}"  # never touch work the last run left behind
    if git(wt, "rebase", "--quiet", base).returncode:
        git(wt, "rebase", "--abort")
        return wt, f"rebase on {base} had conflicts, left as is"
    return wt, f"rebased on {base}"


def run(name):
    job = load(name)
    (HOME / "locks").mkdir(parents=True, exist_ok=True)
    lock = open(HOME / "locks" / f"{name}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{name}: previous run still going, skipping")
        return 0

    runs = HOME / "runs" / name
    for old in run_dirs(name)[:-KEEP_RUNS]:
        shutil.rmtree(old, ignore_errors=True)
    previous = sorted(runs.glob("*/summary.md")) if runs.exists() else []
    now = time.time()
    run_dir = runs / (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}")
    run_dir.mkdir(parents=True)
    meta = {"job": name, "started": now}
    try:
        wt, meta["sync"] = prepare_worktree(job)
    except RuntimeError as e:
        meta.update(exit_code=-1, error=str(e), ended=time.time())
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        print(f"{name}: {e}")
        return 1

    prompt = (
        f"You are running the scheduled job '{name}'. Read {job['md']} and do what it says.\n"
        f"Other files for this job are in {job['md'].parent}.\n"
        + (f"You are in a git worktree on branch reps/{name}; it is kept between runs.\n" if job.get("repo") else "")
        + (f"Your summary from the last run is at {previous[-1]}.\n" if previous else "This is the first run.\n")
        + f"When done, write a short summary of what you did and what is left to {run_dir}/summary.md."
    )
    env = {**os.environ, "REPS_JOB": name, "REPS_JOB_DIR": str(job["md"].parent), "REPS_RUN_DIR": str(run_dir)}
    meta.update(worktree=str(wt), head_before=git(wt, "rev-parse", "HEAD").stdout.strip())
    with open(run_dir / "output.log", "w") as out:
        # own process group so a timeout kills the agent's children too
        try:
            p = subprocess.Popen(shlex.split(job["agent"]) + [prompt], cwd=wt, env=env, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:  # agent not on PATH etc.: record it, don't leave the run looking "running"
            p, meta["exit_code"], meta["error"] = None, -1, str(e)
            print(e, file=out)
        if p:
            try:
                meta["exit_code"] = p.wait(timeout=seconds(job.get("timeout", "1h")))
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                meta["exit_code"], meta["timed_out"] = p.wait(), True
    meta["ended"] = time.time()
    meta["head_after"] = git(wt, "rev-parse", "HEAD").stdout.strip()
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"{name}: exit {meta['exit_code']}, log in {run_dir}")
    return meta["exit_code"]


def install():
    """One launchd agent per job with `every`; removes agents for jobs that are gone."""
    agents = Path.home() / "Library/LaunchAgents"
    domain = f"gui/{os.getuid()}"
    wanted = {}
    for name in all_jobs():
        job = load(name)
        if "every" in job:
            wanted[f"dev.reps.{name}"] = job
    for plist in agents.glob("dev.reps.*.plist"):
        if plist.stem not in wanted:
            subprocess.run(["launchctl", "bootout", f"{domain}/{plist.stem}"], capture_output=True)
            plist.unlink()
            print(f"removed {plist.stem}")
    (HOME / "launchd").mkdir(parents=True, exist_ok=True)
    for label, job in wanted.items():
        plist = agents / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": label,
            "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "run", job["name"]],
            "StartInterval": seconds(job["every"]),
            # launchd starts with a bare PATH; keep the one agents were found on
            "EnvironmentVariables": {"PATH": os.environ["PATH"], "REPS_HOME": str(HOME), "REPS_JOBS": str(JOBS)},
            "StandardOutPath": str(HOME / "launchd" / f"{job['name']}.log"),
            "StandardErrorPath": str(HOME / "launchd" / f"{job['name']}.log"),
        }))
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
        print(f"installed {label} every {job['every']}")


def run_dirs(name):
    d = HOME / "runs" / name
    return sorted(p for p in d.iterdir() if p.is_dir()) if d.exists() else []


def running(name):
    lock = HOME / "locks" / f"{name}.lock"
    if not lock.exists():
        return False
    with open(lock) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def status(run_dir, is_last_and_running=False):
    meta = run_dir / "meta.json"
    if not meta.exists():
        return {"status": "running" if is_last_and_running else "crashed"}
    m = json.loads(meta.read_text())
    m["status"] = ("timeout" if m.get("timed_out") else "error" if m.get("error")
                   else "ok" if m["exit_code"] == 0
                   else "killed" if m["exit_code"] in (-9, -15, 137, 143)  # stopped from outside
                   else f"failed({m['exit_code']})")
    return m


def job_json(name):
    """One job as data: its settings, where it lives, and how its runs went."""
    try:
        job = load(name)
    except SystemExit as e:  # a broken JOB.md is reported, not fatal to the whole list
        return {"name": name, "dir": str(JOBS / name), "error": str(e)}
    runs = run_dirs(name)
    stats = [status(r, i == len(runs) - 1 and running(name)) for i, r in enumerate(runs)]
    ok = sum(st["status"] == "ok" for st in stats)
    busy = sum(st["status"] == "running" for st in stats)
    wt = HOME / "worktrees" / name
    body = job["md"].read_text().split("\n---\n", 1)[-1].strip()
    return {
        "name": name, "dir": str(job["md"].parent), "md": str(job["md"]), "prompt": body,
        **{k: job.get(k) for k in ("every", "agent", "timeout", "base")},
        "repo": str(job["repo"]) if job.get("repo") else None,
        "worktree": str(wt) if job.get("repo") and wt.exists() else None,
        "scheduled": (Path.home() / f"Library/LaunchAgents/dev.reps.{name}.plist").exists(),
        "running": running(name),
        "runs": len(runs), "ok": ok, "failed": len(runs) - ok - busy,
        "last": run_json(runs[-1], stats[-1]) if runs else None,
    }


def run_json(run_dir, st):
    return {
        "id": run_dir.name, "dir": str(run_dir), "status": st["status"],
        **{k: st.get(k) for k in ("started", "ended", "exit_code", "sync", "error", "worktree")},
        "new_commits": "head_after" in st and st.get("head_before") != st.get("head_after"),
        "summary": (run_dir / "summary.md").exists(),
    }


def list_jobs():
    print(f"{'JOB':20} {'EVERY':6} {'SCHEDULED':9} {'RUNS':>4} {'OK':>3} {'FAIL':>4}  LAST")
    for name in all_jobs():
        job, runs = load(name), run_dirs(name)
        stats = [status(r, i == len(runs) - 1 and running(name)) for i, r in enumerate(runs)]
        ok = sum(st["status"] == "ok" for st in stats)
        busy = sum(st["status"] == "running" for st in stats)
        scheduled = (Path.home() / f"Library/LaunchAgents/dev.reps.{name}.plist").exists()
        last = f"{runs[-1].name} {stats[-1]['status']}" if runs else "never ran"
        print(f"{name:20} {job.get('every', '-'):6} {'yes' if scheduled else 'no':9} "
              f"{len(runs):>4} {ok:>3} {len(runs) - ok - busy:>4}  {last}")


def list_runs(name):
    load(name)
    runs = run_dirs(name)
    if not runs:
        sys.exit(f"{name}: no runs yet")
    for i, r in enumerate(runs):
        st = status(r, i == len(runs) - 1 and running(name))
        took = f"{(st['ended'] - st['started']) / 60:.1f}m" if "ended" in st else "-"
        moved = st.get("head_before") != st.get("head_after") and "head_after" in st
        print(f"{r.name}  {st['status']:11} {took:>7}  {'new commits' if moved else '':11}  {st.get('sync') or st.get('error', '')}")


def logs(name, run_id=None, as_json=False):
    runs = run_dirs(name)
    if not runs:
        sys.exit(f"{name}: no runs yet")
    last = next((r for r in runs if r.name == run_id), None) if run_id else runs[-1]
    if not last:
        sys.exit(f"{name}: no run {run_id}")
    if as_json:
        read = lambda f: (last / f).read_text(errors="replace") if (last / f).exists() else None
        output = read("output.log")
        st = status(last, last == runs[-1] and running(name))
        print(json.dumps({**run_json(last, st), "meta": st, "summary": read("summary.md"),
                          "output": "".join(map(readable, output.splitlines())) if output is not None else None}))
        return
    for f in ("meta.json", "summary.md", "output.log"):
        if (last / f).exists():
            print(f"==> {last / f}")
            for line in (last / f).read_text().splitlines():
                print(readable(line) if f == "output.log" else line + "\n", end="")


def readable(line):
    """Turn one stream-json event (claude --output-format stream-json) into text; other lines pass through."""
    try:
        ev = json.loads(line)
    except ValueError:
        return line + "\n"
    if not isinstance(ev, dict):
        return line + "\n"
    if ev.get("type") == "result":
        return f"\n[result] {ev.get('result', '')}\n"
    if ev.get("type") != "assistant":
        return ""  # hooks, tool results, system noise
    out = ""
    for c in ev.get("message", {}).get("content", []):
        if c.get("type") == "text":
            out += c["text"] + "\n"
        elif c.get("type") == "tool_use":
            out += f"  > {c['name']} {json.dumps(c.get('input', {}))[:200]}\n"
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    run_id = None
    if "--run" in args:
        i = args.index("--run")
        run_id = args[i + 1] if i + 1 < len(args) else sys.exit("--run needs a run id")
        del args[i:i + 2]
    cmd, *rest = args or ["help"]
    if cmd == "run" and rest:
        sys.exit(run(rest[0]))
    elif cmd == "logs" and rest:
        logs(rest[0], run_id, as_json)
    elif cmd == "install":
        install()
    elif cmd == "version":
        print(git(HERE, "rev-parse", "--short", "HEAD").stdout.strip() or "unknown")
    elif cmd == "update":
        # pull first so the shell never reads install.sh while git rewrites it
        subprocess.run(["git", "-C", str(HERE), "pull", "--ff-only", "--quiet"], check=True)
        sys.exit(subprocess.run(["sh", str(HERE / "install.sh")]).returncode)
    elif cmd == "list":
        print(json.dumps([job_json(n) for n in all_jobs()])) if as_json else list_jobs()
    elif cmd == "runs" and rest:
        if as_json:
            load(rest[0])
            runs = run_dirs(rest[0])
            print(json.dumps([run_json(r, status(r, i == len(runs) - 1 and running(rest[0])))
                              for i, r in enumerate(runs)]))
        else:
            list_runs(rest[0])
    else:
        sys.exit(__doc__)
