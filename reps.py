#!/usr/bin/env python3
"""reps: run coding agents on a schedule, each job in its own stable git worktree.

A job is a folder ~/.reps/jobs/<name>/ with a JOB.md:

    ---
    repo: ~/dev/myrepo
    every: 6h
    agent: claude -p --permission-mode acceptEdits
    timeout: 45m          # optional, default 1h
    base: origin/main     # optional, default origin/HEAD or the repo's current branch
    ---
    What the agent should do, in plain markdown.

Usage: reps run <job> | install | list | logs <job> | version | update
"""
import fcntl, json, os, plistlib, re, shlex, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("REPS_HOME", Path.home() / ".reps"))
JOBS = Path(os.environ.get("REPS_JOBS", HOME / "jobs"))
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


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
    for k in ("repo", "agent"):
        if not job.get(k):
            sys.exit(f"{md}: '{k}' is required")
    job["repo"] = Path(job["repo"]).expanduser()
    return job


def all_jobs():
    return sorted(p.parent.name for p in JOBS.glob("*/JOB.md"))


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)


def prepare_worktree(job):
    """Create the job's worktree once, then keep it on top of base. Returns (path, note)."""
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
        f"You are in a git worktree on branch reps/{name}; it is kept between runs.\n"
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


def last_run(name):
    runs = sorted((HOME / "runs" / name).glob("*/")) if (HOME / "runs" / name).exists() else []
    return runs[-1] if runs else None


def list_jobs():
    for name in all_jobs():
        job, last = load(name), last_run(name)
        status = "never ran"
        if last:
            meta = last / "meta.json"
            status = (f"last {last.name} exit {json.loads(meta.read_text())['exit_code']}"
                      if meta.exists() else f"last {last.name} running")
        print(f"{name:20} every {job.get('every', '-'):6} {status}")


def logs(name):
    last = last_run(name)
    if not last:
        sys.exit(f"{name}: no runs yet")
    for f in ("meta.json", "summary.md", "output.log"):
        if (last / f).exists():
            print(f"==> {last / f}\n{(last / f).read_text()}")


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:] or ["help"]
    if cmd == "run" and rest:
        sys.exit(run(rest[0]))
    elif cmd == "logs" and rest:
        logs(rest[0])
    elif cmd == "install":
        install()
    elif cmd == "version":
        print(git(HERE, "rev-parse", "--short", "HEAD").stdout.strip() or "unknown")
    elif cmd == "update":
        # pull first so the shell never reads install.sh while git rewrites it
        subprocess.run(["git", "-C", str(HERE), "pull", "--ff-only", "--quiet"], check=True)
        sys.exit(subprocess.run(["sh", str(HERE / "install.sh")]).returncode)
    elif cmd == "list":
        list_jobs()
    else:
        sys.exit(__doc__)
