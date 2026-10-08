---
repo: ~/dev/myrepo
agent: claude -p --permission-mode acceptEdits --allowedTools=Read,Edit,Write,Bash(git:*)
timeout: 30m
# every: 6h   # uncomment, then `reps.py install` to schedule it
---
Look for TODO comments added since your last run. Pick one small one,
fix it, and commit on this branch. Don't push.
