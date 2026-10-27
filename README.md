# FileHub

A friendly dashboard for the files on a Mac: recent and most-used files, files open now,
related files grouped together (a lesson PDF and its split parts, copies, A/B/C versions),
where each file came from, page counts, previews, folder browsing (Desktop, Documents,
Downloads), and a PDF splitter with page previews. Light and dark theme.

## Run it (on the Mac)
Double-click `Open FileHub.command`, or run:

    python3 filehub.py

The launcher first runs `git pull` to fetch any changes made in the cloud. If there is
no internet or the update fails, it just starts the version you already have. If an older
FileHub is still running, it restarts it so the update takes effect.

It opens http://127.0.0.1:8765 (only this computer can reach it). It never moves
or deletes your files, and only renames one when you choose Rename (in the same folder,
never over another file). Splitting a PDF, pasting and duplicating always make new files,
and a paste never replaces a file that is already there.

Right-click a file, folder or empty space for a menu. Copy (⌘C) puts files on the Mac
clipboard, so you can paste them in a FileHub folder (⌘V) or in Finder; ⌘D duplicates.

## Files
- `filehub.py` – the small local server (Python 3.9+, standard library only).
- `filehub.html` – the whole screen (one page, no build step).
- `Open FileHub.command` – double-click launcher.

## Notes for working on it outside the Mac (e.g. a cloud session)
The server uses macOS-only tools: Spotlight (`mdfind`, `mdls`), Quick Look (`qlmanage`),
`lsof`, `open`, `xattr` and PDFKit through `osascript -l JavaScript`. These do not exist on
Linux, so file scanning, thumbnails and PDF splitting can only really be tested on the Mac.
To work on the screen elsewhere, serve `filehub.html` with sample data for the `/api/...`
endpoints, and keep the Python side compatible with Python 3.9 and the standard library.
