#!/usr/bin/env python3
"""FileHub - a friendly dashboard for the files on your Mac.

Run:  python3 filehub.py
Then it opens http://127.0.0.1:8765 in your browser.
Only your own computer can reach it. Nothing is moved, renamed or deleted.
"""
import json
import plistlib
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
import urllib.request
import urllib.error
import struct
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

PORT = 8765
HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
THUMBS = os.path.join(tempfile.gettempdir(), "filehub-thumbs")
os.makedirs(THUMBS, exist_ok=True)

# Folders and files we never show (system stuff, code caches, app internals).
SKIP_PARTS = ("/Library/", "/.", "/node_modules/", ".app/", "/Music/Music/",
              "/Photos Library", "/__pycache__/", "/venv/", "/.venv/", "/tmp/",
              "/frames/", "/build/", "/dist/", "/Applications/")
SKIP_EXT = {".app", ".crwebloc", ".webloc", ".ds_store", ".lnk", ".plist", ".log",
            ".pyc", ".lock", ".map", ".json", ".js", ".ts", ".tsx", ".css", ".py",
            ".sqlite", ".db", ".icloud", ".crdownload", ".part"}
DOC_EXT = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".key",
           ".pages", ".numbers", ".txt", ".md", ".rtf", ".png", ".jpg", ".jpeg", ".heic",
           ".gif", ".webp", ".svg", ".mp4", ".mov", ".m4v", ".mp3", ".m4a", ".wav",
           ".zip", ".html", ".epub"}

TYPE_GROUPS = {
    "PDF": {".pdf"},
    "Word": {".doc", ".docx", ".pages", ".rtf", ".txt", ".md"},
    "Slides": {".ppt", ".pptx", ".key"},
    "Sheets": {".xls", ".xlsx", ".csv", ".numbers"},
    "Images": {".png", ".jpg", ".jpeg", ".heic", ".gif", ".webp", ".svg"},
    "Video": {".mp4", ".mov", ".m4v"},
    "Audio": {".mp3", ".m4a", ".wav"},
    "Archives": {".zip"},
}


def file_type(ext):
    for name, exts in TYPE_GROUPS.items():
        if ext in exts:
            return name
    return "Other"


VOLUMES = "/Volumes"
TRASH = os.path.join(HOME, ".Trash")


def drives():
    """Plugged-in USB sticks and external disks (what the Mac shows under /Volumes)."""
    out = []
    try:
        names = sorted(os.listdir(VOLUMES), key=str.lower)
    except OSError:
        return out
    for n in names:
        full = os.path.join(VOLUMES, n)
        if n.startswith(".") or not os.path.isdir(full):
            continue
        if os.path.realpath(full) == "/":  # the Mac's own startup disk
            continue
        try:
            st = os.statvfs(full)
            free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        except OSError:
            free = total = 0
        out.append({"name": n, "path": full, "icon": "drive", "free": free, "total": total})
    return out


def drive_roots():
    return [d["path"] for d in drives()]


def on_drive(path):
    return any(path == r or path.startswith(r + "/") for r in drive_roots())


def is_root(path):
    """Home itself or the top of a plugged-in drive."""
    return path == HOME or path in drive_roots()


def inside(path, allow_root=False):
    """Inside your home folder or on a plugged-in drive (never anywhere else on the Mac)."""
    path = os.path.realpath(path)
    if allow_root and is_root(path):
        return True
    return path.startswith(HOME + "/") or on_drive(path)


def allowed(path):
    path = os.path.realpath(path)
    return os.path.exists(path) and inside(path)


ICLOUD = os.path.join(HOME, "Library/Mobile Documents/com~apple~CloudDocs")
MAIN_FOLDERS = [os.path.join(HOME, d) for d in ("Desktop", "Documents", "Downloads")] + [ICLOUD]
DOC_TYPES = ["com.adobe.pdf", "public.presentation", "public.spreadsheet",
             "org.openxmlformats.wordprocessingml.document", "com.microsoft.word.doc",
             "com.apple.iwork.pages.sffpages", "public.image", "public.movie",
             "public.audio", "public.zip-archive", "public.plain-text"]


_code_dirs = {}
CODE_MARKERS = (".git", "package.json", "pyproject.toml", "Podfile", "pubspec.yaml",
                "Cargo.toml", "go.mod", ".xcodeproj")


def in_code_project(path):
    """True when the file sits inside a programming project (those have their own tools)."""
    d = os.path.dirname(path)
    chain = []
    while d.startswith(HOME + "/") and d not in _code_dirs:
        chain.append(d)
        d = os.path.dirname(d)
    result = _code_dirs.get(d, False)
    for c in reversed(chain):
        if not result:
            try:
                names = os.listdir(c)
            except OSError:
                names = []
            result = any(n in CODE_MARKERS or n.endswith(".xcodeproj") for n in names)
        _code_dirs[c] = result
    return result


def wanted(path):
    if not path.startswith(HOME + "/"):
        return False
    check = path.replace(ICLOUD, HOME + "/iCloud Drive", 1)
    if any(p in check for p in SKIP_PARTS):
        return False
    ext = os.path.splitext(path)[1].lower()
    return ext not in SKIP_EXT and os.path.isfile(path) and not in_code_project(path)


# ---------- "Family" detection: files that belong to each other ----------
# e.g. "Lesson 3.pdf", "Lesson 3_part1.pdf", "Lesson 3 - page 2.pdf",
# "Lesson 3 (1).pdf", "Lesson 3-marked.pdf", "Lesson 3 copy.pdf" -> one family.
SUFFIX_RE = [
    r"\s*\(\d+\)$",                                   # (1)
    r"[\s_-]*copy(\s*\d+)?$",                         # copy, copy 2
    r"[\s_-]*(version|form|set|group)[\s_-]?[a-z]$",  # VersionA, Form B
    r"[\s_-]*(part|pt|page|pages|pg|p|split|section|sec|chunk|q|question|"
    r"chapter|ch|unit|slide|sheet|v|ver|version|final|draft|edited|marked|"
    r"results|answers|answer key|key|solutions?|notes|summary|compressed|"
    r"signed|scan|scanned|merged|ocr)[\s_.-]*\d*([\s_-]*(of|to|-)[\s_-]*\d+)?$",
    r"[\s_-]*[(\[]?(\d{1,3}\s*(to|-)\s*\d{1,3})[)\]]?$",  # pages range "3-5"
]
NUMBER_RE = r"[_-]+\d{1,3}$"  # "_1", "-2" (but keeps "Lesson 3")


def family_key(name):
    stem = os.path.splitext(name)[0].lower().strip()
    stem = stem.replace("—", "-").replace("–", "-")
    for _ in range(4):
        before = stem
        for rx in SUFFIX_RE:
            stem = re.sub(rx, "", stem).strip(" _-.")
        if stem == before:
            break
    stem = re.sub(NUMBER_RE, "", stem).strip(" _-.")
    stem = re.sub(r"[\s_.-]+", " ", stem).strip()
    return stem if len(stem) >= 3 else None


# ---------- Page counts ----------
PAGE_WORD = {".pptx": "slides", ".ppt": "slides", ".key": "slides",
             ".xlsx": "sheets", ".xls": "sheets", ".numbers": "sheets"}


def office_pages(path):
    """Spotlight only counts PDF pages, so read Office files directly."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".pptx", ".docx", ".xlsx"):
        return None
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if ext == ".pptx":
                return sum(1 for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)) or None
            if ext == ".xlsx":
                return sum(1 for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)) or None
            if "docProps/app.xml" in names:
                m = re.search(rb"<Pages>(\d+)</Pages>", z.read("docProps/app.xml"))
                return int(m.group(1)) if m else None
    except Exception:
        return None
    return None


# ---------- Spotlight helpers ----------
def mdfind(query, onlyin=HOME, limit=None):
    try:
        out = subprocess.run(["mdfind", "-onlyin", onlyin, query],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    paths = [p for p in out.splitlines() if wanted(p)]
    return paths[:limit] if limit else paths


# mdls prints attributes in alphabetical order, whatever order we ask for.
ATTRS = sorted(["kMDItemLastUsedDate", "kMDItemUseCount", "kMDItemWhereFroms", "kMDItemKind",
                "kMDItemNumberOfPages"])


def parse_date(v):
    v = v.strip()
    if not v or v == "(null)":
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def domain(url):
    m = re.match(r"https?://([^/:]+)", url)
    if not m:
        return None
    host = m.group(1).lower()
    if host in ("127.0.0.1", "localhost"):
        return "Local app"
    return re.sub(r"^www\.", "", host)


def source_of(path, froms):
    origin = origin_from_stamp(froms)
    if origin:
        return "Split from " + os.path.basename(origin["parent"])
    if froms:
        for u in froms:
            d = domain(u)
            if d:
                return "Web · " + d
    rel = os.path.relpath(path.replace(ICLOUD, HOME + "/iCloud Drive", 1), HOME).split(os.sep)
    if len(rel) <= 1:
        return "Home"
    if len(rel) == 2:
        return rel[0]
    return rel[0] + " › " + rel[1]


_meta_cache = {}


def metadata(paths):
    """Return a dict per path with dates, use count, source, family, etc."""
    todo = [p for p in paths if p not in _meta_cache]
    for i in range(0, len(todo), 150):
        chunk = todo[i:i + 150]
        args = ["mdls"]
        for a in ATTRS:
            args += ["-name", a]
        try:
            raw = subprocess.run(args + ["-raw"] + chunk, capture_output=True,
                                 text=True, timeout=30).stdout.split("\0")
        except Exception:
            raw = []
        for j, p in enumerate(chunk):
            vals = raw[j * len(ATTRS):(j + 1) * len(ATTRS)]
            vals += ["(null)"] * (len(ATTRS) - len(vals))
            v = dict(zip(ATTRS, (x.strip() for x in vals)))
            froms = re.findall(r'"([^"]+)"', v["kMDItemWhereFroms"])
            try:
                count = int(v["kMDItemUseCount"])
            except ValueError:
                count = 0
            kind = v["kMDItemKind"]
            try:
                pages = int(float(v["kMDItemNumberOfPages"]))
            except ValueError:
                pages = office_pages(p)
            _meta_cache[p] = {"pages": pages, "lastUsed": parse_date(v["kMDItemLastUsedDate"]),
                              "useCount": count, "froms": froms,
                              "kind": "" if kind == "(null)" else kind}
    result = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        m = _meta_cache.get(p, {})
        name = os.path.basename(p)
        ext = os.path.splitext(name)[1].lower()
        parent, children = links_for(p, m.get("froms"))
        root = root_of(p, parent)
        result.append({
            "splitFrom": parent and {"path": parent["parent"], "name": os.path.basename(parent["parent"]),
                                     "from": parent["from"], "to": parent["to"]},
            "splitInto": children,
            "path": p,
            "name": name,
            "folder": os.path.dirname(p).replace(HOME, "~", 1),
            "ext": ext.lstrip("."),
            "type": file_type(ext),
            "kind": m.get("kind", ""),
            "size": st.st_size,
            "modified": st.st_mtime,
            "created": getattr(st, "st_birthtime", st.st_ctime),
            "lastUsed": m.get("lastUsed") or None,
            "useCount": m.get("useCount", 0),
            "source": source_of(p, m.get("froms")),
            "url": (m.get("froms") or [None])[0],
            # Split files always join their original's group, whatever they're called.
            "family": family_key(os.path.basename(root)) or ("file:" + root),
            "pages": m.get("pages"),
            "pageWord": PAGE_WORD.get(ext, "pages"),
        })
    return result


# ---------- Data views ----------
_library = {"time": 0, "files": []}
_lock = threading.Lock()


def library(days=60, force=False):
    """All recently used / changed / added files, refreshed every 2 minutes."""
    with _lock:
        if not force and time.time() - _library["time"] < 120 and _library["files"]:
            return _library["files"]
        t = "$time.today(-%d)" % days
        def changed(types):
            cond = " || ".join('kMDItemContentTypeTree == "%s"' % c for c in types)
            return "(kMDItemDateAdded >= %s || kMDItemFSContentChangeDate >= %s) && (%s)" % (t, t, cond)
        # Documents often holds code projects with huge numbers of generated images,
        # so there we only look for documents (images still show up once you open them).
        no_media = [c for c in DOC_TYPES if c not in ("public.image", "public.movie", "public.audio")]
        jobs = [("kMDItemLastUsedDate >= %s" % t, HOME)]
        for d in MAIN_FOLDERS:
            if os.path.isdir(d):
                jobs.append((changed(no_media if d.endswith("/Documents") else DOC_TYPES), d))
        results = [None] * len(jobs)

        def run(i, q, d):
            results[i] = mdfind(q, onlyin=d)
        threads = [threading.Thread(target=run, args=(i, q, d)) for i, (q, d) in enumerate(jobs)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        paths = list(dict.fromkeys(p for r in results for p in (r or [])))
        # Spotlight can lag a few seconds: skip what's gone, add what FileHub itself just made or moved
        paths += [p for p in list(CREATED) + list(JUST_MOVED) if os.path.isfile(p)]
        paths = list(dict.fromkeys(p for p in paths if os.path.splitext(p)[1].lower() in DOC_EXT and os.path.exists(p)))
        paths.sort(key=lambda p: -os.path.getmtime(p) if os.path.exists(p) else 0)
        files = metadata(paths[:3000])
        _library.update(time=time.time(), files=files)
        return files


IGNORE_PROCS = {"mds", "mds_stores", "mdworker", "mdworker_shared", "mdwrite",
                "Finder", "QuickLookUIService", "quicklookd", "fseventsd",
                "Python", "python3", "lsof", "bird", "cloudd", "revisiond",
                "photoanalysisd", "corespotlightd", "Spotlight", "mediaanalysisd"}


def system_proc(proc):
    return (proc in IGNORE_PROCS or proc.startswith("com.") or
            any(w in proc for w in ("Extension", "Agent", "Service", "Helper", "Wallpaper",
                                    "Virtualiz", "mdworker", "Spotlight")))


def open_files():
    """Documents currently held open by apps (Word, PowerPoint, Preview...)."""
    try:
        out = subprocess.run(["lsof", "-u", str(os.getuid()), "-Fcn"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    found, proc = {}, ""
    for line in out.splitlines():
        if line.startswith("c"):
            proc = line[1:]
        elif line.startswith("n/") and not system_proc(proc):
            p = line[1:]
            if os.path.splitext(p)[1].lower() in DOC_EXT and wanted(p):
                found.setdefault(p, proc)
    files = metadata(list(found))
    for f in files:
        f["app"] = found.get(f["path"], "")
    return files


def search(q):
    q = q.replace('"', "")
    paths = mdfind('kMDItemFSName == "*%s*"cd || kMDItemTextContent == "%s"cd' % (q, q),
                   limit=300)
    return metadata(paths)


# ---------- The Mac's own look: its accent colour and its real file icons ----------
ACCENT_JS = """ObjC.import('AppKit');
function hex(c){ c = c.colorUsingColorSpace($.NSColorSpace.sRGBColorSpace);
  return '#' + [c.redComponent, c.greenComponent, c.blueComponent].map(v => ('0' + Math.round(v * 255).toString(16)).slice(-2)).join(''); }
function run(){
  $.NSApplication.sharedApplication;
  const out = {};
  ['NSAppearanceNameAqua', 'NSAppearanceNameDarkAqua'].forEach((n, i) => {
    $.NSApp.appearance = $.NSAppearance.appearanceNamed($[n]);
    out[i ? 'dark' : 'light'] = hex($.NSColor.controlAccentColor);
  });
  return JSON.stringify(out);
}"""
# Apple's documented accent colours, used when the script above can't run
ACCENT_TABLE = {-1: ("#8e8e93", "#98989d"), 0: ("#ff3b30", "#ff453a"), 1: ("#ff9500", "#ff9f0a"),
                2: ("#ffcc00", "#ffd60a"), 3: ("#28cd41", "#32d74b"), 4: ("#007aff", "#0a84ff"),
                5: ("#af52de", "#bf5af2"), 6: ("#ff2d55", "#ff375f")}
_sys_look = {"time": 0, "data": None}


def system_look():
    """The accent colour chosen in System Settings (light and dark versions) and the current mode."""
    if time.time() - _sys_look["time"] < 20 and _sys_look["data"]:
        return _sys_look["data"]
    light, dark = ACCENT_TABLE[4]
    try:
        out = subprocess.run(["defaults", "read", "-g", "AppleAccentColor"], capture_output=True, text=True, timeout=5)
        light, dark = ACCENT_TABLE.get(int(out.stdout.strip()), ACCENT_TABLE[4])
    except Exception:
        pass
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", ACCENT_JS], capture_output=True, text=True, timeout=10)
        got = json.loads(out.stdout.strip())
        if got.get("light") and got.get("dark"):
            light, dark = got["light"], got["dark"]
    except Exception:
        pass
    mode = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=5).stdout.strip()
    data = {"accent": {"light": light, "dark": dark}, "dark": mode.lower() == "dark"}
    _sys_look.update(time=time.time(), data=data)
    return data


ICON_JS = """ObjC.import('AppKit');
function run(argv) {
  const [path, size, out] = argv, n = parseInt(size, 10);
  const img = $.NSWorkspace.sharedWorkspace.iconForFile(path);
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(null, n, n, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.currentContext = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  img.drawInRectFromRectOperationFraction($.NSMakeRect(0, 0, n, n), $.NSZeroRect, $.NSCompositingOperationSourceOver, 1);
  $.NSGraphicsContext.restoreGraphicsState;
  rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(out, true);
  return 'ok';
}"""
ICONS_DIR = os.path.join(THUMBS, "mac-icons")
os.makedirs(ICONS_DIR, exist_ok=True)


def mac_icon(path, size=128):
    """The icon the Mac itself shows for this file (cached per file type)."""
    key = "folder" if os.path.isdir(path) else (os.path.splitext(path)[1].lower().lstrip(".") or "file")
    if path.endswith(".app"):
        key = "app-" + os.path.basename(path)
    out = os.path.join(ICONS_DIR, "%s-%d.png" % (re.sub(r"[^a-z0-9.-]", "_", key), size))
    if not os.path.exists(out):
        subprocess.run(["osascript", "-l", "JavaScript", "-e", ICON_JS, path, str(size), out],
                       capture_output=True, timeout=20)
    return out if os.path.exists(out) else None


def thumbnail(path, size=480):
    key = re.sub(r"[^A-Za-z0-9]", "_", path)[-150:] + "_%d" % size
    out_dir = os.path.join(THUMBS, key)
    if not os.path.isdir(out_dir) or not os.listdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
        subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", out_dir, path],
                       capture_output=True, timeout=20)
    imgs = os.listdir(out_dir) if os.path.isdir(out_dir) else []
    return os.path.join(out_dir, imgs[0]) if imgs else None


# ---------- PDF pages and splitting (uses the Mac's built-in PDFKit) ----------
PDF_JS = r'''
function run(argv) {
  ObjC.import("AppKit"); ObjC.import("Quartz");
  const doc = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[1]));
  if (!doc || doc.isNil()) return "ERR cannot open";
  if (doc.isLocked) return "ERR locked";
  const n = doc.pageCount;
  if (argv[0] === "thumbs") {
    const out = argv[2], size = parseInt(argv[3]), max = Math.min(n, 400);
    for (let i = 0; i < max; i++) {
      const img = doc.pageAtIndex(i).thumbnailOfSizeForBox($.NSMakeSize(size, size * 1.4), 1);
      const rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
      rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $())
         .writeToFileAtomically(out + "/p" + (i + 1) + ".png", true);
    }
    return String(n);
  }
  if (argv[0] === "page") {
    // One page in high quality, for reading and zooming.
    const i = parseInt(argv[2]) - 1, w = parseInt(argv[3]);
    if (!(i >= 0 && i < n)) return "ERR bad page";
    const page = doc.pageAtIndex(i), box = page.boundsForBox(1);
    const h = Math.round(w * box.size.height / box.size.width);
    const img = page.thumbnailOfSizeForBox($.NSMakeSize(w, h), 1);
    const rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
    rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $())
       .writeToFileAtomically(argv[4], true);
    return String(n);
  }
  if (argv[0] === "info") {
    // Page sizes (in PDF points) and rotation, so marks drawn on screen land in the right place.
    const out = [];
    for (let i = 0; i < n; i++) { const b = doc.pageAtIndex(i).boundsForBox(1); out.push({w: b.size.width, h: b.size.height, x: b.origin.x, y: b.origin.y, rot: doc.pageAtIndex(i).rotation}); }
    return "JSON" + JSON.stringify({pages: n, sizes: out});
  }
  if (argv[0] === "merge") {
    // argv[2] = JSON file with items [{path, from, to}], argv[3] = output
    const spec = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(argv[2], 4, null).js);
    const nd = $.PDFDocument.alloc.init;
    let k = 0;
    for (const it of spec.items) {
      const d = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(it.path));
      if (!d || d.isNil()) return "ERR cannot open " + it.path;
      if (d.isLocked) return "ERR locked " + it.path;
      const a = Math.max(1, it.from || 1), b = Math.min(d.pageCount, it.to || d.pageCount);
      for (let i = a; i <= b; i++) nd.insertPageAtIndex(d.pageAtIndex(i - 1).copy, k++);
    }
    if (!k) return "ERR no pages";
    return nd.writeToFile(argv[3]) ? String(k) : "ERR cannot save";
  }
  if (argv[0] === "annotate") {
    // argv[2] = JSON file with {annotations:[...]} (positions as fractions of the page as shown), argv[3] = output
    const spec = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(argv[2], 4, null).js);
    const col = (hex, alpha) => { const v = parseInt((hex || "#ffeb3b").slice(1), 16);
      return $.NSColor.colorWithRedGreenBlueAlpha(((v >> 16) & 255) / 255, ((v >> 8) & 255) / 255, (v & 255) / 255, alpha == null ? 1 : alpha); };
    let count = 0;
    for (const a of spec.annotations) {
      const page = doc.pageAtIndex(a.page - 1); if (!page || page.isNil()) continue;
      const box = page.boundsForBox(1), rot = ((page.rotation % 360) + 360) % 360;
      const W = box.size.width, H = box.size.height;
      const dw = (rot === 90 || rot === 270) ? H : W, dh = (rot === 90 || rot === 270) ? W : H;
      // fraction of the page as shown (origin top-left) -> PDF point (origin bottom-left, allowing for rotation)
      const pt = (fx, fy) => { const xd = fx * dw, yd = (1 - fy) * dh; let u, v;
        if (rot === 90) { u = W - yd; v = xd; } else if (rot === 180) { u = W - xd; v = H - yd; }
        else if (rot === 270) { u = yd; v = H - xd; } else { u = xd; v = yd; }
        return {x: box.origin.x + u, y: box.origin.y + v}; };
      const rectOf = r => { const p1 = pt(r.x, r.y), p2 = pt(r.x + r.w, r.y + r.h);
        const x = Math.min(p1.x, p2.x), y = Math.min(p1.y, p2.y);
        return $.NSMakeRect(x, y, Math.abs(p2.x - p1.x), Math.abs(p2.y - p1.y)); };
      const make = (bounds, type) => $.PDFAnnotation.alloc.initWithBoundsForTypeWithProperties(bounds, type, $());
      const border = (ann, w) => { const b = $.PDFBorder.alloc.init; b.lineWidth = w; ann.border = b; };
      const t = a.type;
      if (t === "highlight" || t === "underline" || t === "strike") {
        const r = rectOf(a.rect), sub = t === "highlight" ? $.PDFAnnotationSubtypeHighlight : t === "underline" ? $.PDFAnnotationSubtypeUnderline : $.PDFAnnotationSubtypeStrikeOut;
        const sel = page.selectionForRect(r);
        let lines = null;
        if (sel && !sel.isNil() && sel.string && sel.string.js.trim().length) lines = sel.selectionsByLine;
        if (lines && lines.count) {
          for (let i = 0; i < lines.count; i++) {
            const lb = lines.objectAtIndex(i).boundsForPage(page);
            if (lb.size.width < 1 || lb.size.height < 1) continue;
            const ann = make(lb, sub); ann.color = col(a.color);
            const w = lb.size.width, h = lb.size.height;
            ann.quadrilateralPoints = $([$.NSValue.valueWithPoint($.NSMakePoint(0, h)), $.NSValue.valueWithPoint($.NSMakePoint(w, h)),
                                         $.NSValue.valueWithPoint($.NSMakePoint(0, 0)), $.NSValue.valueWithPoint($.NSMakePoint(w, 0))]);
            page.addAnnotation(ann); count++;
          }
        } else if (t === "highlight") {  // no text there (a picture, a drawing): colour the area instead
          const ann = make(r, $.PDFAnnotationSubtypeSquare); ann.color = col(a.color, 0); ann.interiorColor = col(a.color, 0.35); border(ann, 0);
          page.addAnnotation(ann); count++;
        } else {
          const ann = make(r, $.PDFAnnotationSubtypeLine); const y = t === "underline" ? r.origin.y : r.origin.y + r.size.height / 2;
          ann.startPoint = $.NSMakePoint(r.origin.x, y); ann.endPoint = $.NSMakePoint(r.origin.x + r.size.width, y); ann.color = col(a.color); border(ann, 1.5);
          page.addAnnotation(ann); count++;
        }
      } else if (t === "text") {
        const r = rectOf(a.rect); const ann = make(r, $.PDFAnnotationSubtypeFreeText);
        ann.contents = a.text || ""; ann.font = $.NSFont.systemFontOfSize(a.size || 14); ann.fontColor = col(a.color || "#111111");
        ann.color = $.NSColor.clearColor; ann.alignment = 0; border(ann, 0);
        page.addAnnotation(ann); count++;
      } else if (t === "note") {
        // a sticky note: a yellow box with the text showing on the page (readable in Preview, Acrobat, iPad...)
        const r = rectOf(a.rect); const ann = make(r, $.PDFAnnotationSubtypeFreeText);
        ann.contents = a.text || ""; ann.font = $.NSFont.systemFontOfSize(a.size || 12); ann.fontColor = col("#1a1a1a");
        ann.color = col(a.color || "#ffeb3b"); ann.alignment = 0; border(ann, 0);
        page.addAnnotation(ann); count++;
      } else if (t === "pen") {
        if (!a.points || a.points.length < 2) continue;
        const pts = a.points.map(q => pt(q[0], q[1]));
        const xs = pts.map(q => q.x), ys = pts.map(q => q.y), pad = (a.width || 2) + 2;
        const bounds = $.NSMakeRect(Math.min(...xs) - pad, Math.min(...ys) - pad, Math.max(...xs) - Math.min(...xs) + 2 * pad, Math.max(...ys) - Math.min(...ys) + 2 * pad);
        const ann = make(bounds, $.PDFAnnotationSubtypeInk); ann.color = col(a.color, a.opacity == null ? 1 : a.opacity); border(ann, a.width || 2);
        const path = $.NSBezierPath.bezierPath; path.moveToPoint($.NSMakePoint(pts[0].x, pts[0].y));
        for (let i = 1; i < pts.length; i++) path.lineToPoint($.NSMakePoint(pts[i].x, pts[i].y));
        ann.addBezierPath(path); page.addAnnotation(ann); count++;
      } else if (t === "sig") {
        // a signature: pen strokes drawn into the box a.rect (strokes are 0..1 inside that box)
        if (!a.strokes || !a.strokes.length || !a.rect) continue;
        const r = rectOf(a.rect); const lw = Math.max(0.4, (a.width || 0.01) * a.rect.w * dw);
        const pad = lw + 2;
        const bounds = $.NSMakeRect(r.origin.x - pad, r.origin.y - pad, r.size.width + 2 * pad, r.size.height + 2 * pad);
        const ann = make(bounds, $.PDFAnnotationSubtypeInk); ann.color = col(a.color || "#111111", 1); border(ann, lw);
        for (const st of a.strokes) {
          if (!st.length) continue;
          const path = $.NSBezierPath.bezierPath;
          const q0 = pt(a.rect.x + st[0][0] * a.rect.w, a.rect.y + st[0][1] * a.rect.h);
          path.moveToPoint($.NSMakePoint(q0.x, q0.y));
          if (st.length === 1) path.lineToPoint($.NSMakePoint(q0.x + 0.01, q0.y));
          for (let i = 1; i < st.length; i++) { const q = pt(a.rect.x + st[i][0] * a.rect.w, a.rect.y + st[i][1] * a.rect.h); path.lineToPoint($.NSMakePoint(q.x, q.y)); }
          ann.addBezierPath(path);
        }
        page.addAnnotation(ann); count++;
      } else if (t === "rect" || t === "ellipse") {
        const r = rectOf(a.rect); const ann = make(r, t === "rect" ? $.PDFAnnotationSubtypeSquare : $.PDFAnnotationSubtypeCircle);
        ann.color = col(a.color); if (a.fill) ann.interiorColor = col(a.color, 0.25); border(ann, a.width || 2);
        page.addAnnotation(ann); count++;
      } else if (t === "line" || t === "arrow") {
        if (!a.points || a.points.length < 2) continue;
        const p1 = pt(a.points[0][0], a.points[0][1]), p2 = pt(a.points[1][0], a.points[1][1]), pad = 12;
        const bounds = $.NSMakeRect(Math.min(p1.x, p2.x) - pad, Math.min(p1.y, p2.y) - pad, Math.abs(p2.x - p1.x) + 2 * pad, Math.abs(p2.y - p1.y) + 2 * pad);
        const ann = make(bounds, $.PDFAnnotationSubtypeLine); ann.startPoint = p1; ann.endPoint = p2; ann.color = col(a.color); border(ann, a.width || 2);
        if (t === "arrow") ann.endLineStyle = 5;  // closed arrow head
        page.addAnnotation(ann); count++;
      }
    }
    return doc.writeToFile(argv[3]) ? String(count) : "ERR cannot save";
  }
  if (argv[0] === "rotate") {
    // argv[2] = JSON file with {rotations: {"3": 90, "5": 270}} (page number -> extra turn clockwise), argv[3] = output
    const spec = JSON.parse($.NSString.stringWithContentsOfFileEncodingError(argv[2], 4, null).js);
    let count = 0;
    for (const k of Object.keys(spec.rotations || {})) {
      const i = parseInt(k) - 1, d = ((parseInt(spec.rotations[k]) % 360) + 360) % 360;
      if (!(i >= 0 && i < n) || !d) continue;
      const page = doc.pageAtIndex(i); page.rotation = (page.rotation + d) % 360; count++;
    }
    return doc.writeToFile(argv[3]) ? String(count) : "ERR cannot save";
  }
  if (argv[0] === "shrink") {
    // Keep text and drawings as they are; only the pictures inside are re-saved smaller (macOS 13 or newer).
    const opts = $.NSMutableDictionary.alloc.init;
    const key = (name) => { try { const v = $[name]; if (v && !v.isNil()) return v; } catch (e) {} return $(name); };
    opts.setObjectForKey($(true), key("PDFDocumentOptimizeImagesForScreenOption"));
    opts.setObjectForKey($(true), key("PDFDocumentSaveImagesAsJPEGOption"));
    return doc.writeToFileWithOptions(argv[2], opts) ? String(n) : "ERR cannot save";
  }
  if (argv[0] === "compress") {
    // Make the file smaller: each page is redrawn as a JPEG picture at the chosen sharpness (dpi) and quality.
    const dpi = parseFloat(argv[3]) || 110, q = parseFloat(argv[4]) || 0.65, grey = argv[5] === "grey";
    const nd = $.PDFDocument.alloc.init;
    for (let i = 0; i < n; i++) {
      const page = doc.pageAtIndex(i), box = page.boundsForBox($.kPDFDisplayBoxCropBox), rot = page.rotation;
      const turned = (rot === 90 || rot === 270);
      const pw = turned ? box.size.height : box.size.width, ph = turned ? box.size.width : box.size.height;  // as displayed, in points
      const w = Math.max(8, Math.round(pw * dpi / 72)), h = Math.max(8, Math.round(ph * dpi / 72));
      const img = page.thumbnailOfSizeForBox($.NSMakeSize(w, h), $.kPDFDisplayBoxCropBox);
      let rep = $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation);
      if (grey) {
        const g = rep.bitmapImageRepByConvertingToColorSpaceRenderingIntent($.NSColorSpace.genericGrayColorSpace, 0);
        if (g && !g.isNil()) rep = g;
      }
      const props = $.NSMutableDictionary.alloc.init; props.setObjectForKey($(q), $.NSImageCompressionFactor);
      const jpg = rep.representationUsingTypeProperties($.NSBitmapImageFileTypeJPEG, props);
      const pic = $.NSImage.alloc.initWithData(jpg);
      const jrep = $.NSBitmapImageRep.imageRepWithData(jpg); jrep.size = $.NSMakeSize(pw, ph);  // the bitmap says "I am pw x ph points" (A4 stays A4)
      pic.removeRepresentation(pic.representations.objectAtIndex(0)); pic.addRepresentation(jrep);
      pic.size = $.NSMakeSize(pw, ph);
      const np = $.PDFPage.alloc.initWithImage(pic);
      if (!np || np.isNil()) return "ERR cannot draw page " + (i + 1);
      const mb = np.boundsForBox($.kPDFDisplayBoxMediaBox);
      if (Math.abs(mb.size.width - pw) > 1 || Math.abs(mb.size.height - ph) > 1) {
        // the page came out in pixels: scale it back to the paper size by giving it the right boxes
        np.setBoundsForBox($.NSMakeRect(0, 0, pw, ph), $.kPDFDisplayBoxMediaBox);
        np.setBoundsForBox($.NSMakeRect(0, 0, pw, ph), $.kPDFDisplayBoxCropBox);
      }
      nd.insertPageAtIndex(np, i);
    }
    return nd.writeToFile(argv[2]) ? String(n) : "ERR cannot save";
  }
  if (argv[0] === "split") {
    const a = parseInt(argv[2]), b = parseInt(argv[3]);
    if (!(a >= 1 && b >= a && b <= n)) return "ERR bad range";
    const nd = $.PDFDocument.alloc.init;
    for (let i = a; i <= b; i++) nd.insertPageAtIndex(doc.pageAtIndex(i - 1).copy, i - a);
    return nd.writeToFile(argv[4]) ? String(b - a + 1) : "ERR cannot save";
  }
  return "ERR unknown";
}
'''
PDF_JS_FILE = os.path.join(THUMBS, "pdftool.js")
with open(PDF_JS_FILE, "w") as fh:
    fh.write(PDF_JS)


def pdftool(*args):
    out = subprocess.run(["osascript", "-l", "JavaScript", PDF_JS_FILE] + [str(a) for a in args],
                         capture_output=True, text=True, timeout=600)
    res = (out.stdout or out.stderr).strip()
    if res.startswith("JSON"):
        return json.loads(res[4:])
    if res.startswith("ERR") or not res.isdigit():
        raise ValueError(res.replace("ERR ", "") or "PDF tool failed")
    return int(res)


def pdf_info(path):
    return pdftool("info", path)


def _spec_file(data):
    fd, tmp = tempfile.mkstemp(prefix="filehub-", suffix=".json", dir=THUMBS)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    return tmp


def merge_pdfs(items, name=None, dest=None):
    """Join several PDFs (or parts of them) into one NEW file."""
    items = [it for it in items if allowed(it.get("path", "")) and it["path"].lower().endswith(".pdf")]
    if len(items) < 1:
        raise ValueError("Choose at least one PDF.")
    folder = dest or os.path.dirname(items[0]["path"])
    folder = os.path.realpath(folder)
    if not inside(folder, True) or not os.path.isdir(folder):
        raise ValueError("Can't save in that folder.")
    name = (name or "").strip()
    if not name:
        stem = os.path.splitext(os.path.basename(items[0]["path"]))[0]
        name = "%s + %d more.pdf" % (stem, len(items) - 1) if len(items) > 1 else stem + " (merged).pdf"
    if "/" in name or ":" in name or name.startswith("."):
        raise ValueError("Please use a different name (no / or :).")
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    out = free_name(folder, name)
    spec = _spec_file({"items": items})
    try:
        count = pdftool("merge", spec, out)
    finally:
        os.unlink(spec)
    CREATED.add(out)
    _library["time"] = 0
    return {"path": out, "name": os.path.basename(out), "pages": count,
            "folder": nice_path(folder), "folderPath": folder}


BACKUPS = os.path.join(THUMBS, "backups")

# ---------- Saved signatures ----------
# Each signature is a small JSON file: the pen strokes (0..1 inside its own box), its shape and a name.
SIG_DIR = os.path.join(HOME, "Library", "Application Support", "FileHub", "signatures")
_sig_lock = threading.Lock()


def _sig_path(sid):
    if not re.fullmatch(r"[0-9a-f]{8,32}", sid or ""):
        raise ValueError("Unknown signature.")
    return os.path.join(SIG_DIR, sid + ".json")


def signatures():
    out = []
    try:
        names = sorted(os.listdir(SIG_DIR))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(SIG_DIR, n)) as fh:
                d = json.load(fh)
            d["id"] = n[:-5]
            out.append(d)
        except (OSError, ValueError):
            continue
    out.sort(key=lambda d: d.get("created", 0))
    return out


def add_signature(name, strokes, aspect, width):
    name = (name or "").strip()[:60] or "Signature"
    if not strokes or not isinstance(strokes, list):
        raise ValueError("Nothing was drawn.")
    strokes = [[[round(float(x), 4), round(float(y), 4)] for x, y in st] for st in strokes if len(st) >= 1]
    if len(json.dumps(strokes)) > 1_500_000:
        raise ValueError("That image is too detailed for a signature. Try a smaller or simpler picture.")
    sid = "%x%x" % (int(time.time() * 1000), os.getpid() % 4096)
    d = {"name": name, "strokes": strokes, "aspect": max(0.1, min(20.0, float(aspect or 3))),
         "width": max(0.001, min(0.2, float(width or 0.01))), "created": time.time()}
    with _sig_lock:
        os.makedirs(SIG_DIR, exist_ok=True)
        tmp = _sig_path(sid) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, _sig_path(sid))
    d["id"] = sid
    return d


def delete_signature(sid):
    with _sig_lock:
        try:
            os.unlink(_sig_path(sid))
        except FileNotFoundError:
            pass


def rename_signature(sid, name):
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the signature a name.")
    with _sig_lock:
        with open(_sig_path(sid)) as fh:
            d = json.load(fh)
        d["name"] = name
        with open(_sig_path(sid), "w") as fh:
            json.dump(d, fh)
    d["id"] = sid
    return d
os.makedirs(BACKUPS, exist_ok=True)


def annotate_pdf(path, annotations, mode="copy"):
    """Write marks (highlights, notes, drawings...) into a copy, or into the file itself with a backup."""
    path = os.path.realpath(path)
    if not allowed(path) or not path.lower().endswith(".pdf"):
        raise ValueError("That PDF isn't there any more.")
    if not annotations:
        raise ValueError("Nothing to save yet.")
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    spec = _spec_file({"annotations": annotations})
    try:
        if mode == "replace":
            tmp = os.path.join(folder, ".%s.filehub-tmp.pdf" % stem)
            count = pdftool("annotate", path, spec, tmp)
            backup = os.path.join(BACKUPS, "%d-%s" % (int(time.time()), name))
            shutil.copy2(path, backup)
            os.replace(tmp, path)
            _library["time"] = 0
            return {"path": path, "name": name, "count": count, "backup": backup, "mode": "replace"}
        out = free_name(folder, "%s - annotated.pdf" % stem)
        count = pdftool("annotate", path, spec, out)
        CREATED.add(out)
        _library["time"] = 0
        return {"path": out, "name": os.path.basename(out), "count": count, "mode": "copy"}
    finally:
        os.unlink(spec)


def rotate_pdf(path, rotations, mode="copy"):
    """Turn pages (90/180/270 clockwise) into a copy, or into the file itself with a backup."""
    path = os.path.realpath(path)
    if not allowed(path) or not path.lower().endswith(".pdf"):
        raise ValueError("That PDF isn't there any more.")
    rotations = {str(int(k)): int(v) % 360 for k, v in (rotations or {}).items() if int(v) % 360}
    if not rotations:
        raise ValueError("No pages were turned.")
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    spec = _spec_file({"rotations": rotations})
    try:
        if mode == "replace":
            tmp = os.path.join(folder, ".%s.filehub-tmp.pdf" % stem)
            count = pdftool("rotate", path, spec, tmp)
            backup = os.path.join(BACKUPS, "%d-%s" % (int(time.time()), name))
            shutil.copy2(path, backup)
            os.replace(tmp, path)
            _library["time"] = 0
            return {"path": path, "name": name, "count": count, "backup": backup, "mode": "replace"}
        out = free_name(folder, "%s - rotated.pdf" % stem)
        count = pdftool("rotate", path, spec, out)
        CREATED.add(out)
        _library["time"] = 0
        return {"path": out, "name": os.path.basename(out), "count": count, "mode": "copy"}
    finally:
        os.unlink(spec)


# ---------- Scanning from network printers / scanners (eSCL, the "AirScan" standard) ----------
# Most network scanners made since ~2015 (HP, Canon, Epson, Brother, Xerox...) answer eSCL over HTTP.
# FileHub finds them with Bonjour (dns-sd), asks what they can do, runs the scan and keeps the pages
# until you choose what to make of them: JPEG, PNG, one PDF per page or one PDF with every page.
ESCL_NS = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03", "pwg": "http://www.pwg.org/schemas/2010/12/sm"}
SCAN_DIR = os.path.join(THUMBS, "scans")
SCANNERS_FILE = os.path.join(HOME, "Library", "Application Support", "FileHub", "scanners.json")
PAPER = {  # width x height in 1/300 inch, the unit eSCL uses
    "a4": (2480, 3508), "letter": (2550, 3300), "legal": (2550, 4200), "a5": (1748, 2480), "a3": (3508, 4961),
}
_scan_jobs = {}      # job id -> {"state", "pages": [paths], "error", "dpi", ...}
_scanners_cache = {"time": 0, "items": []}


def _dns_sd(args, timeout):
    """Run dns-sd (it never exits on its own) for a short while and return what it printed."""
    try:
        proc = subprocess.Popen(["dns-sd"] + args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return ""
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def _unescape_dns(name):
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), name).replace("\\.", ".")


def discover_scanners(timeout=3.0):
    """Scanners announcing eSCL on the local network, via Bonjour's zone listing (-Z)."""
    found = {}
    for service, scheme in (("_uscan._tcp", "http"), ("_uscans._tcp", "https")):
        out = _dns_sd(["-Z", service, "local."], timeout)
        srv, txt = {}, {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[1] == "SRV":
                srv[parts[0]] = (parts[5].rstrip("."), int(parts[4]))
            elif len(parts) >= 3 and parts[1] == "TXT":
                txt[parts[0]] = dict(kv.split("=", 1) for kv in re.findall(r'"([^"]*)"', line) if "=" in kv)
        for key, (host, port) in srv.items():
            t = txt.get(key, {})
            rs = (t.get("rs") or "eSCL").strip("/")
            name = _unescape_dns(key.split("._uscan")[0])
            sid = re.sub(r"[^a-z0-9]+", "-", (name + "-" + host).lower()).strip("-")
            found[sid] = {"id": sid, "name": name, "host": host, "port": port, "base": "%s://%s:%d/%s" % (scheme, host, port, rs),
                          "model": t.get("ty") or "", "source": "network"}
    return list(found.values())


def _load_manual_scanners():
    try:
        with open(SCANNERS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def _save_manual_scanners(items):
    os.makedirs(os.path.dirname(SCANNERS_FILE), exist_ok=True)
    with open(SCANNERS_FILE, "w") as fh:
        json.dump(items, fh)


def scanners(refresh=False):
    if refresh or time.time() - _scanners_cache["time"] > 60:
        _scanners_cache["items"] = discover_scanners()
        _scanners_cache["time"] = time.time()
    items = list(_scanners_cache["items"])
    ids = {x["id"] for x in items}
    for m in _load_manual_scanners():
        if m["id"] not in ids:
            items.append(m)
    return items


def scanner_by_id(sid):
    for x in scanners():
        if x["id"] == sid:
            return x
    raise ValueError("That scanner isn't on the network right now. Press Refresh, or check it is switched on.")


def _http(url, data=None, method=None, timeout=30, headers=None):
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("User-Agent", "FileHub")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # scanners use self-signed certificates on the local network
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def add_scanner_by_address(address):
    """Add a scanner by IP or name (for ones Bonjour doesn't see). Checks it really answers eSCL."""
    address = (address or "").strip().rstrip("/")
    if not address:
        raise ValueError("Type the scanner's address, like 192.168.1.20")
    tries = [address] if "://" in address else ["http://%s" % address, "http://%s:8080" % address, "https://%s" % address]
    last = None
    for base in tries:
        for path in ("/eSCL", ""):
            try:
                with _http(base + path + "/ScannerCapabilities", timeout=6) as r:
                    caps = parse_caps(r.read())
                    host = re.sub(r"^https?://", "", base).split(":")[0]
                    sid = "manual-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
                    item = {"id": sid, "name": caps.get("name") or host, "host": host, "port": 0, "base": base + path,
                            "model": caps.get("model", ""), "source": "manual"}
                    items = [x for x in _load_manual_scanners() if x["id"] != sid] + [item]
                    _save_manual_scanners(items)
                    return item
            except Exception as e:  # noqa: BLE001 - any network problem just means "not this address"
                last = e
    raise ValueError("No scanner answered at “%s” (%s). Check the address and that the scanner is on." % (address, last))


def forget_scanner(sid):
    _save_manual_scanners([x for x in _load_manual_scanners() if x["id"] != sid])


def parse_caps(xml_bytes):
    """What the scanner can do, in plain lists."""
    root = ET.fromstring(xml_bytes)
    f = lambda path: root.find(path, ESCL_NS)  # noqa: E731
    t = lambda path: (f(path).text.strip() if f(path) is not None and f(path).text else "")  # noqa: E731
    caps = {"name": t("pwg:MakeAndModel") or t("scan:MakeAndModel"), "model": t("pwg:MakeAndModel"), "sources": {}}
    for key, path in (("platen", "scan:Platen/scan:PlatenInputCaps"), ("feeder", "scan:Adf/scan:AdfSimplexInputCaps"),
                      ("duplex", "scan:Adf/scan:AdfDuplexInputCaps")):
        node = f(path)
        if node is None:
            continue
        modes = sorted({m.text.strip() for m in node.findall(".//scan:ColorMode", ESCL_NS) if m.text})
        res = sorted({int(r.text) for r in node.findall(".//scan:DiscreteResolution/scan:XResolution", ESCL_NS) if r.text and r.text.isdigit()})
        fmts = sorted({d.text.strip() for d in node.findall(".//pwg:DocumentFormat", ESCL_NS) if d.text} |
                      {d.text.strip() for d in node.findall(".//scan:DocumentFormatExt", ESCL_NS) if d.text})
        mw = node.find("scan:MaxWidth", ESCL_NS)
        mh = node.find("scan:MaxHeight", ESCL_NS)
        caps["sources"][key] = {"colors": modes, "resolutions": res or [300], "formats": fmts,
                                "maxW": int(mw.text) if mw is not None and mw.text else 2550,
                                "maxH": int(mh.text) if mh is not None and mh.text else 3508}
    if not caps["sources"]:
        raise ValueError("That device answered, but doesn't describe a scanner.")
    return caps


def scanner_caps(sid):
    sc = scanner_by_id(sid)
    try:
        with _http(sc["base"] + "/ScannerCapabilities", timeout=10) as r:
            caps = parse_caps(r.read())
    except (urllib.error.URLError, OSError, ET.ParseError) as e:
        raise ValueError("Couldn't talk to “%s” (%s). Is it switched on and awake?" % (sc["name"], e))
    caps["scanner"] = sc
    return caps


def _scan_settings_xml(source, color, dpi, width, height, duplex):
    fmt = "image/jpeg"
    src = "Feeder" if source in ("feeder", "duplex") else "Platen"
    cm = {"color": "RGB24", "grey": "Grayscale8", "bw": "BlackAndWhite1"}.get(color, "RGB24")
    dup = "<scan:Duplex>true</scan:Duplex>" if source == "duplex" else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<scan:ScanSettings xmlns:pwg="%s" xmlns:scan="%s"><pwg:Version>2.0</pwg:Version>'
            '<pwg:ScanRegions><pwg:ScanRegion><pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>'
            '<pwg:Height>%d</pwg:Height><pwg:Width>%d</pwg:Width><pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>'
            '</pwg:ScanRegion></pwg:ScanRegions><pwg:InputSource>%s</pwg:InputSource><scan:ColorMode>%s</scan:ColorMode>'
            '<scan:XResolution>%d</scan:XResolution><scan:YResolution>%d</scan:YResolution>'
            '<pwg:DocumentFormat>%s</pwg:DocumentFormat>%s</scan:ScanSettings>'
            % (ESCL_NS["pwg"], ESCL_NS["scan"], height, width, src, cm, dpi, dpi, fmt, dup)).encode()


def start_scan(sid, source="platen", color="color", dpi=300, paper="a4", job=None):
    """Begin a scan in the background. Pages are added to the job as they arrive."""
    sc = scanner_by_id(sid)
    caps = scanner_caps(sid)
    src = caps["sources"].get(source) or caps["sources"].get("platen") or list(caps["sources"].values())[0]
    if source not in caps["sources"]:
        source = "platen" if "platen" in caps["sources"] else list(caps["sources"])[0]
    if dpi not in src["resolutions"]:
        dpi = min(src["resolutions"], key=lambda r: abs(r - dpi))
    pw, ph = PAPER.get(paper, (src["maxW"], src["maxH"]))
    pw, ph = min(pw, src["maxW"]), min(ph, src["maxH"])
    jid = job or uuid.uuid4().hex[:12]
    j = _scan_jobs.setdefault(jid, {"id": jid, "pages": [], "state": "idle", "error": None, "dpi": dpi})
    if j["state"] == "scanning":
        raise ValueError("Already scanning. Wait for it to finish.")
    j.update({"state": "scanning", "error": None, "dpi": dpi, "scanner": sc["name"]})
    os.makedirs(os.path.join(SCAN_DIR, jid), exist_ok=True)
    threading.Thread(target=_run_scan, args=(j, sc, source, color, dpi, pw, ph), daemon=True).start()
    return {"job": jid, "dpi": dpi, "source": source}


def _run_scan(j, sc, source, color, dpi, pw, ph):
    try:
        body = _scan_settings_xml(source, color, dpi, pw, ph, source == "duplex")
        try:
            with _http(sc["base"] + "/ScanJobs", data=body, timeout=30, headers={"Content-Type": "text/xml"}) as r:
                loc = r.headers.get("Location") or ""
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise ValueError("The scanner is busy or nothing is in the feeder. Put the paper in and try again.")
            if e.code == 503:
                raise ValueError("The scanner says it is busy. Try again in a moment.")
            raise ValueError("The scanner refused the job (%s)." % e.code)
        if not loc:
            raise ValueError("The scanner didn't say where the scan is.")
        if loc.startswith("/"):
            m = re.match(r"^(https?://[^/]+)", sc["base"])
            loc = m.group(1) + loc
        n = len(j["pages"])
        waits = 0
        while True:
            try:
                with _http(loc.rstrip("/") + "/NextDocument", timeout=120) as r:
                    data = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    break  # no more pages
                if e.code in (503, 409) and waits < 60:
                    waits += 1
                    time.sleep(1)
                    continue
                raise ValueError("The scanner stopped early (%s)." % e.code)
            if not data:
                break
            n += 1
            path = os.path.join(SCAN_DIR, j["id"], "p%03d.jpg" % n)
            with open(path, "wb") as fh:
                fh.write(data)
            j["pages"].append({"n": n, "path": path, "rot": 0, "size": jpeg_info(data), "dpi": dpi})
            if source == "platen":
                break  # a flatbed gives one page per scan
        try:
            _http(loc, method="DELETE", timeout=5).close()
        except Exception:  # noqa: BLE001
            pass
        if not j["pages"]:
            raise ValueError("Nothing came back. Is there paper on the glass or in the feeder?")
        j["state"] = "done"
    except Exception as e:  # noqa: BLE001 - whatever went wrong, the page must hear about it
        j["state"] = "error"
        j["error"] = str(e)


def scan_status(jid):
    j = _scan_jobs.get(jid)
    if not j:
        raise ValueError("That scan is gone.")
    return {"job": jid, "state": j["state"], "error": j["error"], "dpi": j["dpi"],
            "pages": [{"n": p["n"], "rot": p["rot"], "w": p["size"][0], "h": p["size"][1], "dpi": p.get("dpi", j["dpi"])} for p in j["pages"]]}


def scan_page_path(jid, n):
    j = _scan_jobs.get(jid)
    for p in (j or {}).get("pages", []):
        if p["n"] == int(n):
            return p["path"]
    return None


def scan_edit(jid, pages):
    """Keep only these pages, in this order, with these turns: [{"n":1,"rot":90}, ...]"""
    j = _scan_jobs.get(jid)
    if not j:
        raise ValueError("That scan is gone.")
    by_n = {p["n"]: p for p in j["pages"]}
    kept = []
    for it in pages:
        p = by_n.get(int(it.get("n", 0)))
        if p:
            p["rot"] = int(it.get("rot", 0)) % 360
            kept.append(p)
    j["pages"] = kept
    return scan_status(jid)


def jpeg_info(data):
    """(width, height, components) of a JPEG, read from its header."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h, data[i + 9])
        i += 2 + length
    return (0, 0, 3)


def pdf_from_jpegs(pages, dpi):
    """A PDF with one JPEG page each, at real paper size. pages: [(jpeg bytes, rotation[, dpi])]. Pure Python."""
    objs = []  # list of bytes, object i+1

    def add(b):
        objs.append(b)
        return len(objs)
    page_ids = []
    pages_id_placeholder = None
    kids = []
    # objects: 1 catalog, 2 pages, then per page: image, content, page
    objs.append(b"")  # 1 catalog (filled later)
    objs.append(b"")  # 2 pages (filled later)
    for item in pages:
        data, rot = item[0], item[1]
        d = item[2] if len(item) > 2 and item[2] else dpi
        w, h, comps = jpeg_info(data)
        cs = b"/DeviceGray" if comps == 1 else (b"/DeviceCMYK" if comps == 4 else b"/DeviceRGB")
        pw, ph = w * 72.0 / d, h * 72.0 / d
        img = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (w, h, cs, len(data)) + data + b"\nendstream")
        content = ("q %.2f 0 0 %.2f 0 0 cm /Im1 Do Q" % (pw, ph)).encode()
        cid = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        pid = add(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] /Rotate %d /Resources << /XObject << /Im1 %d 0 R >> >> /Contents %d 0 R >>" % (pw, ph, rot % 360, img, cid)).encode())
        kids.append(pid)
    objs[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids), len(kids))).encode()
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % (i + 1) + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _sips(args):
    out = subprocess.run(["sips"] + args, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise ValueError("Couldn't convert the picture: " + (out.stderr.strip().split("\n")[-1] or "sips failed"))


def save_scan(jid, fmt="pdf", mode="one", dest=None, name=None):
    """Write the scanned pages as files in dest. fmt: jpeg | png | pdf; mode (pdf): one | separate."""
    j = _scan_jobs.get(jid)
    if not j or not j["pages"]:
        raise ValueError("There are no scanned pages yet.")
    dest = os.path.realpath(dest or os.path.join(HOME, "Documents", "Scans"))
    if not inside(dest, True):
        raise ValueError("Can't save there.")
    os.makedirs(dest, exist_ok=True)
    name = re.sub(r'[/:\\]', "-", (name or "").strip()) or ("Scan " + datetime.now().strftime("%Y-%m-%d %H.%M"))
    made = []
    pages = j["pages"]
    if fmt == "pdf" and mode == "one":
        out = free_name(dest, name + ".pdf")
        with open(out, "wb") as fh:
            fh.write(pdf_from_jpegs([(open(p["path"], "rb").read(), p["rot"], p.get("dpi")) for p in pages], j["dpi"]))
        made.append(out)
    else:
        for k, p in enumerate(pages, 1):
            stem = name if len(pages) == 1 else "%s - page %d" % (name, k)
            if fmt == "pdf":
                out = free_name(dest, stem + ".pdf")
                with open(out, "wb") as fh:
                    fh.write(pdf_from_jpegs([(open(p["path"], "rb").read(), p["rot"], p.get("dpi"))], j["dpi"]))
            elif fmt == "png":
                out = free_name(dest, stem + ".png")
                _sips(["-s", "format", "png"] + (["-r", str(p["rot"])] if p["rot"] else []) + [p["path"], "--out", out])
            else:
                out = free_name(dest, stem + ".jpg")
                if p["rot"]:
                    _sips(["-r", str(p["rot"]), p["path"], "--out", out])
                else:
                    shutil.copy2(p["path"], out)
            made.append(out)
    for m in made:
        CREATED.add(m)
    _library["time"] = 0
    return {"made": made, "folder": dest, "names": [os.path.basename(m) for m in made]}


COMPRESS_LEVELS = {  # name: (dots per inch, JPEG quality)
    "small": (100, 0.5),
    "balanced": (150, 0.7),
    "good": (200, 0.85),
}


def compress_pdf(path, level="balanced", grey=False):
    """Make a smaller copy next to the PDF: "Name - small.pdf". The original is never touched."""
    path = os.path.realpath(path)
    if not allowed(path) or not path.lower().endswith(".pdf"):
        raise ValueError("That PDF isn't there any more.")
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0]
    out = free_name(folder, "%s - small.pdf" % stem)
    before = os.path.getsize(path)
    if level == "text":
        try:
            pages = pdftool("shrink", path, out)
        except ValueError as e:
            raise ValueError("Couldn't shrink the pictures inside (%s). This needs macOS 13 or newer; "
                             "the picture options below work on every Mac." % e)
    else:
        dpi, q = COMPRESS_LEVELS.get(level, COMPRESS_LEVELS["balanced"])
        pages = pdftool("compress", path, out, dpi, q, "grey" if grey else "colour")
    after = os.path.getsize(out)
    if after >= before * 0.98:
        os.unlink(out)  # our own just-made file, not the user's: it didn't help, so don't leave it behind
        raise ValueError("It didn't get smaller: this PDF is already compact at that quality. Try “Smallest”."
                         if level != "text" else "The pictures inside are already small. Try one of the picture options below.")
    CREATED.add(out)
    _library["time"] = 0
    return {"path": out, "name": os.path.basename(out), "pages": pages, "before": before, "after": after}


def restore_backup(path, backup):
    """Undo 'save into this file': put the saved backup back."""
    path = os.path.realpath(path)
    if not allowed(path) or not backup.startswith(BACKUPS + "/") or not os.path.isfile(backup):
        raise ValueError("The earlier version isn't available any more.")
    shutil.copy2(backup, path)
    _library["time"] = 0
    return True


def pdf_pages_dir(path):
    st = os.stat(path)
    key = re.sub(r"[^A-Za-z0-9]", "_", path)[-150:] + "_%d_pages2" % int(st.st_mtime)
    return os.path.join(THUMBS, key)


BIG_PAGE = 1600  # pixels wide: sharp enough to read small print when zoomed


def pdf_page_big(path, n):
    d = pdf_pages_dir(path)
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "big%d.png" % n)
    if not os.path.exists(out):
        pdftool("page", path, n, BIG_PAGE, out)
    return out


def pdf_pages(path):
    """Make a small picture of every page. Returns the page count."""
    d = pdf_pages_dir(path)
    done = os.path.join(d, "count")
    if os.path.exists(done):
        return int(open(done).read())
    os.makedirs(d, exist_ok=True)
    n = pdftool("thumbs", path, d, 380)
    with open(done, "w") as fh:
        fh.write(str(n))
    return n


# ---------- Links between split files and their original ----------
# Saved next to the app, so FileHub always knows which file a split came from,
# even if you rename it or save it in another folder.
RELATIONS_FILE = os.path.join(HERE, "filehub-links.json")
_rel_lock = threading.Lock()


def load_links():
    try:
        with open(RELATIONS_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


_links = load_links()  # {child_path: {"parent": path, "from": n, "to": n, "time": t}}


def save_link(child, parent, start, end):
    with _rel_lock:
        _links[child] = {"parent": parent, "from": start, "to": end, "time": time.time()}
        tmp = RELATIONS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_links, fh, indent=1)
        os.replace(tmp, RELATIONS_FILE)


ORIGIN_PREFIX = "file://"


def stamp_origin(child, parent, start, end):
    """Write 'where this came from' into the file's own Mac info (like a download's source).
    It stays with the file when you rename or move it."""
    url = ORIGIN_PREFIX + quote(parent) + "#filehub-pages=%d-%d" % (start, end)
    data = plistlib.dumps([url], fmt=plistlib.FMT_BINARY).hex()
    subprocess.run(["xattr", "-wx", "com.apple.metadata:kMDItemWhereFroms", data, child],
                   capture_output=True)


def origin_from_stamp(froms):
    for u in froms or []:
        if u.startswith(ORIGIN_PREFIX) and "#filehub-pages=" in u:
            src, pages = u[len(ORIGIN_PREFIX):].split("#filehub-pages=", 1)
            try:
                a, b = (int(x) for x in pages.split("-"))
            except ValueError:
                continue
            return {"parent": unquote(src), "from": a, "to": b}
    return None


def links_for(path, froms=None):
    """What this file was split from, and what was split out of it."""
    parent = _links.get(path) or origin_from_stamp(froms)
    children =[{"path": c, "from": v["from"], "to": v["to"]}
                for c, v in _links.items() if v["parent"] == path and os.path.exists(c)]
    children.sort(key=lambda c: (c["from"], c["to"]))
    return parent, children


def root_of(path, link=None, depth=0):
    link = link or _links.get(path)
    if not link or depth > 10:
        return path
    return root_of(link["parent"], None, depth + 1)


# ---------- Places (main folders) and browsing ----------
def places():
    items = [("Desktop", "Desktop"), ("Documents", "Documents"), ("Downloads", "Downloads")]
    out = [{"name": n, "path": os.path.join(HOME, d), "icon": n.lower()} for n, d in items]
    if os.path.isdir(ICLOUD):
        out.append({"name": "iCloud Drive", "path": ICLOUD, "icon": "icloud"})
    out.append({"name": "Pictures", "path": os.path.join(HOME, "Pictures"), "icon": "pictures"})
    out.append({"name": "Home", "path": HOME, "icon": "home"})
    out.append({"name": "Bin", "path": TRASH, "icon": "trash"})
    return [p for p in out if os.path.isdir(p["path"])]


def nice_path(p):
    if p.startswith(VOLUMES + "/"):
        return p[len(VOLUMES) + 1:]
    if p == TRASH or p.startswith(TRASH + "/"):
        return "Bin" + p[len(TRASH):]
    return p.replace(ICLOUD, "iCloud Drive", 1).replace(HOME, "~", 1)


# macOS lets a Terminal-started program look inside ~/.Trash only with "Full Disk Access".
# Without it we ask Finder for the list instead (Finder may ask you once to allow this).
BIN_LIST = """tell application "Finder"
  set out to ""
  repeat with it in (items of trash)
    try
      set out to out & (POSIX path of (it as alias)) & tab & (name of it) & tab & (physical size of it) & tab & (class of it as string) & tab & ((modification date of it) as «class isot» as string) & linefeed
    end try
  end repeat
  return out
end tell"""


class NoAccess(Exception):
    pass


def browse_bin_via_finder():
    out = subprocess.run(["osascript", "-e", BIN_LIST], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise NoAccess(out.stderr.strip())
    folders, files = [], []
    for line in out.stdout.split("\n"):
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        path, name, size_s, kind, mod = parts[:5]
        path = path.rstrip("/")
        try:
            size = int(float(size_s))
        except ValueError:
            size = 0
        try:
            modified = datetime.fromisoformat(mod[:19]).timestamp()
        except ValueError:
            modified = 0
        if kind == "folder":
            folders.append({"name": name, "path": path, "count": 0})
        else:
            ext = os.path.splitext(name)[1].lower()
            files.append({"splitFrom": None, "splitInto": [], "path": path, "name": name, "folder": "Bin",
                          "ext": ext.lstrip("."), "type": file_type(ext), "kind": "", "size": size,
                          "modified": modified, "created": modified, "lastUsed": None, "useCount": 0,
                          "source": "Bin", "url": None, "family": "file:" + path, "pages": None,
                          "pageWord": PAGE_WORD.get(ext, "pages")})
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    return {"path": TRASH, "display": "Bin", "crumbs": [{"name": "Bin", "path": TRASH}],
            "folders": folders, "files": files, "viaFinder": True}


def browse(path):
    try:
        names = sorted((n for n in os.listdir(path) if not n.startswith(".")),
                       key=lambda n: n.lower())
    except PermissionError:
        if path == TRASH:
            return browse_bin_via_finder()
        raise NoAccess(path)
    folders, files = [], []
    for n in names:
        full = os.path.join(path, n)
        if os.path.isdir(full) and not n.endswith((".app", ".photoslibrary", ".localized")):
            try:
                count = len([x for x in os.listdir(full) if not x.startswith(".")])
            except OSError:
                count = 0
            folders.append({"name": n, "path": full, "count": count})
        elif os.path.isfile(full):
            files.append(full)
    crumbs, cur = [], path
    tops = [HOME, ICLOUD, TRASH] + drive_roots()
    while cur.startswith(HOME) or on_drive(cur):
        crumbs.insert(0, {"name": "Home" if cur == HOME else ("Bin" if cur == TRASH else
                          ("iCloud Drive" if cur == ICLOUD else os.path.basename(cur))), "path": cur})
        if cur in tops:
            break
        cur = os.path.dirname(cur)
    return {"path": path, "display": nice_path(path), "crumbs": crumbs,
            "folders": folders, "files": metadata(files[:600])}


PICKER_PROMPTS = {"split": "Save the split PDF in:", "sidebar": "Add this folder to the FileHub sidebar:",
                  "move": "Move the selected items to:", "copy": "Copy the selected items to:"}


def choose_folder(purpose="split"):
    """Open the Mac's own folder picker and return the chosen folder (or None)."""
    prompt = PICKER_PROMPTS.get(purpose, PICKER_PROMPTS["split"])
    script = ('tell application "System Events" to activate\n'
              'POSIX path of (choose folder with prompt "%s")' % prompt)
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    p = out.stdout.strip().rstrip("/")
    return p if p and os.path.isdir(p) else None


def split_pdf(path, start, end, dest=None):
    """Save pages start..end as a new PDF. Never changes the original."""
    folder, name = os.path.split(path)
    if dest:
        dest = os.path.realpath(dest)
        if not inside(dest, True) or not os.path.isdir(dest):
            raise ValueError("can't save in that folder")
        folder = dest
    stem = os.path.splitext(name)[0]
    label = "page %d" % start if start == end else "pages %d-%d" % (start, end)
    out = os.path.join(folder, "%s - %s.pdf" % (stem, label))
    i = 2
    while os.path.exists(out):
        out = os.path.join(folder, "%s - %s (%d).pdf" % (stem, label, i))
        i += 1
    count = pdftool("split", path, start, end, out)
    save_link(out, path, start, end)
    stamp_origin(out, path, start, end)
    _library["time"] = 0  # show the new file on next refresh
    return {"path": out, "name": os.path.basename(out), "pages": count,
            "folder": nice_path(folder), "folderPath": folder}


# ---------- Copy / paste (only ever ADDS files: never moves, renames or deletes) ----------
CLIP_WRITE = """ObjC.import('AppKit');
function run(argv) {
  const pb = $.NSPasteboard.generalPasteboard;
  pb.clearContents;
  const urls = $.NSMutableArray.array;
  argv.forEach(p => urls.addObject($.NSURL.fileURLWithPath(p)));
  return pb.writeObjects(urls) ? 'ok' : 'fail';
}"""
CLIP_READ = """ObjC.import('AppKit');
function run() {
  const items = $.NSPasteboard.generalPasteboard.pasteboardItems;
  const out = [];
  if (items) for (let i = 0; i < items.count; i++) {
    const u = items.objectAtIndex(i).stringForType('public.file-url');
    if (u && !u.isNil()) { const url = $.NSURL.URLWithString(u); if (url && url.isFileURL) out.push(url.path.js); }
  }
  return JSON.stringify(out);
}"""


# The Mac's own share panels (AirDrop / Messages). The script stays alive while the panel is open.
SHARE_JS = """ObjC.import('AppKit');
function run(argv) {
  const via = argv.shift();
  const name = via === 'messages' ? $.NSSharingServiceNameComposeMessage : $.NSSharingServiceNameSendViaAirDrop;
  const svc = $.NSSharingService.sharingServiceNamed(name);
  if (svc.isNil()) return 'missing';
  const items = $.NSMutableArray.array;
  argv.forEach(p => items.addObject($.NSURL.fileURLWithPath(p)));
  $.NSApplication.sharedApplication;
  svc.performWithItems(items);
  $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(300));
  return 'ok';
}"""


def clipboard_set(paths):
    """Put files on the Mac clipboard, so they can be pasted here, in Finder, Mail..."""
    out = subprocess.run(["osascript", "-l", "JavaScript", "-e", CLIP_WRITE] + paths,
                         capture_output=True, text=True, timeout=15)
    return out.stdout.strip() == "ok"


def clipboard_files():
    """Files currently on the Mac clipboard (copied here or in Finder)."""
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", CLIP_READ],
                             capture_output=True, text=True, timeout=15)
        paths = json.loads(out.stdout.strip() or "[]")
    except Exception:
        return []
    return [p for p in paths if os.path.exists(p)]


def free_name(folder, name, copy_word=False):
    """A name that is not taken yet in folder, like Finder: 'Report copy.pdf', 'Report copy 2.pdf'."""
    if os.path.isdir(os.path.join(folder, name)) or "." not in name.strip("."):
        stem, ext = name, ""
    else:
        stem, ext = os.path.splitext(name)
    if not copy_word and not os.path.exists(os.path.join(folder, name)):
        return os.path.join(folder, name)
    cand = os.path.join(folder, "%s copy%s" % (stem, ext))
    i = 2
    while os.path.exists(cand):
        cand = os.path.join(folder, "%s copy %d%s" % (stem, i, ext))
        i += 1
    return cand


def copy_into(sources, dest):
    """Copy files/folders into dest. Existing files are never replaced."""
    dest = os.path.realpath(dest)
    if not inside(dest, True) or not os.path.isdir(dest):
        raise ValueError("can't paste into that folder")
    made = []
    for src in sources:
        src = os.path.realpath(src)
        if not os.path.exists(src):
            continue
        if os.path.isdir(src) and (dest == src or dest.startswith(src + "/")):
            raise ValueError("can't paste a folder inside itself")
        out = free_name(dest, os.path.basename(src))
        # ditto keeps the Mac details too (where it was downloaded from, tags...)
        subprocess.run(["ditto", src, out], check=True, capture_output=True, timeout=3600)
        made.append(out)
        CREATED.add(out)
    _library["time"] = 0  # show the new files on next refresh
    return made


def duplicate(path):
    path = os.path.realpath(path)
    out = free_name(os.path.dirname(path), os.path.basename(path), copy_word=True)
    subprocess.run(["ditto", path, out], check=True, capture_output=True, timeout=3600)
    CREATED.add(out)
    _library["time"] = 0
    return out


def new_folder(dest):
    dest = os.path.realpath(dest)
    if not inside(dest, True) or not os.path.isdir(dest):
        raise ValueError("can't make a folder there")
    out = os.path.join(dest, "untitled folder")
    i = 2
    while os.path.exists(out):
        out = os.path.join(dest, "untitled folder %d" % i)
        i += 1
    os.mkdir(out)
    CREATED.add(out)
    return out


CREATED = set()  # items FileHub made while running (copies, duplicates, new folders)
JUST_MOVED = set()  # where FileHub moved or renamed things to (so they show before Spotlight notices)


def relink(old, new):
    """Keep the split-PDF links pointing at a renamed or moved file/folder."""
    with _rel_lock:
        changed = False

        def moved(p):
            if p == old:
                return new
            if p.startswith(old + "/"):
                return new + p[len(old):]
            return p
        for child in list(_links):
            v = _links.pop(child)
            nc, np_ = moved(child), moved(v["parent"])
            changed |= nc != child or np_ != v["parent"]
            v["parent"] = np_
            _links[nc] = v
        if changed:
            tmp = RELATIONS_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(_links, fh, indent=1)
            os.replace(tmp, RELATIONS_FILE)


in_home = inside  # kept for older code paths


def move_one(src, dst):
    """Move src to the exact new path dst. Never replaces anything."""
    src, dst = os.path.realpath(src), os.path.abspath(dst)
    if not in_home(src) or not os.path.exists(src):
        raise ValueError("“%s” isn't there any more." % os.path.basename(src))
    if os.path.dirname(src) == HOME or is_root(src):
        raise ValueError("Main folders, like Desktop and Documents, and whole drives can't be moved.")
    parent = os.path.realpath(os.path.dirname(dst))
    if not in_home(parent, True) or not os.path.isdir(parent):
        raise ValueError("Can't move things there.")
    if os.path.isdir(src) and (parent == src or parent.startswith(src + "/")):
        raise ValueError("Can't move a folder inside itself.")
    if os.path.lexists(dst):
        raise ValueError("“%s” already exists there." % os.path.basename(dst))
    try:
        os.rename(src, dst)  # same disk: instant
    except OSError:
        subprocess.run(["ditto", src, dst], check=True, capture_output=True, timeout=3600)
        trash([src], force=True)  # other disk: the original goes to the Bin, never deleted
    relink(src, dst)
    JUST_MOVED.discard(src)
    JUST_MOVED.add(dst)
    if src in CREATED:
        CREATED.discard(src)
        CREATED.add(dst)
    _library["time"] = 0
    return [src, dst]


def move_into(sources, dest):
    """Move items into the folder dest; names that are taken get ' 2', ' 3'..."""
    dest = os.path.realpath(dest)
    if not in_home(dest, True) or not os.path.isdir(dest):
        raise ValueError("Can't move things there.")
    pairs = []
    for src in sources:
        src = os.path.realpath(src)
        if os.path.dirname(src) == dest:
            continue  # already there
        name = os.path.basename(src)
        out = os.path.join(dest, name)
        if os.path.lexists(out):
            stem, ext = (name, "") if os.path.isdir(src) or "." not in name.strip(".") else os.path.splitext(name)
            i = 2
            while os.path.lexists(out):
                out = os.path.join(dest, "%s %d%s" % (stem, i, ext))
                i += 1
        pairs.append(move_one(src, out))
    return pairs


TRASH_JS = """ObjC.import('Foundation');
function run(argv) {
  const fm = $.NSFileManager.defaultManager, out = [];
  for (const p of argv) {
    try {
      const where = Ref(), err = Ref();
      if (fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), where, err)) {
        let np = null; try { np = where[0].path.js; } catch (e) {}
        out.push([p, np]);
      } else {
        let msg = ''; try { msg = err[0].localizedDescription.js; } catch (e) {}
        out.push([p, null, msg || 'could not move']);
      }
    } catch (e) { out.push([p, null, String(e)]); }
  }
  return JSON.stringify(out);
}"""


def _trash_with_finder(path):
    """Fallback: ask Finder to put one item in the Bin (works everywhere Finder does, keeps Put Back)."""
    script = 'tell application "Finder" to delete (POSIX file "%s" as alias)' % path.replace('\\', '\\\\').replace('"', '\\"')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    return out.returncode == 0 and not os.path.lexists(path)


def trash(paths, force=False):
    """Put items in the Bin (they can be taken back out). Only things FileHub made, for Undo."""
    paths = [os.path.realpath(p) for p in paths]
    ok = [p for p in paths if os.path.exists(p) and (force or p in CREATED)]
    if not ok:
        return []
    out = subprocess.run(["osascript", "-l", "JavaScript", "-e", TRASH_JS] + ok,
                         capture_output=True, text=True, timeout=120)
    try:
        results = json.loads(out.stdout.strip() or "[]")
    except ValueError:
        results = []
    pairs, problems = [], []
    gone = {r[0]: r[1] for r in results if len(r) == 2 or (len(r) >= 3 and not r[2])}
    for p in ok:
        if p in gone and not os.path.lexists(p):
            pairs.append([p, gone[p]])
            continue
        # Not moved by the batch: try again on its own, then through Finder.
        if os.path.lexists(p):
            try:
                one = subprocess.run(["osascript", "-l", "JavaScript", "-e", TRASH_JS, p],
                                     capture_output=True, text=True, timeout=60)
                r = json.loads(one.stdout.strip() or "[]")
                if r and len(r[0]) < 3 and not os.path.lexists(p):
                    pairs.append([p, r[0][1]])
                    continue
            except (ValueError, subprocess.SubprocessError):
                pass
        if os.path.lexists(p):
            try:
                if _trash_with_finder(p):
                    pairs.append([p, None])
                    continue
            except subprocess.SubprocessError:
                pass
        if os.path.lexists(p):
            why = next((r[2] for r in results if r and r[0] == p and len(r) >= 3), "") or (out.stderr or "").strip().split("\n")[-1]
            problems.append((os.path.basename(p), why))
        else:
            pairs.append([p, None])
    for p, where in pairs:
        CREATED.discard(p)
        if where:
            relink(p, where)
    _library["time"] = 0
    if problems and not pairs:
        raise ValueError("Couldn't move to the Bin: %s%s" % (problems[0][0], (" — " + problems[0][1]) if problems[0][1] else ""))
    if problems:
        names = ", ".join(n for n, _ in problems[:3]) + (" and more" if len(problems) > 3 else "")
        raise PartialTrash(pairs, "%d of %d moved. Couldn't move: %s" % (len(pairs), len(ok), names))
    return pairs


class PartialTrash(Exception):
    def __init__(self, pairs, msg):
        super().__init__(msg)
        self.pairs = pairs


def delete_items(paths):
    """Move your own files/folders to the Bin (never deleted for good). Returns [[old, inBin], ...]."""
    paths = [os.path.realpath(p) for p in paths]
    for p in paths:
        if not allowed(p):
            raise ValueError("“%s” isn't there any more." % os.path.basename(p))
        if os.path.dirname(p) == HOME or is_root(p):
            raise ValueError("Main folders, like Desktop and Documents, and whole drives can't be put in the Bin.")
    return trash(paths, force=True)


def empty_bin():
    out = subprocess.run(["osascript", "-e", 'tell application "Finder" to empty trash'],
                         capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise ValueError("Couldn't empty the Bin: " + (out.stderr.strip().split("\n")[-1] or "Finder said no"))
    _library["time"] = 0
    return True


def eject(path):
    path = os.path.realpath(path)
    if path not in drive_roots():
        raise ValueError("That isn't a plugged-in drive.")
    out = subprocess.run(["diskutil", "unmount", path], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().split("\n")[-1]
        raise ValueError("Couldn't eject: %s" % (msg or "it's still in use. Close files on it first."))
    return True


def rename_item(path, new_name):
    """Rename a file or folder in the same folder. Never replaces another file."""
    path = os.path.realpath(path)
    new_name = (new_name or "").strip()
    if not new_name or new_name in (".", "..") or "/" in new_name or ":" in new_name \
            or new_name.startswith(".") or len(new_name.encode()) > 255:
        raise ValueError("Please use a different name (no / or : and not starting with a dot).")
    folder = os.path.dirname(path)
    if folder == HOME or is_root(path):
        raise ValueError("Main folders, like Desktop and Documents, and drives can't be renamed here.")
    new = os.path.join(folder, new_name)
    if new == path:
        return path
    if os.path.exists(new) and not os.path.samefile(new, path):  # same file = only the capitals changed
        raise ValueError("“%s” already exists in this folder." % new_name)
    os.rename(path, new)
    relink(path, new)
    JUST_MOVED.discard(path)
    JUST_MOVED.add(new)
    _library["time"] = 0
    return new


# ---------- Web server ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None):
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            self.send(200, fh.read(), ctype)

    def do_GET(self):
        url = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self.send_file(os.path.join(HERE, "filehub.html"), "text/html; charset=utf-8")
            if url.path == "/api/version":
                return self.send(200, {"version": VERSION})
            if url.path == "/api/library":
                return self.send(200, library(force=qs.get("refresh") == "1"))
            if url.path == "/api/open":
                return self.send(200, open_files())
            if url.path == "/api/search":
                return self.send(200, search(qs.get("q", "")) if qs.get("q") else [])
            if url.path == "/api/places":
                return self.send(200, places())
            if url.path == "/api/drives":
                return self.send(200, drives())
            if url.path == "/api/systemlook":
                return self.send(200, system_look())
            if url.path == "/api/icon":
                p = qs.get("path", "")
                if not (inside(p, True) and os.path.exists(p)):
                    return self.send(404, {"error": "not found"})
                ic = mac_icon(p, int(qs.get("s", 128)))
                if not ic:
                    return self.send(404, {"error": "no icon"})
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "max-age=86400")
                with open(ic, "rb") as fh:
                    body = fh.read()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if url.path == "/api/stamp":  # cheap: has this folder changed? (files added, removed, renamed)
                p = os.path.realpath(qs.get("path", "") or HOME)
                if not inside(p, True) or not os.path.isdir(p):
                    return self.send(404, {"error": "not found"})
                try:
                    names = [n for n in os.listdir(p) if not n.startswith(".")]
                    st = os.stat(p)
                    return self.send(200, {"stamp": "%d-%d-%d" % (int(st.st_mtime), int(st.st_ctime), len(names))})
                except OSError as e:
                    return self.send(500, {"error": str(e)})
            if url.path == "/api/browse":
                p = os.path.realpath(qs.get("path", HOME) or HOME)
                if not inside(p, True) or not os.path.isdir(p):
                    return self.send(404, {"error": "not found"})
                try:
                    return self.send(200, browse(p))
                except NoAccess as e:
                    return self.send(403, {"error": "no-access", "detail": str(e)})
            if url.path in ("/api/thumb", "/api/raw"):
                p = qs.get("path", "")
                if not allowed(p):
                    return self.send(404, {"error": "not found"})
                if url.path == "/api/raw":
                    return self.send_file(p)
                t = thumbnail(p, int(qs.get("s", 480)))
                return self.send_file(t, "image/png") if t else self.send(404, {"error": "no preview"})
            if url.path == "/api/signatures":
                return self.send(200, {"items": signatures()})
            if url.path == "/api/scanners":
                return self.send(200, {"items": scanners(refresh=qs.get("refresh") == "1")})
            if url.path == "/api/scanner/caps":
                try:
                    return self.send(200, scanner_caps(qs.get("id", "")))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if url.path == "/api/scan/status":
                try:
                    return self.send(200, scan_status(qs.get("job", "")))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if url.path == "/api/scan/page":
                path = scan_page_path(qs.get("job", ""), qs.get("n", "0"))
                return self.send_file(path, "image/jpeg") if path else self.send(404, {"error": "no page"})
            if url.path == "/api/pdfinfo":
                p = qs.get("path", "")
                if not allowed(p) or not p.lower().endswith(".pdf"):
                    return self.send(404, {"error": "not found"})
                try:
                    return self.send(200, pdf_info(p))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if url.path in ("/api/pdfpages", "/api/pdfpage"):
                p = qs.get("path", "")
                if not allowed(p) or not p.lower().endswith(".pdf"):
                    return self.send(404, {"error": "not found"})
                if url.path == "/api/pdfpages":
                    return self.send(200, {"pages": pdf_pages(p)})
                n = int(qs.get("n", 1))
                if qs.get("size") == "big":
                    return self.send_file(pdf_page_big(p, n), "image/png")
                img = os.path.join(pdf_pages_dir(p), "p%d.png" % n)
                if not os.path.exists(img):
                    pdf_pages(p)
                return self.send_file(img, "image/png") if os.path.exists(img) else self.send(404, {"error": "no page"})
            return self.send(404, {"error": "not found"})
        except Exception as e:  # keep the server alive whatever happens
            return self.send(500, {"error": str(e)})

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        p = data.get("path", "")
        # Only accept requests from our own page.
        if self.headers.get("Origin") not in (None, "http://127.0.0.1:%d" % PORT,
                                              "http://localhost:%d" % PORT):
            return self.send(403, {"error": "forbidden"})
        if url.path == "/api/clipboard":
            files = clipboard_files()
            return self.send(200, {"files": files, "names": [os.path.basename(f) for f in files]})
        if url.path == "/api/share":
            paths = [os.path.realpath(x) for x in data.get("paths", []) if allowed(x)]
            via = data.get("via")
            if not paths:
                return self.send(404, {"error": "not found"})
            if via == "mail":  # Mail makes a new message with the files attached
                subprocess.Popen(["open", "-a", "Mail"] + paths)
            elif via in ("airdrop", "messages"):
                subprocess.Popen(["osascript", "-l", "JavaScript", "-e", SHARE_JS, via] + paths,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                return self.send(400, {"error": "unknown way to share"})
            return self.send(200, {"ok": True})
        if url.path == "/api/copy":
            paths = [x for x in data.get("paths", []) if allowed(x)]
            if not paths:
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": clipboard_set([os.path.realpath(x) for x in paths])})
        if url.path in ("/api/move", "/api/moveto", "/api/copyto", "/api/trash", "/api/duplicate", "/api/delete", "/api/eject"):
            try:
                if url.path == "/api/delete":
                    try:
                        return self.send(200, {"pairs": delete_items(data.get("paths", []))})
                    except PartialTrash as e:
                        return self.send(200, {"pairs": e.pairs, "warning": str(e)})
                if url.path == "/api/eject":
                    eject(p)
                    return self.send(200, {"ok": True})
                if url.path == "/api/move":
                    paths = [x for x in data.get("paths", []) if allowed(x)]
                    return self.send(200, {"pairs": move_into(paths, data.get("dest", ""))})
                if url.path == "/api/moveto":  # exact moves, used by Undo / Redo
                    return self.send(200, {"pairs": [move_one(a, b) for a, b in data.get("pairs", [])]})
                if url.path == "/api/copyto":
                    paths = [x for x in data.get("paths", []) if allowed(x)]
                    return self.send(200, {"made": copy_into(paths, data.get("dest", "")), "sources": paths})
                if url.path == "/api/duplicate":
                    paths = [x for x in (data.get("paths") or [p]) if allowed(x)]
                    if not paths:
                        return self.send(404, {"error": "not found"})
                    return self.send(200, {"made": [duplicate(x) for x in paths], "sources": paths})
                done = trash(data.get("paths", []))
                return self.send(200, {"trashed": [x[0] for x in done], "pairs": done})
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                return self.send(400, {"error": str(e)})
        if url.path in ("/api/paste", "/api/newfolder"):
            if not inside(p, True) or not os.path.isdir(p):
                return self.send(404, {"error": "not found"})
            try:
                if url.path == "/api/newfolder":
                    return self.send(200, {"made": [new_folder(p)]})
                files = clipboard_files()
                if not files:
                    return self.send(400, {"error": "Nothing to paste. Copy a file first."})
                return self.send(200, {"made": copy_into(files, p), "sources": files})
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/merge":
            try:
                return self.send(200, merge_pdfs(data.get("items", []), data.get("name"), data.get("dest") or None))
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/scanners/add":
            try:
                return self.send(200, add_scanner_by_address(data.get("address", "")))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/scanners/forget":
            forget_scanner(data.get("id", ""))
            return self.send(200, {"ok": True})
        if url.path == "/api/scan/start":
            try:
                return self.send(200, start_scan(data.get("scanner", ""), data.get("source", "platen"), data.get("color", "color"),
                                                 int(data.get("dpi", 300)), data.get("paper", "a4"), data.get("job")))
            except (ValueError, TypeError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/scan/edit":
            try:
                return self.send(200, scan_edit(data.get("job", ""), data.get("pages", [])))
            except (ValueError, TypeError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/scan/save":
            try:
                return self.send(200, save_scan(data.get("job", ""), data.get("format", "pdf"), data.get("mode", "one"),
                                                data.get("dest") or None, data.get("name")))
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/signatures/add":
            try:
                return self.send(200, add_signature(data.get("name"), data.get("strokes"), data.get("aspect"), data.get("width")))
            except (ValueError, TypeError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/signatures/delete":
            try:
                delete_signature(data.get("id", ""))
                return self.send(200, {"ok": True})
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/signatures/rename":
            try:
                return self.send(200, rename_signature(data.get("id", ""), data.get("name")))
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/annotate":
            try:
                return self.send(200, annotate_pdf(p, data.get("annotations", []), data.get("mode", "copy")))
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/rotate":
            try:
                return self.send(200, rotate_pdf(p, data.get("rotations", {}), data.get("mode", "copy")))
            except (ValueError, TypeError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/compress":
            try:
                return self.send(200, compress_pdf(p, data.get("level", "balanced"), bool(data.get("grey"))))
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/restore":
            try:
                restore_backup(p, data.get("backup", ""))
                return self.send(200, {"ok": True})
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/privacy":  # opens System Settings at Full Disk Access
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
            return self.send(200, {"ok": True})
        if url.path == "/api/emptybin":
            try:
                empty_bin()
                return self.send(200, {"ok": True})
            except (ValueError, subprocess.SubprocessError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/choosefolder":
            chosen = choose_folder(data.get("for") or "split")
            return self.send(200, {"path": chosen, "display": nice_path(chosen) if chosen else None})
        if not allowed(p):
            return self.send(404, {"error": "not found"})
        if url.path == "/api/launch":
            subprocess.Popen(["open", p])
        elif url.path == "/api/reveal":
            subprocess.Popen(["open", "-R", p])
        elif url.path == "/api/quicklook":
            subprocess.Popen(["qlmanage", "-p", p], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        elif url.path == "/api/rename":
            try:
                return self.send(200, {"path": rename_item(p, data.get("name"))})
            except (ValueError, OSError) as e:
                return self.send(400, {"error": str(e)})
        elif url.path == "/api/split":
            try:
                return self.send(200, split_pdf(p, int(data.get("from")), int(data.get("to")),
                                                data.get("dest") or None))
            except (ValueError, TypeError) as e:
                return self.send(400, {"error": str(e)})
        else:
            return self.send(404, {"error": "not found"})
        return self.send(200, {"ok": True})


# ---------- Updates from the cloud ----------
# FileHub fetches new versions itself (git pull), whether it was started from the Dock,
# the .command file or Terminal. No internet or a failed update = keep the current version.
UPDATE_EVERY = int(os.environ.get("FILEHUB_UPDATE_EVERY", 30 * 60))  # also check every 30 minutes while it runs


def git_version():
    try:
        out = subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


VERSION = git_version()


def self_update():
    """Get the latest FileHub from GitHub. True when new code arrived."""
    if not os.path.isdir(os.path.join(HERE, ".git")):
        return False
    # git needs Apple's command line tools; never trigger their install pop-up from here
    if subprocess.run(["xcode-select", "-p"], capture_output=True).returncode != 0:
        return False
    before = git_version()
    try:
        subprocess.run(["git", "-C", HERE, "pull", "--ff-only", "--quiet"],
                       capture_output=True, timeout=30,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    except Exception:
        return False
    after = git_version()
    return bool(after and after != before)


def restart():
    """Start the new version in place of this one (the browser page reloads by itself)."""
    env = dict(os.environ, FILEHUB_NO_BROWSER="1", FILEHUB_UPDATED="1")
    os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)], env)


def update_loop():
    while True:
        time.sleep(UPDATE_EVERY)
        if self_update():
            print("A new version of FileHub arrived. Restarting it...")
            restart()


def running_version():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/version" % PORT, timeout=3) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None  # older versions don't answer /api/version


def stop_old_filehub():
    """If an older FileHub is still running, stop it so this version can start."""
    if VERSION and running_version() == VERSION:
        return False  # the same version is already running: just use it
    out = subprocess.run(["lsof", "-nP", "-t", "-iTCP:%d" % PORT, "-sTCP:LISTEN"],
                         capture_output=True, text=True)
    stopped = False
    for pid in out.stdout.split():
        cmd = subprocess.run(["ps", "-p", pid, "-o", "command="], capture_output=True, text=True).stdout
        if "filehub.py" in cmd and int(pid) != os.getpid():  # only ever FileHub itself
            os.kill(int(pid), signal.SIGTERM)
            stopped = True
    return stopped


def start_server():
    for _ in range(20):
        try:
            return ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        except OSError:
            time.sleep(0.5)
    if running_version() == VERSION:
        raise SystemExit("FileHub is already running.")
    raise SystemExit("FileHub couldn't start: something else is using port %d." % PORT)


def main():
    global VERSION
    if not os.environ.get("FILEHUB_UPDATED"):
        print("Checking for updates...")
        if self_update():
            print("FileHub was updated. Starting the new version...")
            env = dict(os.environ, FILEHUB_UPDATED="1")
            os.execve(sys.executable, [sys.executable, os.path.abspath(__file__)], env)
    VERSION = git_version()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:  # port busy: an older FileHub, this same version, or our own restart
        if stop_old_filehub():
            print("Stopped the older FileHub.")
        elif not os.environ.get("FILEHUB_UPDATED"):
            print("FileHub is already running.")
            webbrowser.open("http://127.0.0.1:%d" % PORT)
            return
        server = start_server()
    print("FileHub is running at http://127.0.0.1:%d  (press Ctrl+C to stop)" % PORT)
    threading.Thread(target=library, daemon=True).start()  # warm up
    threading.Thread(target=update_loop, daemon=True).start()
    if not os.environ.get("FILEHUB_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
