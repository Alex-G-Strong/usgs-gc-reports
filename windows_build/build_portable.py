"""Builds a fully self-contained, no-installer-needed "portable Python" bundle -
the fallback for a computer whose security policy won't let a compiled .exe run
at all. The result is a plain folder (no installer, no admin rights, no registry
writes) containing a real Python interpreter plus this app's code and every
dependency it needs, launched via a .bat file.

Why not just ship the official "embeddable" Python zip on its own: it deliberately
excludes tkinter (no _tkinter.pyd, no tcl/tk DLLs, no Lib/tkinter) to keep it
small, and this app is a Tkinter GUI - so those pieces are copied in from a real,
already-installed Python of the *same* version on this build machine (the "donor").
Everything else (pip, matplotlib/numpy/openpyxl/Pillow) is installed normally
into the bundle's own site-packages, so the target computer never touches the
internet.

Usage: python windows_build/build_portable.py
Requires: a working internet connection on THIS (build) machine only, and a
full (non-embeddable) install of the same Python version to source tkinter from.
"""

import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_VERSION = "3.11.9"
PY_VERSION_NOTAG = PY_VERSION.replace(".", "")  # "3119" -> used in the _pth filename
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

OUT_DIR = ROOT / "portable" / "USGS_GC_Reports_Portable"
PY_DIR = OUT_DIR / "python"
APP_DIR = OUT_DIR / "app"


def run(cmd, **kwargs):
    print(">", " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, **kwargs)


def find_donor_python():
    """Locates a full (non-embeddable) install of the same Python version
    already on this machine, to copy tkinter's DLLs/.pyd/stdlib package from -
    the embeddable distribution never ships these."""
    base_prefix = Path(sys.base_prefix)
    if (base_prefix / "DLLs" / "_tkinter.pyd").exists():
        return base_prefix
    raise SystemExit(
        f"Could not find _tkinter.pyd under {base_prefix} - this build script must be "
        f"run with a full (non-embeddable) Python {PY_VERSION} install that has tkinter, "
        "since the embeddable distribution doesn't ship it."
    )


def main():
    if OUT_DIR.exists():
        print(f"Removing previous build at {OUT_DIR}")
        shutil.rmtree(OUT_DIR)
    PY_DIR.mkdir(parents=True)
    APP_DIR.mkdir(parents=True)

    donor = find_donor_python()
    print(f"Using donor Python (for tkinter) at: {donor}")

    # -- 1. download + extract the embeddable interpreter ------------------------
    zip_path = ROOT / "windows_build" / f"python-{PY_VERSION}-embed-amd64.zip"
    if not zip_path.exists():
        print(f"Downloading {EMBED_URL}")
        urllib.request.urlretrieve(EMBED_URL, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(PY_DIR)

    # -- 2. graft tkinter in from the donor install -------------------------------
    shutil.copy2(donor / "DLLs" / "_tkinter.pyd", PY_DIR / "_tkinter.pyd")
    for dll in ("tcl86t.dll", "tk86t.dll"):
        shutil.copy2(donor / "DLLs" / dll, PY_DIR / dll)
    (PY_DIR / "Lib").mkdir(exist_ok=True)
    shutil.copytree(donor / "Lib" / "tkinter", PY_DIR / "Lib" / "tkinter", dirs_exist_ok=True)
    shutil.copytree(donor / "tcl" / "tcl8.6", PY_DIR / "tcl" / "tcl8.6", dirs_exist_ok=True)
    shutil.copytree(donor / "tcl" / "tk8.6", PY_DIR / "tcl" / "tk8.6", dirs_exist_ok=True)

    # -- 3. enable site-packages + the loose Lib/ dir on sys.path -----------------
    # The embeddable dist ships a restrictive python311._pth that (a) comments out
    # "import site" (so pip-installed packages would never be found) and (b) only
    # points at its own zipped stdlib. Both are needed here.
    pth_files = list(PY_DIR.glob("python3*._pth"))
    if not pth_files:
        raise SystemExit(f"Could not find a python3*._pth file under {PY_DIR}")
    pth_path = pth_files[0]
    lines = pth_path.read_text().splitlines()
    new_lines = []
    for line in lines:
        if line.strip() == "#import site":
            new_lines.append("import site")
        else:
            new_lines.append(line)
    if "Lib" not in new_lines:
        new_lines.append("Lib")
    if "Lib\\site-packages" not in new_lines:
        new_lines.append("Lib\\site-packages")
    # A ._pth file's presence disables the normal behavior of prepending the
    # launched script's own directory to sys.path - without this, "import
    # gc_pipeline" fails even though app/gc_pipeline sits right there next to
    # main.py. ..\app is relative to this file's own directory (python\), i.e.
    # the sibling app\ folder - confirmed by a real launch failing without it
    # (ModuleNotFoundError: No module named 'gc_pipeline').
    if "..\\app" not in new_lines:
        new_lines.append("..\\app")
    pth_path.write_text("\n".join(new_lines) + "\n")

    # -- 4. bootstrap pip, then install this app's real dependencies -------------
    get_pip_path = ROOT / "windows_build" / "get-pip.py"
    if not get_pip_path.exists():
        print(f"Downloading {GET_PIP_URL}")
        urllib.request.urlretrieve(GET_PIP_URL, get_pip_path)
    python_exe = PY_DIR / "python.exe"
    # PYTHONNOUSERSITE matters here: enabling "import site" above also enables
    # per-user site-packages lookup (site.getusersitepackages(), keyed off the
    # OS user profile - not this interpreter's own prefix). Without this, pip
    # sees this build machine's *already-installed* system Python packages under
    # %APPDATA%, reports them "already satisfied," and skips installing anything
    # into the bundle's own site-packages - producing a bundle that only happens
    # to work on this machine and is broken everywhere else. Confirmed by
    # inspecting the bundle's site-packages after a first attempt: only pip's own
    # bootstrap packages were present, not matplotlib/numpy/openpyxl/Pillow.
    env = {**__import__("os").environ, "PYTHONNOUSERSITE": "1"}
    run([str(python_exe), str(get_pip_path), "--no-warn-script-location"], env=env)
    run([
        str(python_exe), "-m", "pip", "install", "--no-warn-script-location",
        "-r", str(ROOT / "requirements.txt"),
    ], env=env)

    # -- 5. copy the app itself ----------------------------------------------------
    shutil.copytree(ROOT / "gc_pipeline", APP_DIR / "gc_pipeline", dirs_exist_ok=True,
                     ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "main.py", APP_DIR / "main.py")
    shutil.copytree(ROOT / "assets", APP_DIR / "assets", dirs_exist_ok=True)

    # -- 6. launcher scripts --------------------------------------------------------
    # PYTHONNOUSERSITE=1 keeps this bundle from ever picking up packages from
    # some *other* Python install's per-user site-packages on whatever machine
    # it's run on (see the build-time comment above) - this bundle's own
    # site-packages should be the only place packages are found.
    (OUT_DIR / "Launch USGS GC Reports.bat").write_text(
        '@echo off\r\n'
        'REM No console window (pythonw.exe) - the normal, double-click launch.\r\n'
        'set PYTHONNOUSERSITE=1\r\n'
        'start "" "%~dp0python\\pythonw.exe" "%~dp0app\\main.py"\r\n'
    )
    (OUT_DIR / "Launch (debug console).bat").write_text(
        '@echo off\r\n'
        'REM Keeps a console window open so errors are visible - use this one if\r\n'
        'REM the normal launcher silently doesn\'t open anything.\r\n'
        'set PYTHONNOUSERSITE=1\r\n'
        '"%~dp0python\\python.exe" "%~dp0app\\main.py"\r\n'
        'pause\r\n'
    )
    (OUT_DIR / "README.txt").write_text(
        "USGS GC Reports - portable edition\r\n"
        "\r\n"
        "No installation needed and no admin rights required - this folder contains\r\n"
        "its own copy of Python plus everything the app needs.\r\n"
        "\r\n"
        "To run: double-click \"Launch USGS GC Reports.bat\".\r\n"
        "If nothing appears to happen, try \"Launch (debug console).bat\" instead - it\r\n"
        "keeps a window open showing any error message.\r\n"
        "\r\n"
        "This whole folder can be copied anywhere (a USB drive, another computer,\r\n"
        "Desktop, Documents) and still works - nothing outside this folder is used.\r\n"
        "A database file (gc_data.sqlite3) will be created inside app\\ the first\r\n"
        "time it runs.\r\n"
    )

    print(f"\nPortable bundle built at: {OUT_DIR}")
    print('Run it via: "Launch USGS GC Reports.bat"')


if __name__ == "__main__":
    main()
