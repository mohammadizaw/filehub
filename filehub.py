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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
import urllib.request
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
                         capture_output=True, text=True, timeout=120)
    res = (out.stdout or out.stderr).strip()
    if res.startswith("ERR") or not res.isdigit():
        raise ValueError(res.replace("ERR ", "") or "PDF tool failed")
    return int(res)


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
  argv.forEach(p => {
    const where = Ref();
    if (fm.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(p), where, null)) {
      let np = null; try { np = where[0].path.js; } catch (e) {}
      out.push([p, np]);
    }
  });
  return JSON.stringify(out);
}"""


def trash(paths, force=False):
    """Put items in the Bin (they can be taken back out). Only things FileHub made, for Undo."""
    paths = [os.path.realpath(p) for p in paths]
    ok = [p for p in paths if os.path.exists(p) and (force or p in CREATED)]
    if not ok:
        return []
    out = subprocess.run(["osascript", "-l", "JavaScript", "-e", TRASH_JS] + ok,
                         capture_output=True, text=True, timeout=60)
    try:
        pairs = json.loads(out.stdout.strip() or "[]")
    except ValueError:
        pairs = []
    for p, where in pairs:
        CREATED.discard(p)
        if where:
            relink(p, where)
    _library["time"] = 0
    return pairs


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
                    return self.send(200, {"pairs": delete_items(data.get("paths", []))})
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
