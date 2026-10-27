#!/bin/bash
# Double-click to start FileHub. Keep this window open while you use it.
# It first fetches any updates made in the cloud (git pull), then starts FileHub.
# If there is no internet or the update fails, it simply starts the version you have.

cd "$(dirname "$0")" || exit 1

PORT=8765
STAMP=".filehub-running-version"   # which version the running FileHub was started from

# --- 1. Get updates (never blocks startup) ---------------------------------
current_version() { git rev-parse HEAD 2>/dev/null; }

update() {
  # git on a Mac needs the Apple command line tools; without them, skip quietly.
  if ! xcode-select -p >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
    echo "Note: couldn't check for updates (git isn't installed). Starting your current version."
    return
  fi
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "Note: this folder isn't linked to GitHub, so no updates. Starting your current version."
    return
  fi

  echo "Checking for updates..."
  # Run the pull in the background and give up after 20 seconds (e.g. slow or no internet).
  GIT_TERMINAL_PROMPT=0 git pull --ff-only --quiet >/tmp/filehub-update.log 2>&1 &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge 20 ]; then
      kill "$pid" 2>/dev/null
      wait "$pid" 2>/dev/null
      echo "Note: the update check took too long (no internet?). Starting your current version."
      return
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if wait "$pid"; then
    echo "FileHub is up to date."
  else
    echo "Note: couldn't get updates right now (no internet?). Starting your current version."
  fi
}

update
VERSION="$(current_version)"

# --- 2. Is FileHub already running? ----------------------------------------
filehub_pids() {
  # Only the FileHub server listening on our port, never any other program.
  for pid in $(lsof -nP -t -iTCP:$PORT -sTCP:LISTEN 2>/dev/null); do
    if ps -p "$pid" -o command= 2>/dev/null | grep -q "filehub.py"; then
      echo "$pid"
    fi
  done
}

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  RUNNING_VERSION="$(cat "$STAMP" 2>/dev/null)"
  PIDS="$(filehub_pids)"
  if [ -n "$VERSION" ] && [ "$RUNNING_VERSION" != "$VERSION" ] && [ -n "$PIDS" ]; then
    # It's an older version: stop it so the new one can start.
    echo "Restarting FileHub to use the new version..."
    kill $PIDS 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break
      sleep 0.5
    done
    if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
      kill -9 $PIDS 2>/dev/null
      sleep 1
    fi
  else
    # Already running the latest version: just open it.
    open "http://127.0.0.1:$PORT"
    exit 0
  fi
fi

# --- 3. Start FileHub -------------------------------------------------------
[ -n "$VERSION" ] && echo "$VERSION" > "$STAMP"
python3 filehub.py
