#!/usr/bin/env python3
"""Builds FileHub.app: a double-clickable Mac app that starts FileHub (no Terminal window).
Run on any computer with Python 3 and Pillow:  python3 tools/make_app.py
The app only needs the FileHub folder next to it (or anywhere on the Mac: it finds it)."""
import os, struct, io, plistlib, stat
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(HERE, "FileHub.app", "Contents")

LAUNCHER = r'''#!/bin/bash
# FileHub.app: starts FileHub without a Terminal window. Everything it prints goes to ~/Library/Logs/FileHub.log
APP="$(cd "$(dirname "$0")/../.." && pwd)"
PARENT="$(dirname "$APP")"
SAVED="$HOME/Library/Application Support/FileHub/folder"
LOG="$HOME/Library/Logs/FileHub.log"
mkdir -p "$(dirname "$SAVED")" "$(dirname "$LOG")"

find_folder() {
  [ -f "$PARENT/filehub.py" ] && { echo "$PARENT"; return; }
  if [ -f "$SAVED" ]; then p="$(cat "$SAVED")"; [ -f "$p/filehub.py" ] && { echo "$p"; return; }; fi
  for d in "$HOME/filehub" "$HOME/FileHub" "$HOME/Documents/filehub" "$HOME/Desktop/filehub" "$HOME/Downloads/filehub" "$HOME/Documents/GitHub/filehub"; do
    [ -f "$d/filehub.py" ] && { echo "$d"; return; }
  done
  mdfind -name filehub.py 2>/dev/null | grep '/filehub.py$' | head -1 | sed 's#/filehub.py$##'
}

FOLDER="$(find_folder)"
if [ -z "$FOLDER" ]; then
  osascript -e 'display dialog "FileHub could not find its folder (the one with filehub.py). Keep FileHub.app inside that folder, or open it from there once." buttons {"OK"} default button 1 with title "FileHub" with icon stop' >/dev/null 2>&1
  exit 1
fi
echo "$FOLDER" > "$SAVED"
echo "--- $(date) starting from $FOLDER" >> "$LOG"
cd "$FOLDER" || exit 1
exec /bin/bash "./Open FileHub.command" >> "$LOG" 2>&1
'''

def icon(size):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(s * 0.06)
    r = int(s * 0.22)
    # soft shadow
    sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([pad, pad + int(s*0.02), s - pad, s - pad + int(s*0.02)], r, fill=(0, 0, 0, 70))
    sh = sh.filter(ImageFilter.GaussianBlur(s * 0.02))
    img.alpha_composite(sh)
    # gradient tile (coral to orange, like FileHub's accent)
    grad = Image.new("RGBA", (s, s))
    gp = grad.load()
    for y in range(s):
        for x in range(s):
            t = (x + y) / (2 * s)
            gp[x, y] = (int(255 - 10 * t), int(122 + 50 * t), int(69 + 40 * t), 255)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, s - pad, s - pad], r, fill=255)
    img.paste(grad, (0, 0), mask)
    # folder: white tab + body
    d = ImageDraw.Draw(img)
    fx0, fy0, fx1, fy1 = int(s*0.20), int(s*0.30), int(s*0.80), int(s*0.72)
    tab_w = int(s*0.24); tab_h = int(s*0.07)
    d.rounded_rectangle([fx0, fy0 - tab_h, fx0 + tab_w, fy0 + tab_h], int(s*0.03), fill=(255, 255, 255, 235))
    d.rounded_rectangle([fx0, fy0, fx1, fy1], int(s*0.05), fill=(255, 255, 255, 245))
    # hub: three dots joined, in accent colour on the folder
    cx, cy = (fx0 + fx1) // 2, int(s*0.53)
    dr = max(2, int(s*0.035)); lw = max(2, int(s*0.016))
    pts = [(cx - int(s*0.14), cy + int(s*0.05)), (cx, cy - int(s*0.06)), (cx + int(s*0.14), cy + int(s*0.05))]
    for p in pts:
        d.line([p, (cx, cy + int(s*0.02))], fill=(255, 122, 69, 255), width=lw)
    for p in pts + [(cx, cy + int(s*0.02))]:
        d.ellipse([p[0]-dr, p[1]-dr, p[0]+dr, p[1]+dr], fill=(255, 122, 69, 255))
    return img

def icns(sizes):
    """An .icns file holding PNG images (what modern macOS expects)."""
    chunks = b""
    types = {16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07", 256: b"ic08", 512: b"ic09", 1024: b"ic10"}
    for sz in sizes:
        b = io.BytesIO(); icon(sz).save(b, "PNG"); data = b.getvalue()
        chunks += types[sz] + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(chunks)) + chunks

os.makedirs(os.path.join(APP, "MacOS"), exist_ok=True)
os.makedirs(os.path.join(APP, "Resources"), exist_ok=True)
with open(os.path.join(APP, "Info.plist"), "wb") as fh:
    plistlib.dump({
        "CFBundleName": "FileHub", "CFBundleDisplayName": "FileHub", "CFBundleIdentifier": "com.mohammadizaw.filehub",
        "CFBundleVersion": "1.0", "CFBundleShortVersionString": "1.0", "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "FileHub", "CFBundleIconFile": "AppIcon", "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True, "LSApplicationCategoryType": "public.app-category.utilities",
    }, fh)
launcher = os.path.join(APP, "MacOS", "FileHub")
with open(launcher, "w") as fh:
    fh.write(LAUNCHER)
os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
with open(os.path.join(APP, "Resources", "AppIcon.icns"), "wb") as fh:
    fh.write(icns([16, 32, 64, 128, 256, 512, 1024]))
icon(512).save(os.path.join(HERE, "tools", "icon-preview.png"))
print("built", os.path.dirname(APP))
