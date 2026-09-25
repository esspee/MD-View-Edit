#!/usr/bin/env python3
"""
build_and_package.py
---------------------
Runs the Nuitka --standalone build for markdown_viewer.py, then packages
the result in whatever form makes sense for the host OS:

  * Linux   -- feeds the resulting *.dist folder into appimage_builder_gui.py
               (CLI mode) to produce a single-file .AppImage.
  * Windows -- keeps the standalone *.dist folder and renames it to
               "Markdown Viewer" (a self-contained app directory you can
               zip and hand out; no AppImage equivalent exists here).

The platform is detected automatically; use --target to override it (e.g.
--target windows to test the packaging path on another OS).

Usage:
    python build_and_package.py

Build inputs (icons, vendored tooling) live under assets/:

    assets/common/            markdown-viewer-icon.png, markdown-viewer-icon.ico
    assets/portable-linux/    appimage_builder_gui.py, appimagetool*
    assets/portable-windows/  Windows-specific tooling, if ever needed

Vendored copies take priority, so a checkout builds against known versions.
Everything there is optional: without a vendored appimage_builder_gui.py the
script falls back to PATH and /usr/local/bin, and without a vendored
appimagetool appimage_builder_gui.py downloads its own as before.

Icons degrade gracefully too -- a missing .png means appimage_builder_gui.py
uses its built-in generic AppImage icon, and a missing .ico means the .exe
keeps Nuitka's default. Never blocks the build; it's just nicer to ship a
real app icon.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---- Asset layout ----------------------------------------------------------
# Everything the build needs that isn't source code lives under assets/:
#
#   assets/common/           artwork and other platform-neutral inputs
#   assets/portable-linux/   vendored Linux build tooling (appimage_builder_gui.py,
#                            appimagetool) so the build doesn't depend on
#                            system-installed copies or a network download
#   assets/portable-windows/ vendored Windows build tooling, if any is ever needed
#
# Vendored tooling is preferred over anything installed on the machine, but the
# old PATH / /usr/local/bin lookups remain as fallbacks so existing build
# machines keep working. See default_builder_script().
ASSETS = HERE / "assets"
ASSETS_COMMON = ASSETS / "common"
ASSETS_LINUX = ASSETS / "portable-linux"
ASSETS_WINDOWS = ASSETS / "portable-windows"


def assets_dir_for(target: str) -> Path:
    """Returns the platform-specific assets folder for a packaging target."""
    return ASSETS_WINDOWS if target == "windows" else ASSETS_LINUX


def detect_target() -> str:
    """Maps the host platform onto a packaging strategy."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    fail(
        f"Unsupported platform: {sys.platform}. This script packages for "
        "Linux (AppImage) and Windows (standalone app directory) only. "
        "Use --target to force one of those packaging paths."
    )


def default_builder_script() -> Path:
    """Locates appimage_builder_gui.py (Linux packaging only).

    Resolution order: vendored copy in assets/portable-linux/ > PATH lookup >
    /usr/local/bin > next to this script (the historical default). The
    vendored copy wins so a checkout builds reproducibly against a known
    version, but the system lookups remain so machines that already rely on
    an installed copy keep working.
    """
    vendored = ASSETS_LINUX / "appimage_builder_gui.py"
    if vendored.is_file():
        return vendored
    on_path = shutil.which("appimage_builder_gui.py")
    if on_path:
        return Path(on_path)
    usr_local = Path("/usr/local/bin/appimage_builder_gui.py")
    if usr_local.is_file():
        return usr_local
    return HERE / "appimage_builder_gui.py"


def find_appimagetool() -> Path | None:
    """Locates a vendored appimagetool in assets/portable-linux/, if present.

    appimage_builder_gui.py downloads appimagetool itself when it can't find
    one, which needs network access and pins nothing. A vendored binary here
    is passed through to it instead. Returns None to let it fall back to its
    own download behaviour.
    """
    if not ASSETS_LINUX.is_dir():
        return None
    candidates = sorted(ASSETS_LINUX.glob("appimagetool*"))
    for c in candidates:
        if c.is_file():
            return c
    return None

# ---- Nuitka build config ---------------------------------------------------
SCRIPT = "markdown_viewer.py"
EXEC_NAME = "Markdown_Viewer"          # matches --output-filename below
APP_NAME = "Markdown Viewer"
VERSION = "1.0.0"
# Same artwork in two formats; both are platform-neutral inputs, so they live
# under assets/ rather than in either portable-* folder.
ICON = ASSETS_COMMON / "markdown-viewer-icon.png"      # Linux: AppImage .desktop entry


def _first_existing(*candidates: Path) -> Path:
    """Returns the first candidate that exists, else the first candidate
    (so callers still have a sensible path to name in a 'not found' message)."""
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


# Windows wants a .ico (not the .png). The shipped icon is assets/common/logo.ico;
# a couple of alternative names/locations are accepted as fallbacks so an
# existing checkout keeps working without renaming.
WINDOWS_ICON = _first_existing(
    ASSETS_COMMON / "logo.ico",                    # shipped icon
    ASSETS_COMMON / "markdown-viewer-icon.ico",    # alt name
    ASSETS / "logo.ico",                           # old location (pre-move)
)
CATEGORIES = "Office;"

COMMON_NUITKA_ARGS = [
    "--standalone",
    "--enable-plugin=pyside6",
    "--include-package=markdown.extensions",
    "--include-package=pygments",
    "--include-package=pymdownx",
    "--include-module=pymdownx.highlight",
    "--include-module=pymdownx.superfences",
    "--include-module=pymdownx.emoji",
    "--include-module=pymdownx.tasklist",
    "--include-module=pymdownx.tilde",
    "--include-module=pymdownx.caret",
    "--include-module=pymdownx.mark",
    "--include-module=pymdownx.magiclink",
    "--include-module=pymdownx.gemoji_db",
    "--include-module=pymdownx.util",
    "--include-module=pymdownx.quotes",
    "--include-module=pygments.formatters.html",
    # NOTE: --noinclude-qt-translations is deliberately NOT used here.
    # It strips qtwebengine_locales/*.pak along with the .qm files, and
    # Chromium's renderer needs at least one locale .pak to initialize --
    # without it you get "could not find translation file for en-US.pak"
    # warnings at runtime. Locale pruning is done after the build instead,
    # in prune_locales(), which keeps English and removes everything else.
    "--nofollow-import-to=PySide6.QtMultimedia",
    "--nofollow-import-to=PySide6.QtMultimediaWidgets",
    "--nofollow-import-to=PySide6.QtSerialPort",
    "--nofollow-import-to=PySide6.QtConnectivity",
    "--nofollow-import-to=PySide6.QtPositioning",
    "--nofollow-import-to=PySide6.QtBluetooth",
    "--nofollow-import-to=PySide6.QtSensors",
    "--nofollow-import-to=PySide6.QtQml",
    "--nofollow-import-to=PySide6.QtQuick",
    "--nofollow-import-to=PySide6.QtPdf",
    "--nofollow-import-to=PyQt6",
    "--nofollow-import-to=IPython",
    "--remove-output",
    # NOTE: --jobs is appended in nuitka_args_for(), computed from the number of
    # CPUs on the build machine rather than hard-coded, so the C-compile stage
    # uses all available cores.
]


def nuitka_args_for(target: str) -> list[str]:
    """Builds the Nuitka argument list for the given packaging target.

    The output filename differs per platform: on Windows Nuitka appends .exe
    itself, and the app must be built windowed (--windows-console-mode=disable)
    so launching it from Explorer doesn't flash a console window behind the GUI.
    """
    # os.cpu_count() can return None in rare/constrained environments; fall
    # back to 8 there, which matches the previous hard-coded value.
    jobs = os.cpu_count() or 8
    args = [*COMMON_NUITKA_ARGS, f"--jobs={jobs}", f"--output-filename={EXEC_NAME}"]

    if target == "windows":
        args.append("--windows-console-mode=disable")
        if WINDOWS_ICON.is_file():
            args.append(f"--windows-icon-from-ico={WINDOWS_ICON}")
        else:
            log(
                f"NOTE: no Windows icon found at {WINDOWS_ICON} -- the .exe will "
                "use Nuitka's default icon. Supply a .ico there for a real one."
            )
    # Linux needs nothing extra here: the icon is applied later by
    # appimage_builder_gui.py via the AppImage's .desktop entry.
    return args


def log(msg: str) -> None:
    print(f"[build_and_package] {msg}")


def fail(msg: str) -> None:
    print(f"[build_and_package] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def run_nuitka(script_path: Path, target: str) -> Path:
    """Runs Nuitka in --standalone mode and returns the resulting .dist dir."""
    if not script_path.is_file():
        fail(f"Script not found: {script_path}")

    cmd = [sys.executable, "-m", "nuitka", *nuitka_args_for(target), str(script_path)]
    log("Running Nuitka:\n  " + " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        fail(f"Nuitka build failed (exit code {result.returncode}).")

    dist_dir = script_path.with_suffix("").parent / f"{script_path.stem}.dist"
    if not dist_dir.is_dir():
        fail(f"Expected Nuitka output folder not found: {dist_dir}")
    log(f"Nuitka build complete: {dist_dir}")
    return dist_dir


def detect_system_locale() -> str:
    """Detects the current system locale, e.g. 'en_US.UTF-8' -> 'en_US'.
    Checks LC_ALL / LC_MESSAGES / LANG env vars first (in the order glibc
    itself resolves them), then falls back to the `locale` command's LANG=
    line on Unix, then finally to Python's own locale module. Returns
    'en_US' if nothing usable is found anywhere (e.g. bare 'C'/'POSIX'
    locale, or a Windows box where none of those env vars are set)."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "").strip()
        if val and val not in ("C", "POSIX", "C.UTF-8"):
            return val.split(".")[0]  # strip encoding suffix, e.g. '.UTF-8'

    # `locale` is a Unix utility; on Windows it's absent (or, worse, shadowed
    # by something unrelated), so this step is skipped there entirely.
    if not sys.platform.startswith("win"):
        try:
            result = subprocess.run(
                ["locale"], capture_output=True, text=True, timeout=3, check=False
            )
            for line in result.stdout.splitlines():
                if line.startswith("LANG="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    if val and val not in ("C", "POSIX", "C.UTF-8"):
                        return val.split(".")[0]
        except (OSError, subprocess.SubprocessError):
            pass

    # On Windows this is the primary source: getlocale() reads the user's
    # regional settings and returns e.g. 'English_United States'; getdefaultlocale()
    # gives the 'en_US' shape we want, so prefer it when it yields something.
    try:
        import locale as locale_module
        try:
            lang, _ = locale_module.getdefaultlocale()
        except (AttributeError, ValueError):
            lang = None
        if not lang or "_" not in lang:
            lang, _ = locale_module.getlocale()
        if lang and lang not in ("C", "POSIX") and "_" in lang:
            return lang
    except (ValueError, TypeError):
        pass

    return "en_US"


def find_pyside6_qtwebengine_locales_dir() -> Path | None:
    """Locates the qtwebengine_locales directory inside the installed
    PySide6 package, used as a repair source if a needed .pak is missing
    from the Nuitka dist folder for any reason."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import PySide6, os; print(os.path.dirname(PySide6.__file__))"],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    pyside6_dir = Path(result.stdout.strip())
    candidates = [
        pyside6_dir / "Qt" / "translations" / "qtwebengine_locales",
        pyside6_dir / "translations" / "qtwebengine_locales",
        pyside6_dir / "qtwebengine_locales",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    for p in pyside6_dir.rglob("qtwebengine_locales"):
        if p.is_dir():
            return p
    return None


def prune_locales(dist_dir: Path, locale_override: str | None = None) -> None:
    """Removes Qt/WebEngine translation and locale files for every locale
    except the current system locale (auto-detected via detect_system_locale).
    Keeps that locale's .pak (required by QtWebEngine's renderer -- removing
    it entirely causes 'could not find translation file' warnings at
    runtime) and its pyphen hyphenation dictionary (used by weasyprint's PDF
    export). Dev-tool translations (designer/linguist/assistant .qm) are
    always removed -- those are Qt Creator's own translations and are never
    used by a compiled PySide6 app, regardless of locale."""
    system_locale = locale_override or detect_system_locale()   # e.g. "en_US"
    lang = system_locale.split("_")[0]               # e.g. "en"
    hyphenated = system_locale.replace("_", "-")      # e.g. "en-US" (pak naming)
    # en-US is always kept regardless of detected locale: it's Chromium's
    # baseline/fallback resource bundle, and the build machine's locale
    # (e.g. a headless root shell) can easily differ from the locale of
    # whatever desktop session actually launches the AppImage later.
    keep_locales = {lang, system_locale, hyphenated, "en", "en-US", "en_US"}
    source = "override" if locale_override else "auto-detected"
    log(f"Locale ({source}): {system_locale} -- keeping files for: {sorted(keep_locales)}")

    qm_patterns = [
        "designer_*.qm", "linguist_*.qm", "assistant_*.qm",   # dev tools, never needed
        "qt_??.qm", "qt_???.qm", "qt_?????.qm",
        "qtbase_*.qm", "qt_help_*.qm", "qtmultimedia_*.qm",
        "qtconnectivity_*.qm", "qtserialport_*.qm", "qtlocation_*.qm",
        "qtdeclarative_*.qm", "qtwebsockets_*.qm", "qtwebengine_*.qm",
    ]
    keep_qm_patterns = [f"*{loc}*.qm" for loc in keep_locales]

    removed_count = 0
    removed_bytes = 0

    for f in dist_dir.glob("*.qm"):
        if any(fnmatch.fnmatch(f.name, pat) for pat in keep_qm_patterns):
            continue
        if any(fnmatch.fnmatch(f.name, pat) for pat in qm_patterns):
            removed_bytes += f.stat().st_size
            f.unlink()
            removed_count += 1

    locales_dir = dist_dir / "qtwebengine_locales"
    if locales_dir.is_dir():
        for f in locales_dir.glob("*.pak"):
            if f.stem in keep_locales:
                continue
            removed_bytes += f.stat().st_size
            f.unlink()
            removed_count += 1

    pyphen_dir = dist_dir / "pyphen" / "dictionaries"
    if pyphen_dir.is_dir():
        for f in pyphen_dir.glob("*"):
            if any(loc in f.name for loc in keep_locales):
                continue
            removed_bytes += f.stat().st_size
            f.unlink()
            removed_count += 1

    mb = removed_bytes / (1024 * 1024)
    log(f"Pruned {removed_count} locale/translation files for other languages ({mb:.1f} MB freed).")

    kept_pak = None
    if locales_dir.is_dir():
        kept_pak = next((locales_dir / f"{loc}.pak" for loc in keep_locales
                          if (locales_dir / f"{loc}.pak").is_file()), None)

    if locales_dir.is_dir() and not kept_pak:
        log("No matching locale .pak survived pruning -- repairing by copying "
            "en-US.pak directly from the installed PySide6 package.")
        source_dir = find_pyside6_qtwebengine_locales_dir()
        if source_dir:
            copied = []
            for loc in sorted(keep_locales):
                src = source_dir / f"{loc}.pak"
                dst = locales_dir / f"{loc}.pak"
                if src.is_file() and not dst.is_file():
                    shutil.copy2(src, dst)
                    copied.append(loc)
            if copied:
                log(f"Repaired: copied {copied} into qtwebengine_locales.")
            else:
                log(f"WARNING: none of {sorted(keep_locales)} found in {source_dir} either -- "
                    "QtWebEngine will still warn about missing translations at runtime.")
        else:
            log("WARNING: could not locate PySide6's qtwebengine_locales directory -- "
                "QtWebEngine will warn about missing translations at runtime.")



def package_windows(dist_dir: Path, output_dir: Path) -> Path:
    """Turns the raw Nuitka *.dist folder into a distributable Windows app
    directory named after the app, placed in output_dir.

    There's no AppImage-style single-file format on Windows, so the
    deliverable is the standalone folder itself: it's fully self-contained
    and runs from anywhere, including a USB stick. Zip it to hand it out.
    """
    app_dir = output_dir / APP_NAME
    exe_name = f"{EXEC_NAME}.exe"

    if not (dist_dir / exe_name).is_file():
        fail(
            f"Expected {exe_name} inside {dist_dir}, but it isn't there -- "
            "the Nuitka build may not have produced a Windows executable."
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    if app_dir.resolve() == dist_dir.resolve():
        log(f"App directory is already in place: {app_dir}")
        return app_dir

    if app_dir.exists():
        log(f"Removing previous app directory: {app_dir}")
        shutil.rmtree(app_dir, ignore_errors=True)
        if app_dir.exists():
            fail(
                f"Could not remove the existing {app_dir} -- it may be open in "
                "Explorer or the app may still be running. Close it and retry."
            )

    log(f"Assembling Windows app directory: {app_dir}")
    shutil.move(str(dist_dir), str(app_dir))

    _bundle_windows_gtk(app_dir)
    _bundle_fonts(app_dir)
    return app_dir


def _bundle_fonts(app_dir: Path) -> None:
    """Copies assets/common/fonts into the app directory (as fonts/), so the
    app can register them at startup via QFontDatabase and render its editor/UI
    fonts consistently on machines that don't have them installed.

    Optional: if the fonts folder is absent the build still succeeds and the app
    falls back to system fonts. See markdown_viewer.py::_load_bundled_fonts,
    which reads from this same fonts/ folder next to the executable.
    """
    src = ASSETS_COMMON / "fonts"
    font_files = [p for p in src.glob("*") if p.suffix.lower() in (".ttf", ".otf")] if src.is_dir() else []
    if not font_files:
        log(
            f"NOTE: no bundled fonts at {src} -- the app will use system fonts. "
            "Drop .ttf/.otf files there to ship them with the app."
        )
        return

    dst = app_dir / "fonts"
    log(f"Bundling {len(font_files)} font file(s) for the app: {src} -> {dst}")
    dst.mkdir(parents=True, exist_ok=True)
    for f in font_files:
        shutil.copy2(f, dst / f.name)


def _bundle_windows_gtk(app_dir: Path) -> None:
    """Copies the vendored GTK3 runtime beside the .exe so WeasyPrint's PDF
    export works on machines without a system-wide GTK3 install.

    WeasyPrint loads native libs (gobject/pango/cairo/fontconfig) at runtime
    via ctypes, which Nuitka can't trace and therefore doesn't bundle. The
    app registers this folder's bin/ on its DLL search path at startup (see
    markdown_viewer.py::_register_bundled_gtk); this step just puts it there.

    The runtime is optional: if assets/portable-windows/gtk3-runtime/ isn't
    present, the build still succeeds, but PDF export will require GTK3 to be
    installed on the target machine. A partial folder (bin/ missing) is
    treated as a misconfiguration and fails the build loudly.
    """
    src = ASSETS_WINDOWS / "gtk3-runtime"
    if not src.is_dir():
        log(
            f"NOTE: no GTK3 runtime at {src} -- the app will build, but PDF export "
            "will only work on machines that have GTK3-Runtime Win64 installed. "
            "Drop the runtime there to make PDF export fully portable."
        )
        return

    if not (src / "bin").is_dir():
        fail(
            f"{src} exists but has no bin/ subfolder -- that's not a usable GTK3 "
            "runtime. Expected gtk3-runtime/bin/ with libgobject-2.0-0.dll etc."
        )

    dst = app_dir / "gtk3-runtime"
    log(f"Bundling GTK3 runtime for WeasyPrint PDF export: {src} -> {dst}")
    shutil.copytree(src, dst, dirs_exist_ok=True)


def run_appimage_builder(
    builder_script: Path,
    dist_dir: Path,
    output_dir: Path,
    keep_appdir: bool,
) -> Path:
    if not builder_script.is_file():
        fail(
            f"appimage_builder_gui.py not found at: {builder_script}\n"
            f"  Vendor it into {ASSETS_LINUX}, pass --builder-script, or put it on PATH."
        )

    cmd = [
        sys.executable, str(builder_script),
        "--name", APP_NAME,
        "--exec-name", EXEC_NAME,
        "--source-dir", str(dist_dir),
        "--version", VERSION,
        "--output-dir", str(output_dir),
        "--categories", CATEGORIES,
        "--comment", "A fast, native Markdown viewer and editor",
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    else:
        log(
            f"NOTE: custom icon not found at {ICON} -- appimage_builder_gui.py "
            "will fall back to its built-in generic AppImage icon instead."
        )
    if keep_appdir:
        cmd.append("--keep-appdir")

    # A vendored appimagetool is handed down via the environment rather than a
    # CLI flag: APPIMAGETOOL is the variable appimagetool-based tooling
    # conventionally honours, and it's inert if appimage_builder_gui.py ignores
    # it -- in which case it just falls back to downloading its own copy.
    env = os.environ.copy()
    vendored_tool = find_appimagetool()
    if vendored_tool:
        if not os.access(vendored_tool, os.X_OK):
            log(f"Making vendored appimagetool executable: {vendored_tool}")
            vendored_tool.chmod(vendored_tool.stat().st_mode | 0o111)
        env["APPIMAGETOOL"] = str(vendored_tool)
        log(f"Using vendored appimagetool: {vendored_tool}")
    else:
        log(
            f"No vendored appimagetool in {ASSETS_LINUX} -- appimage_builder_gui.py "
            "will locate or download one itself."
        )

    log("Running AppImage builder:\n  " + " ".join(cmd))
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        fail(f"AppImage build failed (exit code {result.returncode}).")

    output_dir.mkdir(parents=True, exist_ok=True)
    matches = sorted(output_dir.glob(f"*{VERSION}*.AppImage"))
    return matches[-1] if matches else output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", default=SCRIPT, type=Path,
                         help=f"Path to the main Python file to compile (default: {SCRIPT})")
    parser.add_argument("--target", choices=("linux", "windows"), default=None,
                         help="Packaging target. Defaults to the host OS: linux -> .AppImage, "
                              "windows -> standalone app directory.")
    parser.add_argument("--builder-script", default=None, type=Path,
                         help="Path to appimage_builder_gui.py (Linux target only; defaults to "
                              "assets/portable-linux/, then PATH, then /usr/local/bin, then "
                              "this script's directory)")
    parser.add_argument("--output-dir", default=HERE, type=Path,
                         help="Where the finished .AppImage / app directory should end up "
                              "(default: this script's directory)")
    parser.add_argument("--keep-appdir", action="store_true",
                         help="Keep the intermediate AppDir for inspection/debugging (Linux target only)")
    parser.add_argument("--skip-nuitka", action="store_true",
                         help="Skip the Nuitka build and package an existing .dist folder")
    parser.add_argument("--no-prune-locales", action="store_true",
                         help="Skip pruning non-matching-locale translation/locale files from the dist folder")
    parser.add_argument("--locale",
                         help="Override auto-detected locale (e.g. 'de_DE') used for pruning, "
                              "instead of reading it from the build machine's environment")
    parser.add_argument("--keep-dist", action="store_true",
                         help="Keep the Nuitka *.dist folder after a successful AppImage build "
                              "(Linux only; by default it's deleted, since the AppImage is "
                              "self-contained. On Windows the dist folder becomes the app "
                              "directory, so it's always kept.)")
    args = parser.parse_args()

    target = args.target or detect_target()
    log(f"Packaging target: {target} ({'explicit --target' if args.target else 'auto-detected'})")

    script_path = args.script.resolve()
    output_dir = args.output_dir.resolve()

    if args.skip_nuitka:
        dist_dir = script_path.with_suffix("").parent / f"{script_path.stem}.dist"
        if not dist_dir.is_dir():
            fail(f"--skip-nuitka given but {dist_dir} does not exist.")
        log(f"Skipping Nuitka build, using existing folder: {dist_dir}")
    else:
        dist_dir = run_nuitka(script_path, target)

    if not args.no_prune_locales:
        prune_locales(dist_dir, locale_override=args.locale)

    if target == "windows":
        app_dir = package_windows(dist_dir=dist_dir, output_dir=output_dir)
        log(f"Done. Windows app directory: {app_dir}")
        log(f"Run it via: {app_dir / (EXEC_NAME + '.exe')}")
        log("This folder is self-contained -- zip it to distribute.")
        return

    builder_script = (args.builder_script or default_builder_script()).resolve()
    appimage_path = run_appimage_builder(
        builder_script=builder_script,
        dist_dir=dist_dir,
        output_dir=output_dir,
        keep_appdir=args.keep_appdir,
    )

    log(f"Done. AppImage output directory: {output_dir}")
    if appimage_path.is_file():
        log(f"Built: {appimage_path}")

    if args.keep_dist:
        log(f"Keeping dist folder (--keep-dist): {dist_dir}")
    elif appimage_path.is_file():
        log(f"Cleaning up dist folder: {dist_dir}")
        shutil.rmtree(dist_dir, ignore_errors=True)
    else:
        log(f"AppImage build did not produce a recognizable output file -- "
            f"leaving {dist_dir} in place so you can inspect it.")


if __name__ == "__main__":
    main()
