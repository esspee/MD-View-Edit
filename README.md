# assets/

Build inputs for `build_and_package.py` that aren't source code: artwork and
vendored build tooling.

```
assets/
  common/            platform-neutral inputs (icons)
  portable-linux/    vendored Linux build tooling
  portable-windows/  vendored Windows build tooling
```

## Why "portable"

The `portable-*` folders hold tooling vendored into the checkout rather than
installed on the build machine. Vendored copies take priority over anything
found on `PATH`, so a given checkout builds against a known version instead of
whatever the machine happens to have (or whatever the network serves that day).

Everything here is **optional** — the build degrades gracefully when a file is
missing, and says so in its log output. Nothing in `assets/` blocks a build.

## What goes where

### `common/`

| File | Used by | Missing? |
| --- | --- | --- |
| `markdown-viewer-icon.png` | Linux — the AppImage's `.desktop` entry | `build_appimage_gui.py` uses its built-in generic icon |
| `markdown-viewer-icon.ico` | Windows — Nuitka's `--windows-icon-from-ico` | The `.exe` keeps Nuitka's default icon |

Both files are the same artwork in two formats, so they're shared source
material rather than per-platform binaries — hence `common/` and not the
`portable-*` folders.

### `portable-linux/`

| File | Used by | Missing? |
| --- | --- | --- |
| `build_appimage_gui.py` | The Linux packaging step | Falls back to `PATH`, then `/usr/local/bin`, then the repo root |
| `appimagetool*` | `build_appimage_gui.py`, passed down as `$APPIMAGETOOL` | `build_appimage_gui.py` locates or downloads its own |

The vendored `appimagetool` is `chmod +x`'d automatically if it isn't already —
the executable bit doesn't survive some transfers (zip archives, Windows
checkouts).

### `portable-windows/`

Empty. The Windows build currently needs no external tooling: Nuitka produces
the standalone directory directly, and the app spawns no external executables
at runtime. The folder exists so that any future Windows-only tool has an
obvious home.
