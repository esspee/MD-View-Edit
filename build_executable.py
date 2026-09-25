#!/usr/bin/env python3
"""
build_launcher.py
-----------------
Build a *tiny* (KB-sized) launcher executable for a Python GUI script — by
default file_obfuscator.py — for the OS this script is run on.

Unlike a frozen build (PyInstaller/Nuitka), this does NOT bundle Python, Qt or
any dependency. It produces a small native launcher that simply runs the given
.py file with the Python interpreter and libraries ALREADY installed on the
machine. That is why it can be kilobytes instead of hundreds of megabytes — the
tradeoff is that the target machine must already have Python + the script's
dependencies (PySide6, cryptography) installed.

Platform behaviour (based on the OS currently running):
  * Linux  -> compiles a small C stub into a real ELF binary (a few KB).
              Falls back to a POSIX shell launcher if no C compiler is found.
  * Windows -> writes a .cmd launcher and, if pythonw.exe is available, wires it
               to run without a console window. (A true .exe needs a compiler;
               the .cmd is the dependency-free KB-sized equivalent.)
  * macOS / other POSIX -> a shell launcher.

Usage
-----
    python3 build_launcher.py                    # launcher for file_obfuscator.py
    python3 build_launcher.py path/to/app.py     # launcher for another script
    python3 build_launcher.py --name Obfuscator  # custom launcher filename
    python3 build_launcher.py --output-dir ~/bin
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _defaults_from_build_and_package() -> tuple[Path | None, str | None]:
    """Pull the script path and exec name build_and_package.py already knows.

    Looked up next to the CURRENT WORKING DIRECTORY (not this script's own
    location) so one shared copy of build_executable.py can be run from many
    project folders, each with its own build_and_package.py / app name, and
    pick up that folder's own defaults rather than always falling back to
    wherever build_executable.py itself happens to live.

    Loaded by file path (not sys.path + import) so a same-named
    build_and_package.py in a different folder is never served stale from
    sys.modules.

    Two schemas are in use across projects: the newer one has MAIN_SCRIPT
    (a Path) plus OS-specific WIN_OUTPUT_FILENAME/LINUX_EXEC_NAME; the older
    one has SCRIPT (a filename string, relative to build_and_package.py's own
    HERE) plus a single EXEC_NAME. APP_NAME is the last-resort name fallback
    on either schema.
    """
    candidate = Path.cwd() / "build_and_package.py"
    if not candidate.is_file():
        return None, None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_bap_defaults", candidate)
        bap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bap)

        if hasattr(bap, "MAIN_SCRIPT"):
            script = bap.MAIN_SCRIPT
        elif hasattr(bap, "SCRIPT"):
            base = getattr(bap, "HERE", candidate.parent)
            script = Path(base) / bap.SCRIPT
        else:
            script = None

        if platform.system() == "Windows" and hasattr(bap, "WIN_OUTPUT_FILENAME"):
            name = bap.WIN_OUTPUT_FILENAME
        elif platform.system() != "Windows" and hasattr(bap, "LINUX_EXEC_NAME"):
            name = bap.LINUX_EXEC_NAME
        elif hasattr(bap, "EXEC_NAME"):
            name = bap.EXEC_NAME
        elif hasattr(bap, "APP_NAME"):
            name = bap.APP_NAME
        else:
            name = None

        return script, name
    except Exception:
        return None, None


def _sanitize_filename(name: str) -> str:
    """Turn an app name like "OMR Database Export & Analysis" into a safe
    filename stem: spaces to underscores, everything else that a filesystem
    or shell could choke on stripped out."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", name)
    return name.strip("_")


_BAP_SCRIPT, _BAP_EXEC_NAME = _defaults_from_build_and_package()
DEFAULT_SCRIPT = _BAP_SCRIPT if _BAP_SCRIPT is not None else ""
DEFAULT_NAME = _sanitize_filename(_BAP_EXEC_NAME) if _BAP_EXEC_NAME else "run_app"


def log(msg: str) -> None:
    print(f"[build_launcher] {msg}")


def fail(msg: str) -> None:
    print(f"[build_launcher] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def python_gui_exe() -> str:
    """Best interpreter to launch a GUI app with, for the current OS.

    On Windows prefer pythonw.exe (no console window); elsewhere the running
    interpreter's path. This is baked into the launcher as an absolute path so
    it keeps working regardless of the user's PATH / double-click environment.
    """
    exe = Path(sys.executable)
    if platform.system() == "Windows":
        candidate = exe.with_name("pythonw.exe")
        if candidate.is_file():
            return str(candidate)
    return str(exe)


def script_reference(script: Path, out_dir: Path) -> str:
    """Return the script's absolute path, baked into the launcher as-is."""
    return str(script)

def make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# ── Linux: compile a small C ELF stub ───────────────────────────────────────
_C_STUB = r"""
/* Auto-generated launcher — absolute interpreter, relative script path.
 * Uses /proc/self/exe to locate its own directory, then resolves the script
 * beside it. The interpreter path is baked in so double-click works even
 * when PATH doesn't include Python. */
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>
#include <libgen.h>

static const char *PYTHON  = "%(python)s";
static const char *SCRIPT_REL = "%(script)s";

int main(int argc, char **argv) {
    char self[PATH_MAX];
    ssize_t n = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (n < 0) { perror("cannot locate self"); return 127; }
    self[n] = 0;

    char selfcopy[PATH_MAX];
    snprintf(selfcopy, sizeof(selfcopy), "%%s", self);
    char *dir = dirname(selfcopy);

    char script[PATH_MAX];
    if (SCRIPT_REL[0] == '/')
        snprintf(script, sizeof(script), "%%s", SCRIPT_REL);
    else
        snprintf(script, sizeof(script), "%%s/%%s", dir, SCRIPT_REL);

    if (access(script, R_OK) != 0) {
        fprintf(stderr, "launcher: script not found beside me: %%s\n", script);
        return 127;
    }

    char *args[argc + 3];
    int i2 = 0;
    args[i2++] = (char *)PYTHON;
    args[i2++] = script;
    for (int i = 1; i < argc; i++) args[i2++] = argv[i];
    args[i2] = 0;
    execv(PYTHON, args);
    perror("launch failed");
    return 127;
}
"""


def build_linux_elf(python_exe: str, script: Path, out: Path) -> bool:
    """Compile the C stub into a stripped ELF. Returns True on success."""
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        return False

    def c_escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    csrc = _C_STUB % {"python": c_escape(python_exe), "script": c_escape(str(script))}
    csrc_path = out.with_suffix(".launcher.c")
    csrc_path.write_text(csrc)
    try:
        # -Os small, -s strip symbols -> smallest ELF.
        cmd = [cc, "-Os", "-s", "-o", str(out), str(csrc_path)]
        log("Compiling ELF launcher:\n  " + " ".join(cmd))
        r = subprocess.run(cmd)
        if r.returncode != 0:
            log("C compile failed; will fall back to a shell launcher.")
            return False
    finally:
        csrc_path.unlink(missing_ok=True)
    make_executable(out)
    return True


def build_posix_shell(python_exe: str, script: Path, out: Path) -> None:
    """Fallback (and macOS default): a KB-sized POSIX shell launcher."""
    content = (
        "#!/bin/sh\n"
        "# Auto-generated launcher — absolute interpreter, relative script.\n"
        'dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        f'script="$dir/{script}"\n'
        '[ -r "$script" ] || { echo "launcher: script not found: $script" >&2; exit 127; }\n'
        f'exec "{python_exe}" "$script" "$@"\n'
    )
    out.write_text(content)
    make_executable(out)


def build_windows_cmd(python_exe: str, script: Path, out: Path) -> Path:
    """Windows: a .cmd launcher (KB-sized, no compiler needed). Uses pythonw so
    no console window appears for the GUI app."""
    cmd_path = out.with_suffix(".cmd")
    content = (
        "@echo off\r\n"
        "rem Auto-generated launcher — absolute interpreter, relative script.\r\n"
        "setlocal\r\n"
        f'set "SCRIPT=%~dp0{script}"\r\n'
        'if not exist "%SCRIPT%" (\r\n'
        '    echo Launcher error: cannot find "%SCRIPT%"\r\n'
        '    pause\r\n'
        '    exit /b 127\r\n'
        ')\r\n'
        f'start "" "{python_exe}" "%SCRIPT%" %*\r\n'
    )
    cmd_path.write_text(content, encoding="ascii", errors="replace")
    return cmd_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("script", nargs="?", default=DEFAULT_SCRIPT or None, type=Path,
                        help=f"Python script to launch (default: {DEFAULT_SCRIPT or 'ask'})")
    parser.add_argument("--name", default=None,
                        help=f"Base name of the launcher (default: {DEFAULT_NAME})")
    parser.add_argument("--output-dir", default=HERE, type=Path,
                        help="Where the launcher is written (default: this script's dir)")
    parser.add_argument("--python", default=None,
                        help="Interpreter to bake into the launcher "
                             "(default: the one running this build script)")
    parser.add_argument("--shell", action="store_true",
                        help="On Linux, force a shell launcher instead of compiling an ELF")
    args = parser.parse_args()

    script = args.script
    if script is None:
        # build_and_package.py couldn't be imported (missing/moved), so we
        # have no app name/script to fall back on -- ask rather than guess.
        entered = input("Path to the Python script to launch: ").strip()
        if not entered:
            fail("No script provided.")
        script = Path(entered)
    script = script.resolve()
    if not script.is_file():
        fail(f"Script not found: {script}")

    name = args.name
    if name is None:
        name = DEFAULT_NAME
        if not DEFAULT_NAME or DEFAULT_NAME == "run_app":
            entered = input(
                f"Launcher name [{DEFAULT_NAME or 'run_app'}]: ").strip()
            name = entered or DEFAULT_NAME or "run_app"

    python_exe = args.python or python_gui_exe()
    if not Path(python_exe).exists():
        log(f"WARNING: interpreter path does not exist: {python_exe}")

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Compute a relative path from the launcher directory to the script,
    # so the executable is portable and not tied to an absolute path.
    script_ref = script_reference(script, out_dir)

    system = platform.system()
    log(f"Detected OS: {system}  ·  script ref: {script_ref}")

    if system == "Windows":
        produced = build_windows_cmd(python_exe, script_ref, out_dir / name)
    elif system == "Linux":
        out = out_dir / name
        if args.shell or not build_linux_elf(python_exe, script_ref, out):
            build_posix_shell(python_exe, script_ref, out)
        produced = out
    else:  # macOS or other POSIX
        produced = out_dir / name
        build_posix_shell(python_exe, script_ref, produced)

    size = produced.stat().st_size
    kb = size / 1024
    log("Done.")
    log(f"Launcher: {produced}  ({size} bytes ≈ {kb:.1f} KB)")
    log("Double-click it (or run it from a terminal) to start the app.")
    log("NOTE: the target machine must already have Python plus the app's "
        "dependencies (PySide6, cryptography) installed.")


if __name__ == "__main__":
    main()
