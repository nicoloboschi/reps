#!/bin/sh
# Install or update reps:  curl -fsSL https://raw.githubusercontent.com/nicoloboschi/reps/main/install.sh | sh
set -eu
REPS_HOME="${REPS_HOME:-$HOME/.reps}"
APP="$REPS_HOME/app"
REPO="${REPS_REPO:-https://github.com/nicoloboschi/reps.git}"

command -v git >/dev/null || { echo "reps needs git" >&2; exit 1; }
command -v python3 >/dev/null || { echo "reps needs python3" >&2; exit 1; }

if [ -d "$APP/.git" ]; then
  git -C "$APP" pull --ff-only --quiet
else
  mkdir -p "$REPS_HOME"
  git clone --quiet "$REPO" "$APP"
fi
mkdir -p "$REPS_HOME/jobs" "$HOME/.local/bin"
ln -sf "$APP/reps.py" "$HOME/.local/bin/reps"

# the skill goes to every agent that is set up on this machine
for dir in "$HOME/.claude" "$HOME/.codex" "$HOME/.agents" "$HOME/.gemini"; do
  if [ -d "$dir" ]; then
    rm -rf "$dir/skills/reps" && mkdir -p "$dir/skills" && cp -R "$APP/skill" "$dir/skills/reps"
    echo "skill -> $dir/skills/reps"
  fi
done

echo "reps $(git -C "$APP" rev-parse --short HEAD) installed"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to your PATH to use 'reps'";; esac
