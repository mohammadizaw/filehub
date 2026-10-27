#!/bin/bash
# Double-click to start FileHub. Keep this window open while you use it.
cd "$(dirname "$0")"
if lsof -i :8765 >/dev/null 2>&1; then
  open "http://127.0.0.1:8765"
else
  python3 filehub.py
fi
