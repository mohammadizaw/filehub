# FileHub

A friendly dashboard for the files on a Mac: recent and most-used files, files open now,
related files grouped together (a lesson PDF and its split parts, copies, A/B/C versions),
where each file came from, page counts, previews, folder browsing (Desktop, Documents,
Downloads), and a PDF splitter with page previews. Light and dark theme.

## Run it (on the Mac)
Double-click `Open FileHub.command`, or run:

    python3 filehub.py

FileHub updates itself: every time it starts (from the Dock, the launcher or Terminal) and
every 30 minutes while it runs, it fetches changes made in the cloud with `git pull`, and
the open page refreshes by itself. The launcher also runs `git pull` first. If there is
no internet or the update fails, it just starts the version you already have. If an older
FileHub is still running, it restarts it so the update takes effect.

It opens http://127.0.0.1:8765 (only this computer can reach it). It never moves
or deletes your files, and only renames one when you choose Rename (in the same folder,
never over another file). Splitting a PDF, pasting and duplicating always make new files,
and a paste never replaces a file that is already there.

USB sticks and external disks show up under "Drives" in the sidebar by themselves, with an
eject button; copy, move and paste work on them like any folder. "Move to Bin" (⌘⌫) only
ever puts things in the Bin, and can be undone.

Select two or more PDFs and press Merge to join them (drag into order, pick page ranges).
Open a PDF full-screen and press Edit to annotate it: highlight, underline, strike through,
text, sticky notes, pen, shapes and arrows; save as a new copy or into the PDF (undoable).

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

## Scanning

The scanner button in the top bar finds printers and scanners on your Wi-Fi (anything that supports
AirScan / eSCL, which is most network scanners made since about 2015). Choose colour, glass or feeder,
sharpness and paper size, press Scan, then turn, remove or reorder the pages and save them as JPEG,
PNG, one PDF per page or one PDF with every page. Scans go to Documents › Scans unless you pick
another folder. If a scanner isn't found, "Add by address" takes its IP address.

## FileHub as a Mac app

`FileHub.app` in this folder is a double-clickable app: it starts FileHub quietly (no Terminal window)
and opens it in your browser. Drag it to the Dock and choose Options › Keep in Dock to have it one click
away. Keep the app inside the FileHub folder (it finds the folder next to it; if you move the app, it
remembers where the folder was). What it prints goes to `~/Library/Logs/FileHub.log`.
If you downloaded the folder as a zip rather than with git, the first time you open the app right-click
it and choose Open, because macOS asks before running apps from the internet.
To rebuild the app after changing its icon or launcher: `python3 tools/make_app.py` (needs Pillow).

**Permissions.** Because it is a real app, macOS asks FileHub itself for permission the first time it
needs something: your Documents, Desktop and Downloads folders, controlling Finder (for the Bin and
Share), and finding scanners on the local network. Click Allow / OK on those. The Bin needs one more
step on newer Macs: System Settings › Privacy & Security › Full Disk Access › **+** › add FileHub.app
(FileHub shows a button that opens the right place and another that shows the app in Finder).

## If another program is using FileHub's port

FileHub normally runs at http://127.0.0.1:8765. If another program has taken that port (for example a
`python3 -m http.server` from another project, which shows a plain "Directory listing" page), FileHub says
so in its window and uses the next free port, such as 8766, and opens the browser there.

