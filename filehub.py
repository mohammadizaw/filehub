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
import subprocess
import tempfile
import threading
import time
import webbrowser
import zipfile
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


def allowed(path):
    path = os.path.realpath(path)
    return path.startswith(HOME + "/") and os.path.exists(path)


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
        paths = [p for p in paths if os.path.splitext(p)[1].lower() in DOC_EXT]
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
    return [p for p in out if os.path.isdir(p["path"])]


def nice_path(p):
    return p.replace(ICLOUD, "iCloud Drive", 1).replace(HOME, "~", 1)


def browse(path):
    names = sorted((n for n in os.listdir(path) if not n.startswith(".")),
                   key=lambda n: n.lower())
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
    while cur.startswith(HOME):
        crumbs.insert(0, {"name": "Home" if cur == HOME else
                          ("iCloud Drive" if cur == ICLOUD else os.path.basename(cur)), "path": cur})
        if cur in (HOME, ICLOUD):
            break
        cur = os.path.dirname(cur)
    return {"path": path, "display": nice_path(path), "crumbs": crumbs,
            "folders": folders, "files": metadata(files[:600])}


def choose_folder():
    """Open the Mac's own folder picker and return the chosen folder (or None)."""
    script = ('tell application "System Events" to activate\n'
              'POSIX path of (choose folder with prompt "Save the split PDF in:")')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    p = out.stdout.strip().rstrip("/")
    return p if p and os.path.isdir(p) else None


def split_pdf(path, start, end, dest=None):
    """Save pages start..end as a new PDF. Never changes the original."""
    folder, name = os.path.split(path)
    if dest:
        dest = os.path.realpath(dest)
        if not (dest == HOME or dest.startswith(HOME + "/")) or not os.path.isdir(dest):
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
    if not (dest == HOME or dest.startswith(HOME + "/")) or not os.path.isdir(dest):
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
    _library["time"] = 0  # show the new files on next refresh
    return made


def duplicate(path):
    path = os.path.realpath(path)
    out = free_name(os.path.dirname(path), os.path.basename(path), copy_word=True)
    subprocess.run(["ditto", path, out], check=True, capture_output=True, timeout=3600)
    _library["time"] = 0
    return out


def new_folder(dest):
    dest = os.path.realpath(dest)
    if not (dest == HOME or dest.startswith(HOME + "/")) or not os.path.isdir(dest):
        raise ValueError("can't make a folder there")
    out = os.path.join(dest, "untitled folder")
    i = 2
    while os.path.exists(out):
        out = os.path.join(dest, "untitled folder %d" % i)
        i += 1
    os.mkdir(out)
    return out


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
            if url.path == "/api/library":
                return self.send(200, library(force=qs.get("refresh") == "1"))
            if url.path == "/api/open":
                return self.send(200, open_files())
            if url.path == "/api/search":
                return self.send(200, search(qs.get("q", "")) if qs.get("q") else [])
            if url.path == "/api/places":
                return self.send(200, places())
            if url.path == "/api/browse":
                p = os.path.realpath(qs.get("path", HOME) or HOME)
                if not (p == HOME or allowed(p)) or not os.path.isdir(p):
                    return self.send(404, {"error": "not found"})
                return self.send(200, browse(p))
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
        if url.path == "/api/copy":
            paths = [x for x in data.get("paths", []) if allowed(x)]
            if not paths:
                return self.send(404, {"error": "not found"})
            return self.send(200, {"ok": clipboard_set([os.path.realpath(x) for x in paths])})
        if url.path in ("/api/paste", "/api/newfolder"):
            if not (os.path.realpath(p) == HOME or allowed(p)):
                return self.send(404, {"error": "not found"})
            try:
                if url.path == "/api/newfolder":
                    return self.send(200, {"made": [new_folder(p)]})
                files = clipboard_files()
                if not files:
                    return self.send(400, {"error": "Nothing to paste. Copy a file first."})
                return self.send(200, {"made": copy_into(files, p)})
            except (ValueError, OSError, subprocess.SubprocessError) as e:
                return self.send(400, {"error": str(e)})
        if url.path == "/api/choosefolder":
            chosen = choose_folder()
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
        elif url.path == "/api/duplicate":
            try:
                return self.send(200, {"made": [duplicate(p)]})
            except (OSError, subprocess.SubprocessError) as e:
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


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("FileHub is running at http://127.0.0.1:%d  (press Ctrl+C to stop)" % PORT)
    threading.Thread(target=library, daemon=True).start()  # warm up
    if not os.environ.get("FILEHUB_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
