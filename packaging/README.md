# Packaging

Two ways to distribute this app to a lab machine, for two different security
postures:

1. **`USGS_GC_Reports.spec` → a single .exe** (via PyInstaller). Simplest for a
   normal machine. Some institutional/lab computers block running unsigned,
   unknown .exe files outright (SmartScreen, application whitelisting) — if
   that's the case, use option 2 instead.
2. **`build_portable.py` → a portable Python folder**, no installer, no admin
   rights, nothing written outside the folder itself. Use this when option 1 is
   blocked.

Both ship with a desktop-usable icon (`assets/icon.ico`) and neither one bundles
`gc_data.sqlite3` — the app creates a fresh database next to itself the first
time it runs, so the target machine always starts clean.

Each build output (the .exe's folder, the portable bundle's folder) is a single
unit — every file in it has to stay together, or it breaks (e.g. the .exe alone,
without its `_internal\` folder, won't run). After building, run
`set_folder_icon.ps1` against the output folder to give it a custom Explorer
icon — a visual cue that it's one bundle, not a folder to pick files out of:

```
powershell -ExecutionPolicy Bypass -File packaging\set_folder_icon.ps1 -FolderPath "dist\USGS_GC_Reports"
powershell -ExecutionPolicy Bypass -File packaging\set_folder_icon.ps1 -FolderPath "portable\USGS_GC_Reports_Portable"
```

## Option 1: the .exe (PyInstaller)

```
packaging\build_exe.bat
```

(or directly: `python -m PyInstaller packaging\USGS_GC_Reports.spec --noconfirm`,
run from anywhere — the spec resolves all its paths relative to itself, not the
current directory.)

Output: `dist\USGS_GC_Reports\USGS_GC_Reports.exe` plus a `_internal\` folder next
to it — **copy the whole `USGS_GC_Reports` folder**, not just the .exe, to the
target machine. Requires PyInstaller (`pip install pyinstaller`); everything else
gets bundled automatically from whatever's installed in the build environment
(see `requirements.txt`).

## Option 2: the portable Python bundle

```
python packaging\build_portable.py
```

Output: `portable\USGS_GC_Reports_Portable\` — copy this whole folder anywhere
(USB drive, network share, the target machine's Desktop) and run
`Launch USGS GC Reports.bat`. No install step of any kind on the target machine.

### How it works, and the two non-obvious problems it works around

The official Python "embeddable" distribution (what this is built from) is
deliberately stripped down: no pip, no tkinter, and — this is the part that broke
the first attempt at this — no automatic sys.path setup for a script launched
from outside its own folder.

1. **No tkinter.** The embeddable zip ships no `_tkinter.pyd`, no `tcl86t.dll`/
   `tk86t.dll`, no `Lib/tkinter`. Since this app is a Tkinter GUI, the build
   script copies all of these from a real, already-installed Python of the
   *same version* already on the build machine (the "donor" — found via
   `sys.base_prefix`). This means **you must build this on a machine that has a
   full, working, tkinter-capable Python 3.11.9 install**, not just the
   embeddable zip.
2. **Per-user site-packages leak across interpreters.** Enabling `import site`
   (needed so pip-installed packages are found at all) also enables
   `site.getusersitepackages()`, which is keyed off the *OS user profile*, not
   the interpreter doing the lookup — so `pip install` inside the bundle can see
   the build machine's *other*, already-installed Python's per-user packages and
   report them "already satisfied," silently skipping the actual install into
   the bundle. Confirmed by inspecting a first-attempt build: only pip's own
   bootstrap packages ended up in the bundle's `site-packages`, nothing else.
   Fixed by setting `PYTHONNOUSERSITE=1` for every `pip install` at build time —
   and in the launcher `.bat` files too, so the same thing can't happen on
   whatever machine the bundle is eventually run on.
3. **A `._pth` file disables the "add the launched script's own folder to
   sys.path" behavior** that normal `python.exe some/script.py` invocations get
   for free — without an explicit fix, `import gc_pipeline` fails even though
   `app\gc_pipeline` sits right next to `app\main.py`. Fixed by appending a
   relative `..\app` line to the bundle's `python3xx._pth` file (relative to
   the `python\` folder the interpreter itself lives in).

All three fixes live in `build_portable.py` with comments at the point they're
applied — read there for the exact mechanics if something needs adjusting for a
future Python version bump.

### Rebuilding after a dependency change

Add the new package to `requirements.txt` (also install it in your normal dev
environment) and re-run `build_portable.py` — it always wipes and rebuilds the
`portable/` output from scratch, so there's no stale-package risk.

## What's intentionally excluded from both builds

- `gc_data.sqlite3` and `RAWs/` — real lab data, never shipped. See the root
  `.gitignore`.
- Anything under `.claude/` — local tool config, not app code.
