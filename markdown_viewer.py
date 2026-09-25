#!/usr/bin/env python3
"""
=====================================================================================
 Lightweight Markdown Viewer  —  PySide6 + python-markdown
=====================================================================================
A small, dependency-light desktop Markdown viewer/editor with:

  - Live split-pane preview (Markdown source  |  rendered output)
  - Syntax-highlighted code blocks (Pygments)
  - Full color theming (Light / Dark) for headers, links, quotes, tables, code
  - Emoji support, both as typed-unicode (😀) and GitHub-style shortcodes (:smile:)
  - Tables, task lists, strikethrough, fenced code, footnotes, etc. (GFM-ish)
  - Drag & drop a .md file onto the window to open it
  - Export the rendered preview to a standalone HTML file

Install:
    pip install PySide6 markdown pymdown-extensions pygments

Run:
    python markdown_viewer.py [optional_file.md]

Notes on emoji rendering:
    Qt draws emoji using whatever color-emoji font your OS has installed.
      - Windows / macOS: works out of the box (Segoe UI Emoji / Apple Color Emoji)
      - Linux: install a color emoji font, e.g.  sudo apt install fonts-noto-color-emoji
    Shortcodes like :rocket: are converted to the real unicode character at render
    time using pymdown-extensions' offline emoji index (no network access needed).
=====================================================================================
"""

import re
import sys
import os
import hashlib
import tempfile
import functools
from collections import OrderedDict
from pathlib import Path


def _register_bundled_gtk() -> None:
    """Make a GTK3 runtime bundled next to the executable discoverable.

    WeasyPrint's PDF export depends on native GTK libraries (gobject, pango,
    cairo, fontconfig) that it loads at runtime via ctypes.find_library() --
    i.e. by DLL name, off the process DLL search path. Nuitka can't see those
    ctypes loads, so they aren't bundled automatically; the Windows build
    ships a gtk3-runtime/ folder beside the .exe instead (see
    build_and_package.py::package_windows).

    This registers that folder's bin/ on the DLL search path and points
    fontconfig at the bundled config, so PDF export works on a machine with
    no system-wide GTK3 install. It's a no-op off Windows and a no-op when
    the bundled folder isn't present (e.g. running from source, where GTK is
    expected to be found on the system PATH as before) -- so it never breaks
    the uncompiled dev workflow.
    """
    if not sys.platform.startswith("win"):
        return

    # The executable's own directory: sys.frozen/Nuitka put the .exe (and the
    # bundled gtk3-runtime/ next to it) here; from source this is the repo dir
    # and the folder simply won't exist, short-circuiting below.
    exe_dir = Path(sys.executable).resolve().parent
    gtk_root = exe_dir / "gtk3-runtime"
    gtk_bin = gtk_root / "bin"
    if not gtk_bin.is_dir():
        return

    try:
        os.add_dll_directory(str(gtk_bin))       # Python 3.8+ DLL search
    except (OSError, AttributeError):
        pass
    # Belt-and-braces: prepend to PATH too, since some ctypes lookups and
    # child GTK modules still consult it rather than the added directory.
    os.environ["PATH"] = str(gtk_bin) + os.pathsep + os.environ.get("PATH", "")

    # Point fontconfig at the bundled config/cache; without this it looks for
    # a system fontconfig and either errors or renders with fallback fonts.
    fonts_conf = gtk_root / "etc" / "fonts" / "fonts.conf"
    if fonts_conf.is_file():
        os.environ.setdefault("FONTCONFIG_FILE", str(fonts_conf))
        os.environ.setdefault("FONTCONFIG_PATH", str(fonts_conf.parent))


# Must run before anything imports weasyprint (which resolves its native libs
# at import time), so it lives at module load rather than inside main().
_register_bundled_gtk()


def _down_triangle_icon_path() -> str:
    """Path to a small down-arrow PNG for combo drop-arrows, rendered from the
    '⏷' (U+23F7) glyph.

    Drawn once to a temp file and cached. We reference a real file path in the
    stylesheet's url(...) rather than an inline SVG/data-URI: Qt's stylesheet
    parser rejects data-URIs containing raw '<', '>', '#' and spaces, which
    silently breaks the WHOLE stylesheet ("Could not parse stylesheet ..."). A
    plain forward-slashed file path parses cleanly. Returns "" on failure, in
    which case the caller omits the rule and Qt keeps the native arrow."""
    cached = globals().get("_DOWN_TRIANGLE_PATH")
    if cached:
        return cached
    result = ""
    try:
        from PySide6.QtGui import QPixmap, QPainter, QColor, QFont
        from PySide6.QtCore import Qt as _Qt

        size = 14
        pm = QPixmap(size, size)
        pm.fill(_Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(QColor("#555555"))          # dark grey
        f = QFont()
        f.setPixelSize(size)
        p.setFont(f)
        p.drawText(pm.rect(), _Qt.AlignCenter, "⏷")   # ⏷ down triangle
        p.end()

        out = Path(tempfile.gettempdir()) / "mdviewer_down_arrow.png"
        pm.save(str(out), "PNG")
        # Qt stylesheet url() wants forward slashes even on Windows.
        result = str(out).replace("\\", "/")
    except Exception:
        result = ""
    globals()["_DOWN_TRIANGLE_PATH"] = result
    return result


def _default_editor_font() -> str:
    """Default monospace family for the editor pane, per OS.

    Windows ships Consolas as its standard programming font; Linux
    distributions don't, so we ask for the generic "monospace" family and let
    fontconfig map it to whatever the system's default fixed-width font is.
    (macOS falls through to "Menlo".) The user can still override this via the
    toolbar font picker; _apply_editor_font enforces fixed-pitch regardless of
    what's chosen, so a bad name degrades gracefully rather than breaking.
    """
    if sys.platform.startswith("win"):
        return "Consolas"
    if sys.platform == "darwin":
        return "Menlo"
    return "monospace"


def _bundled_fonts_dir() -> Path | None:
    """Locate the bundled fonts directory, whether running from source or from
    a Nuitka build. Returns None if it isn't present."""
    candidates = []
    # Compiled build: fonts ship next to the executable (see
    # build_and_package.py::_bundle_windows_fonts).
    candidates.append(Path(sys.executable).resolve().parent / "fonts")
    # Running from source: assets/common/fonts next to this file.
    candidates.append(Path(__file__).resolve().parent / "assets" / "common" / "fonts")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _load_bundled_fonts() -> int:
    """Register every .ttf/.otf under the bundled fonts dir with Qt, so the
    app's editor/UI fonts render identically on machines that don't have them
    installed system-wide. Must be called AFTER QApplication is constructed
    (QFontDatabase needs a running app). Returns the number of fonts loaded;
    a no-op returning 0 when no bundled fonts dir exists (e.g. a lean source
    checkout), so it never breaks startup."""
    from PySide6.QtGui import QFontDatabase

    fonts_dir = _bundled_fonts_dir()
    if not fonts_dir:
        return 0
    loaded = 0
    for f in sorted(fonts_dir.iterdir()):
        if f.suffix.lower() in (".ttf", ".otf") and f.is_file():
            if QFontDatabase.addApplicationFont(str(f)) != -1:
                loaded += 1
    return loaded

from PySide6.QtCore import Qt, QTimer, QUrl, Signal, QPointF, QRectF, QObject, QThread
from PySide6.QtGui import (
    QAction, QKeySequence, QFont, QTextCursor,
    QSyntaxHighlighter, QTextCharFormat, QColor,
    QDesktopServices, QImage, QTextDocument,
    QPainter, QPainterPath, QPen, QTextTable, QPalette,
)
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QPlainTextEdit, QTextBrowser,
    QFileDialog, QToolBar, QWidget, QMessageBox, QLabel,
    QSpinBox, QMenu, QMenuBar, QComboBox, QToolButton, QLineEdit, QPushButton,
    QCheckBox, QAbstractSpinBox, QHBoxLayout,
)

import markdown

try:
    from pymdownx.emoji import gemoji, to_alt
    HAS_PYMDOWNX = True
except ImportError:
    HAS_PYMDOWNX = False


# --------------------------------------------------------------------------------- #
#  Markdown -> HTML conversion
# --------------------------------------------------------------------------------- #

def build_extensions(pygments_style: str = "monokai"):
    """Pick the richest extension set available without hard-failing if optional
    packages are missing, so the app stays usable with just `markdown` installed.
    pygments_style must be a valid Pygments style name; it is passed through to
    the highlight extension with noclasses=True so every token span gets an
    inline style="color:..." attribute — Qt's QTextBrowser cannot apply CSS
    class-selector rules (.k, .nf, …) so class-based colouring is invisible."""
    exts = [
        "extra",           # tables, footnotes, attr_list, abbr, etc.
        "toc",              # heading anchors
        "sane_lists",
        "nl2br",
    ]
    configs = {}
    if HAS_PYMDOWNX:
        exts += [
            # highlight+superfences (drop-in replacement for codehilite) keeps the
            # fenced-code language as a CSS class, so we can render a Claude-style
            # "python" / "bash" header strip above each block.
            "pymdownx.highlight",
            "pymdownx.superfences",
            "pymdownx.emoji",
            "pymdownx.tasklist",
            "pymdownx.tilde",      # ~~strikethrough~~
            "pymdownx.caret",      # ^^insert^^
            "pymdownx.mark",       # ==highlight==
            "pymdownx.magiclink",  # auto-link bare URLs
        ]
        configs["pymdownx.highlight"] = {
            "guess_lang": False,
            "css_class": "codehilite",
            "pygments_lang_class": True,
            "pygments_style": pygments_style,
            "noclasses": True,     # inline style= on every span; Qt can't do CSS classes
        }
        configs["pymdownx.emoji"] = {
            "emoji_index": gemoji,
            "emoji_generator": to_alt,  # renders the literal unicode char (offline, lightweight)
        }
        configs["pymdownx.tasklist"] = {"custom_checkbox": True}
    else:
        # Bare-bones fallback: still get highlighted fenced code, just without
        # the language-name header (that needs pymdownx, which isn't installed).
        exts += ["codehilite"]
        configs["codehilite"] = {
            "guess_lang": False,
            "css_class": "codehilite",
            "pygments_style": pygments_style,
            "noclasses": True,     # inline style= on every span
        }
    return exts, configs


def _ensure_blank_line_before_fences(text: str) -> str:
    """Guarantee a blank line precedes every fenced code block opener.

    Markdown's fence rule only starts a code block cleanly when it's set off
    from preceding text by a blank line; a ``` or ~~~ glued directly to the
    line above it can get swallowed into a paragraph or list item instead of
    rendering as its own code section. Insert the missing blank line rather
    than relying on every source file to already have one.
    """
    fence_re = re.compile(r'^([ \t]*)(`{3,}|~{3,})\s*(.*)$')
    lines = text.split('\n')
    out: list[str] = []
    in_fence = False
    fence_marker = ""
    fence_len = 0
    for line in lines:
        m = fence_re.match(line)
        if m and not in_fence:
            # Opening fence: make sure it's preceded by a blank line.
            if out and out[-1].strip() != '':
                out.append('')
            out.append(line)
            in_fence = True
            fence_marker = m.group(2)[0]
            fence_len = len(m.group(2))
        elif (in_fence and m and m.group(2)[0] == fence_marker
              and len(m.group(2)) >= fence_len and m.group(3) == ''):
            # Closing fence: same character, at least as long as the opener,
            # and (per CommonMark) nothing but whitespace after it.
            out.append(line)
            in_fence = False
        else:
            out.append(line)
    return '\n'.join(out)


def _preprocess_markdown(text: str) -> str:
    """Strip block-level HTML wrappers that prevent the Markdown parser from
    seeing the content inside them (e.g. <div align="center"> around tables).
    The Markdown spec treats raw HTML block tags as opaque — content inside
    them is never re-parsed as Markdown, so tables/lists inside <div> tags
    render as literal pipe characters instead of proper HTML tables."""
    # Remove <div ...> and </div> lines that wrap Markdown content.
    # Only remove standalone tag lines (the entire line is just the tag) so we
    # don't accidentally strip inline divs that are part of actual HTML content.
    text = re.sub(r'(?im)^[ \t]*<div\b[^>]*>[ \t]*$', '', text)
    text = re.sub(r'(?im)^[ \t]*</div>[ \t]*$', '', text)
    text = _ensure_blank_line_before_fences(text)
    text = _normalize_insert_syntax(text)
    return text


# ++text++ is the GitHub/common-Markdown syntax for inserted (underlined) text,
# but pymdownx.caret only recognizes ^^text^^. Rewrite the ++...++ form to the
# ^^...^^ form the extension understands, so both spellings render as <ins>.
_INSERT_PLUSPLUS_RE = re.compile(r'\+\+(?=\S)(.+?)(?<=\S)\+\+')


def _normalize_insert_syntax(text: str) -> str:
    """Convert ++inserted++ to ^^inserted^^ outside of code spans/fences.

    Backtick code spans and fenced blocks are left untouched so literal ``++``
    inside code (e.g. C++'s increment operator) is never rewritten."""
    out = []
    in_fence = False
    fence_re = re.compile(r'^\s*(```|~~~)')
    for line in text.split('\n'):
        if fence_re.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        # Protect inline `code` spans, rewrite ++...++ only in the gaps between.
        parts = re.split(r'(`+[^`]*`+)', line)
        for i, seg in enumerate(parts):
            if i % 2 == 0:  # even segments are outside backticks
                parts[i] = _INSERT_PLUSPLUS_RE.sub(r'^^\1^^', seg)
        out.append(''.join(parts))
    return '\n'.join(out)


def render_markdown(text: str, pygments_style: str = "monokai") -> str:
    exts, configs = build_extensions(pygments_style)
    md = markdown.Markdown(extensions=exts, extension_configs=configs)
    return md.convert(_preprocess_markdown(text))


# Maps file extensions to (pygments-lexer-alias, display-label) for source
# files dropped/opened directly (non-Markdown mode).
_SOURCE_LEXERS: dict[str, tuple[str, str]] = {
    ".py":    ("python",     "Python"),
    ".pyw":   ("python",     "Python"),
    ".js":    ("javascript", "JavaScript"),
    ".mjs":   ("javascript", "JavaScript"),
    ".ts":    ("typescript", "TypeScript"),
    ".tsx":   ("tsx",        "TypeScript JSX"),
    ".jsx":   ("jsx",        "JavaScript JSX"),
    ".java":  ("java",       "Java"),
    ".c":     ("c",          "C"),
    ".h":     ("c",          "C Header"),
    ".cpp":   ("cpp",        "C++"),
    ".cc":    ("cpp",        "C++"),
    ".cxx":   ("cpp",        "C++"),
    ".hpp":   ("cpp",        "C++ Header"),
    ".cs":    ("csharp",     "C#"),
    ".go":    ("go",         "Go"),
    ".rs":    ("rust",       "Rust"),
    ".rb":    ("ruby",       "Ruby"),
    ".php":   ("php",        "PHP"),
    ".swift": ("swift",      "Swift"),
    ".kt":    ("kotlin",     "Kotlin"),
    ".kts":   ("kotlin",     "Kotlin"),
    ".scala": ("scala",      "Scala"),
    ".sh":    ("bash",       "Shell"),
    ".bash":  ("bash",       "Bash"),
    ".zsh":   ("bash",       "Zsh"),
    ".fish":  ("fish",       "Fish"),
    ".ps1":   ("powershell", "PowerShell"),
    ".lua":   ("lua",        "Lua"),
    ".r":     ("r",          "R"),
    ".R":     ("r",          "R"),
    ".sql":   ("sql",        "SQL"),
    ".css":   ("css",        "CSS"),
    ".scss":  ("scss",       "SCSS"),
    ".sass":  ("sass",       "Sass"),
    ".json":  ("json",       "JSON"),
    ".yaml":  ("yaml",       "YAML"),
    ".yml":   ("yaml",       "YAML"),
    ".toml":  ("toml",       "TOML"),
    ".xml":   ("xml",        "XML"),
    ".html":  ("html",       "HTML"),
    ".htm":   ("html",       "HTML"),
    ".ini":   ("ini",        "INI"),
    ".cfg":   ("ini",        "Config"),
    ".conf":  ("nginx",      "Config"),
    ".dockerfile": ("docker", "Dockerfile"),
    ".tf":    ("hcl",        "Terraform"),
    ".dart":  ("dart",       "Dart"),
    ".ex":    ("elixir",     "Elixir"),
    ".exs":   ("elixir",     "Elixir"),
    ".hs":    ("haskell",    "Haskell"),
    ".jl":    ("julia",      "Julia"),
    ".vim":   ("vim",        "Vim Script"),
    ".cmake": ("cmake",      "CMake"),
    ".m":     ("matlab",     "MATLAB"),
}

# Matches the wrapper div pymdownx.highlight emits for a fenced block with a
# Matches the opening tag of a codehilite div that carries a language class,
# e.g. <div class="language-python codehilite" style="background: #272822">.
_CODE_LANG_RE = re.compile(r'<div class="language-([\w+.#-]+) codehilite"[^>]*>')

# Matches a codehilite div with NO language class (fenced block without a tag).
_CODE_NOLANG_RE = re.compile(r'<div class="codehilite"[^>]*>')

# Vertical padding on the label table's <td>.  Must match the constant used in
# _paint_rounded_code_borders to compute the true outer top of the pane.
_LABEL_PAD_TOP = 10
_LABEL_PAD_BOT = 5


def _lang_label(lang: str) -> str:
    return "plain text" if lang == "text" else lang


POST_PROCESSED_MARKER = "<!--mdv-processed-->"


def post_process_html(body_html: str, stripe_color: str = "", code_bg: str = "#f6f8fa", code_fg: str = "#24292f", lang_px: int = 11, code_px: int = 14, border_color: str = "#444", page_bg: str = "#ffffff", label_fg: str = "#24292f", wrap_code: bool = False, label_bg: str = "", label_border: str = "") -> str:
    """HTML touch-ups shared by the live preview and HTML export, so exporting
    always matches what's on screen.

    NOT idempotent: it converts <pre> code blocks into per-row tables, adds
    label headers, and stamps table borders -- running it a second time on its
    own output stacks a second label + treats the code table as a data table,
    producing the "doubled dark bar + invisible code" artifact. So when the
    input already carries POST_PROCESSED_MARKER (e.g. a previously-exported
    README.html re-opened via the HTML-source path), it's returned unchanged."""
    if POST_PROCESSED_MARKER in body_html:
        return body_html
    # The language-label header sits on its own background. It defaults to the
    # page background (on a dark on-screen pane, page_bg reads as a distinct
    # lighter header strip). For the light PDF pane, callers pass an explicit
    # label_bg so the header is a shade darker than the code body and still reads
    # as a header instead of blending into the page.
    if not label_bg:
        label_bg = page_bg
    # Qt's QTextBrowser rich-text engine does NOT support CSS `border-collapse`,
    # so a CSS-only table border ends up drawn on just some cells (the value
    # columns lose their borders, which looks like a "broken" table). Qt *does*
    # honor the legacy HTML table attributes, so inject them directly. The HTML
    # export (real browsers) ignores these in favour of the stylesheet, so this
    # is safe for both targets.
    body_html = re.sub(
        r'(?i)<table\b((?![^>]*\bborder=)[^>]*)>',
        r'<table border="1" cellspacing="0" cellpadding="4"\1>',
        body_html,
    )
    # Wrap tables so the export/markup keeps the existing hook class.
    body_html = re.sub(r'(?i)(<table\b)', r'<div class="table-wrapper">\1', body_html)
    body_html = re.sub(r'(?i)(</table>)', r'\1</div>', body_html)

    # Qt's CSS engine can't scope tr:nth-child(even) away from <pre> internal rows,
    # so inject bgcolor directly on every even <tr> inside real tables.
    if stripe_color:
        def _stripe_table_rows(m: "re.Match[str]") -> str:
            table_html = m.group(0)
            rows = re.split(r'(?i)(<tr\b[^>]*>)', table_html)
            row_index = 0
            out = []
            for part in rows:
                if re.match(r'(?i)<tr\b', part):
                    row_index += 1
                    if row_index % 2 == 0:
                        part = re.sub(r'(?i)<tr\b', f'<tr bgcolor="{stripe_color}"', part, count=1)
                out.append(part)
            return "".join(out)
        body_html = re.sub(r'(?i)<table\b.*?</table>', _stripe_table_rows, body_html, flags=re.DOTALL)

    # Stamp word-break on every td/th in Markdown tables so long URLs and text
    # wrap instead of overflowing. Qt ignores CSS word-break but does respect
    # inline style on individual cells.
    _style_attr_re = re.compile(r'''style\s*=\s*(["'])''')

    def _add_cell_wrap(m: "re.Match[str]") -> str:
        tag = m.group(0)
        sm = _style_attr_re.search(tag)
        if sm:
            quote = sm.group(1)
            pos = sm.end()
            return tag[:pos] + 'word-break:break-word;' + tag[pos:]
        return tag[:-1] + ' style="word-break:break-word;">'
    body_html = re.sub(r'<t[dh]\b[^>]*>', _add_cell_wrap, body_html)

    # ── Code block rendering ──────────────────────────────────────────────────
    # A single combined pass replaces each codehilite div+pre with a <table>
    # ── Code block label + pane styling ──────────────────────────────────────
    # Qt's QTextBrowser honours inline styles far more reliably than CSS classes,
    # so every property is baked directly onto the element.
    # IMPORTANT: font names in style="..." MUST use single quotes — double
    # quotes terminate the attribute early, dropping color/size/weight.
    mono_inline = ("'JetBrains Mono','Fira Code','Cascadia Code','SF Mono',"
                   "Consolas,'Liberation Mono',Menlo,monospace")

    # Qt ignores CSS padding/margin on block <div>s, so the label is a
    # single-row <table>: the only HTML element where Qt reliably applies
    # td padding for proper vertical centring.  The table sits inside the
    # outer .codehilite div (which provides the dark background fill for
    # the gap between this table and the code table below).
    # A divider under the label strip, used for the light PDF pane so the header
    # reads as a header (matching how the dark on-screen pane separates them).
    _label_divider = (
        f'border-bottom:1px solid {label_border};' if label_border else ''
    )

    def _label_table(text: str) -> str:
        return (
            f'<table cellspacing="0" cellpadding="0" width="100%"'
            f' bgcolor="{label_bg}">'
            f'<tr bgcolor="{label_bg}">'
            f'<td bgcolor="{label_bg}" style="'
            f'padding:{_LABEL_PAD_TOP}px 0 {_LABEL_PAD_BOT}px 18px;{_label_divider}'
            f'font-family:{mono_inline};font-size:{lang_px}px;font-weight:600;color:{label_fg};">'
            f'{text}'
            f'</td></tr></table>'
        )

    def _add_header(m: "re.Match[str]") -> str:
        return m.group(0) + _label_table(_lang_label(m.group(1)))

    body_html = _CODE_LANG_RE.sub(_add_header, body_html)

    body_html = _CODE_NOLANG_RE.sub(
        lambda m: m.group(0) + _label_table('plain text'),
        body_html,
    )

    # The outer .codehilite div gets the pane background stamped as inline style
    # so Qt renders the dark area.  border-radius is kept for HTML export (real
    # browsers); Qt silently ignores it.  border is omitted — the QPainter
    # overlay draws the rounded border to avoid a double-border artifact.
    # No background-color here: the outer div would create a Qt empty-block
    # predecessor that inherits that color and shows as an unwanted dark strip
    # above the label.  The dark pane fill comes from the code table's own
    # bgcolor rows.  border-radius is kept for HTML export only (Qt ignores it).
    _pane_style = (
        f'border-radius:10px;'
        f'margin:1em 0;'
        f'overflow:hidden;'
    )
    # Strip any existing inline style Pygments put on the wrapper div.
    body_html = re.sub(
        r'(<div\b[^>]*?\bcodehilite\b[^>]*?)\s+style="[^"]*"(\s*>)',
        r'\1\2',
        body_html,
    )
    # Inject our pane style onto every codehilite wrapper div.
    body_html = re.sub(
        r'(<div\b[^>]*?\bcodehilite\b[^>]*?)(\s*>)',
        lambda m: f'{m.group(1)} style="{_pane_style}"{m.group(2)}',
        body_html,
    )

    # ── Pre → per-row table conversion ───────────────────────────────────────
    # Each code line becomes a <tr> with explicit bgcolor so Qt's QPalette
    # AlternateBase never zebra-stripes the block.  No color: on cells —
    # Pygments per-span colors win without a competing parent flattening them.
    # This sub runs AFTER the data-table injections above, so code tables are
    # never given border=1 or the data-table stripe colour.
    def _style_pre(m: "re.Match[str]") -> str:
        inner = m.group(1)
        # Strip Pygments sentinel span(s) and <code> wrapper.
        inner = re.sub(r'(?i)^(\s*<span[^>]*>\s*</span>\s*)*<code[^>]*>', '', inner)
        inner = re.sub(r'(?i)</code>\s*$', '', inner)
        inner = re.sub(r'^\n', '', inner)
        inner = re.sub(r'\n$', '', inner)

        # Stamp monospace font + size onto every token span.
        # Qt does NOT inherit font-family from parent elements into <span>s.
        span_font = f'font-family:{mono_inline};font-size:{code_px}px;'
        def _fix_span(sm: "re.Match[str]") -> str:
            tag = sm.group(0)
            am = _style_attr_re.search(tag)
            if am:
                pos = am.end()
                return tag[:pos] + span_font + tag[pos:]
            return tag[:-1] + f' style="{span_font}">'
        inner = re.sub(r'<span[^>]*>', _fix_span, inner)

        lines = inner.split('\n')
        rows  = []
        n     = len(lines)
        for i, ln in enumerate(lines):
            pt = f'{code_px}px' if i == 0     else '0'
            pb = f'{code_px}px' if i == n - 1 else '0'
            ws = 'pre-wrap' if wrap_code else 'pre'
            td_style = (
                f'font-family:{mono_inline};'
                f'font-size:{code_px}px;'
                f'line-height:1.5;'
                f'white-space:{ws};'
                f'padding:{pt} 18px {pb} 18px;'
            )
            content = ln if ln.strip() else '&nbsp;'
            rows.append(
                f'<tr bgcolor="{code_bg}">'
                f'<td bgcolor="{code_bg}" style="{td_style}">{content}</td>'
                f'</tr>'
            )
        return (
            f'<table cellspacing="0" cellpadding="0" width="100%"'
            f' bgcolor="{code_bg}">'
            + ''.join(rows)
            + '</table>'
        )

    body_html = re.sub(
        r'<pre[^>]*>(.*?)</pre>',
        _style_pre,
        body_html,
        flags=re.DOTALL,
    )

    # Mark the output so a re-run (e.g. exported HTML reopened via the
    # HTML-source path) short-circuits at the top instead of double-processing.
    return POST_PROCESSED_MARKER + body_html


def _block_in_table(block) -> bool:
    """True if a QTextBlock sits inside a table cell (data table or the per-line
    code tables). Used to skip such blocks when justifying prose -- table cells
    and code lines must stay left-aligned."""
    cursor = QTextCursor(block)
    frame = cursor.currentFrame()
    while frame is not None:
        if isinstance(frame, QTextTable):
            return True
        frame = frame.parentFrame()
    return False


_INLINE_MONO = ("'JetBrains Mono','Fira Code','Cascadia Code','SF Mono',"
                "Consolas,'Liberation Mono',Menlo,monospace")


def style_inline_marks(body_html: str, theme: dict, code_px: int = 13) -> str:
    """Stamp inline style= onto <mark>/<ins>/<del> and inline <code> so
    QTextBrowser renders them.

    Qt's QTextBrowser CSS engine is fragile: a single rule it can't parse in the
    big preview stylesheet silently drops later rules (including our mark/ins and
    inline-code styling), so ==highlight==, ^^insert^^ and `inline code` came out
    unstyled. Inline style= attributes are always honored, so bake the colors
    straight onto the tags. This runs on the rendered body HTML (not exported
    markup), matching how the codebase already stamps inline styles for tables
    and code blocks."""
    mark_style = (
        f"background-color:{theme['mark_bg']};color:{theme['mark_fg']};"
        "border-radius:3px;padding:0 2px;"
    )
    ins_style = f"text-decoration:underline;color:{theme['fg']};"
    del_style = f"text-decoration:line-through;color:{theme['fg']};"
    # Inline code: light-grey shade, dark-brown text, 1pt smaller than body.
    # NOTE: border-radius is kept for the browser/PDF export path but has no
    # effect in the app preview -- QTextBrowser's rich-text engine does not paint
    # border-radius on HTML content, so the in-app shade is always square-cornered.
    # Block code (<pre><code> with Pygments spans) is styled separately, so
    # only match <code> NOT immediately preceded by a <pre ...> tag.
    code_style = (
        f"background-color:{theme['quote_bg']};color:{theme['inline_code_fg']};"
        f"border-radius:2px;padding:2px 6px;font-size:{code_px}px;"
        f"font-family:{_INLINE_MONO};"
    )
    # Links: QTextBrowser ignores the CSS "a { color }" rule AND the palette's
    # Link role for anchors, painting them in its hardcoded default blue
    # (#0000ff). Stamp the theme link color inline so links actually follow the
    # theme (e.g. the light-blue dark-theme links). Anchors that already carry a
    # style= (e.g. the "Open in browser" button) are left untouched.
    link_style = f"color:{theme['link']};"
    body_html = re.sub(r'<a(?![^>]*\bstyle=)(?=[ >])', f'<a style="{link_style}"', body_html)
    body_html = re.sub(r'<mark(?![^>]*\bstyle=)', f'<mark style="{mark_style}"', body_html)
    body_html = re.sub(r'<ins(?![^>]*\bstyle=)', f'<ins style="{ins_style}"', body_html)
    body_html = re.sub(r'<del(?![^>]*\bstyle=)', f'<del style="{del_style}"', body_html)
    # Style only *inline* <code>. Block code lives inside <pre>...</pre> (with a
    # <span></span> between <pre> and <code>, so adjacency matching is unsafe),
    # so split the HTML on <pre>...</pre> regions and transform <code> only in
    # the segments outside them.
    def _style_inline_code(segment: str) -> str:
        return re.sub(
            r'<code(?![^>]*\bstyle=)>',
            f'<code style="{code_style}">',
            segment,
        )

    parts = re.split(r'(<pre\b.*?</pre>)', body_html, flags=re.DOTALL)
    body_html = ''.join(
        seg if i % 2 else _style_inline_code(seg)   # odd parts are <pre> blocks
        for i, seg in enumerate(parts)
    )
    return body_html


# --------------------------------------------------------------------------------- #
#  Themes (color palettes baked into the preview as CSS)
# --------------------------------------------------------------------------------- #

THEMES = {
    # H1 = deep navy/indigo, H2 = forest green, H3 = burnt orange/sienna
    "Light": {
        "bg": "#ffffff", "fg": "#24292f", "h1": "#1a237e", "h2": "#1b5e20",
        "h3": "#bf360c", "link": "#0969da", "quote_bg": "#f6f8fa",
        "quote_border": "#d0d7de",
        # Dark code block on light page — opposite-tone contrast
        "code_bg": "#1e1e2e", "code_fg": "#cdd6f4",
        "inline_code_fg": "#cf222e",   # red on light quote_bg
        "code_border": "#1e1e2e",      # same as code_bg — border blends into pane
        "table_border": "#d0d7de", "table_stripe": "#f6f8fa", "hr": "#d0d7de",
        "mark_bg": "#fff3a3", "mark_fg": "#24292f",   # ==highlight== yellow
        "editor_bg": "#fafbfc", "editor_fg": "#24292f",
    },
    # H1 = soft sky blue, H2 = mint green, H3 = amber — all readable on dark bg
    "Dark": {
        "bg": "#0d1117", "fg": "#e6edf3", "h1": "#58a6ff", "h2": "#3fb950",
        "h3": "#e3b341", "link": "#79c0ff", "quote_bg": "#161b22",   # light blue links
        "quote_border": "#30363d",
        # Slightly lighter block on dark page
        "code_bg": "#23272e", "code_fg": "#e6edf3",
        "inline_code_fg": "#ff7b72",   # coral on dark quote_bg
        "code_border": "#23272e",      # same as code_bg — border blends into pane
        "table_border": "#30363d", "table_stripe": "#161b22", "hr": "#30363d",
        "mark_bg": "#bb8009", "mark_fg": "#0d1117",   # amber highlight on dark
        "editor_bg": "#0d1117", "editor_fg": "#e6edf3",
    },
    # H1 = deep teal, H2 = dark plum, H3 = warm brown — warm but distinct
    "Sepia": {
        "bg": "#f4efe6", "fg": "#3b2f1e", "h1": "#004d40", "h2": "#4a148c",
        "h3": "#8b4513", "link": "#1a5c9e", "quote_bg": "#ede5d8",
        "quote_border": "#c9a97a",
        # Dark code block contrasts the warm cream page
        "code_bg": "#2a1f0e", "code_fg": "#f0d9b5",
        "inline_code_fg": "#8b4513",   # warm brown on cream quote_bg
        "code_border": "#2a1f0e",      # same as code_bg — border blends into pane
        "table_border": "#c9a97a", "table_stripe": "#ede5d8", "hr": "#c9a97a",
        "mark_bg": "#e9c46a", "mark_fg": "#3b2f1e",   # warm amber on cream
        "editor_bg": "#f9f4ec", "editor_fg": "#3b2f1e",
    },
    # Tokyo Night — modern deep blue-slate dark theme.
    # H1 = blue, H2 = green, H3 = amber — the signature Tokyo Night accents.
    "Tokyo Night": {
        "bg": "#1a1b26", "fg": "#a9b1d6", "h1": "#7aa2f7", "h2": "#9ece6a",
        "h3": "#e0af68", "link": "#79c0ff", "quote_bg": "#24283b",   # light blue links
        "quote_border": "#414868",
        # Grey code panel — a slate-grey lighter than the blue-slate page, with
        # a defining border, so the code block reads as a distinct grey pane
        # instead of a near-black block (matches one-dark's own #282c34 bg).
        "code_bg": "#2a2e3f", "code_fg": "#c0caf5",
        "inline_code_fg": "#7dcfff",   # tokyo-night cyan: high-contrast on quote_bg
        "code_border": "#3b4160",      # visible edge so the grey pane has definition
        "table_border": "#414868", "table_stripe": "#24283b", "hr": "#414868",
        "mark_bg": "#e0af68", "mark_fg": "#1a1b26",   # amber highlight
        "editor_bg": "#1a1b26", "editor_fg": "#a9b1d6",
    },
    # H1 = nord frost blue, H2 = nord aurora green, H3 = nord aurora yellow
    "Nord": {
        "bg": "#2e3440", "fg": "#d8dee9", "h1": "#5e81ac", "h2": "#a3be8c",
        "h3": "#ebcb8b", "link": "#88ccff", "quote_bg": "#3b4252",   # light blue links
        "quote_border": "#4c566a",
        # Darker than nord polar night so nord3 comment tokens (#616e87) clear 3:1
        "code_bg": "#191c24", "code_fg": "#eceff4",
        "inline_code_fg": "#88c0d0",   # nord frost on dark quote_bg
        "code_border": "#191c24",      # same as code_bg — border blends into pane
        "table_border": "#4c566a", "table_stripe": "#3b4252", "hr": "#4c566a",
        "mark_bg": "#ebcb8b", "mark_fg": "#2e3440",   # nord aurora yellow
        "editor_bg": "#2e3440", "editor_fg": "#d8dee9",
    },
    # Catppuccin Latte — modern warm pastel LIGHT theme.
    # H1 = mauve, H2 = green, H3 = peach — the signature Latte accents.
    "Catppuccin Latte": {
        "bg": "#eff1f5", "fg": "#4c4f69", "h1": "#8839ef", "h2": "#40a02b",
        "h3": "#fe640b", "link": "#1e66f5", "quote_bg": "#e6e9ef",
        "quote_border": "#ccd0da",
        # Dark-grey code panel with light text on the light page (opposite-tone
        # contrast, like the Light/Sepia themes). Uses Catppuccin's own dark
        # surface so it stays on-brand; pairs with a dark Pygments style.
        "code_bg": "#313244", "code_fg": "#cdd6f4",
        "inline_code_fg": "#d20f39",   # latte red: strong contrast on the light quote_bg
        "code_border": "#313244",      # same as code_bg — border blends into pane
        "table_border": "#ccd0da", "table_stripe": "#e6e9ef", "hr": "#ccd0da",
        "mark_bg": "#fff3a3", "mark_fg": "#4c4f69",   # soft yellow highlight
        "editor_bg": "#eff1f5", "editor_fg": "#4c4f69",
    },
}


def build_css(theme: dict, base_font_px: int = 15) -> str:
    """base_font_px drives the whole type scale, so zooming resizes headers AND
    body/code together, in sync."""
    h1_px = round(base_font_px * 1.7)
    h2_px = round(base_font_px * 1.4)
    h3_px = round(base_font_px * 1.2)
    h4_px = round(base_font_px * 1.05)
    h5_px = base_font_px
    h6_px = round(base_font_px * 0.92)
    code_px = base_font_px - 2          # code text: 2pt below body
    lang_px = max(9, base_font_px - 3)  # language label: 3pt below body (1pt below code), shown bold
    mono = ('"JetBrains Mono", "Fira Code", "Cascadia Code", "SF Mono", '
            'Consolas, "Liberation Mono", Menlo, monospace')
    return f"""
    <style>
        body {{
            background-color: {theme['bg']};
            color: {theme['fg']};
            font-family: -apple-system, "Segoe UI", "Noto Sans", "Noto Color Emoji",
                         "Apple Color Emoji", "Segoe UI Emoji", sans-serif;
            font-size: {base_font_px}px;
            line-height: 1.6;
            padding: 6px 14px;
            font-variant-numeric: proportional-nums;
            font-feature-settings: "kern" 1, "liga" 1, "pnum" 1, "tnum" 0;
            text-rendering: optimizeLegibility;
            text-align: justify;
        }}
        h1, h2, h3, h4, h5, h6 {{ font-weight: 600; margin-top: 1.2em; }}
        h1 {{ font-size: {h1_px}px; color: {theme['h1']}; border-bottom: 2px solid {theme['hr']}; padding-bottom: .3em; }}
        h2 {{ font-size: {h2_px}px; color: {theme['h2']}; border-bottom: 1px solid {theme['hr']}; padding-bottom: .2em; }}
        h3 {{ font-size: {h3_px}px; color: {theme['h3']}; }}
        h4 {{ font-size: {h4_px}px; color: {theme['fg']}; }}
        h5 {{ font-size: {h5_px}px; color: {theme['fg']}; }}
        h6 {{ font-size: {h6_px}px; color: {theme['fg']}; opacity: 0.85; }}
        /* Qt's rich-text engine does not reliably inherit body font-size OR
           color into block/table elements, so set both explicitly on each —
           otherwise body text falls back to Qt's default (dark) color and is
           nearly invisible on dark themes. */
        p, li, td, th, dd, dt {{ font-size: {base_font_px}px; color: {theme['fg']}; }}
        ul, ol, dl {{ color: {theme['fg']}; }}
        /* Justify prose blocks (Qt doesn't reliably inherit body text-align into
           blocks). Headings, table cells and code panes are left alone below so
           only running text is justified. */
        p, li, dd {{ text-align: justify; }}
        a {{ color: {theme['link']}; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        blockquote {{
            margin: 0.8em 0; padding: 0.2em 1em;
            background-color: {theme['quote_bg']};
            border-left: 4px solid {theme['quote_border']};
            color: {theme['fg']}; opacity: 0.9;
        }}
        code {{
            background-color: {theme['quote_bg']};
            color: {theme['inline_code_fg']};
            padding: 2px 6px; border-radius: 2px;
            font-family: {mono};
            font-size: {code_px}px;
        }}
        /* ==highlight== (pymdownx.mark) and ^^insert^^/++insert++
           (pymdownx.caret). Qt's QTextBrowser doesn't style <mark> or <ins> by
           default, so give them explicit colors so the app view matches the
           browser: highlighted text gets a colored background, inserted text is
           underlined. <del> (~~strikethrough~~) already renders struck out. */
        mark {{
            background-color: {theme['mark_bg']};
            color: {theme['mark_fg']};
            border-radius: 3px;
            padding: 0 2px;
        }}
        ins {{
            text-decoration: underline;
            color: {theme['fg']};
        }}
        del {{
            text-decoration: line-through;
            color: {theme['fg']};
            opacity: 0.7;
        }}
        /* Bare <pre> — reset so it never draws its own box; the .codehilite
           wrapper (injected by post_process_html) provides the visible pane. */
        pre {{
            background: none;
            padding: 16px 18px;
            border: none;
            border-radius: 0;
            overflow-x: auto;
            font-family: {mono};
            font-size: {code_px}px;
            color: {theme['code_fg']};
            margin: 0;
            line-height: 1.5;
        }}
        pre code {{
            background: none; padding: 0; border: none; color: inherit;
            font-family: {mono};
            font-size: 1em;
        }}
        /* Rounded pane: wraps EVERY code block (fenced + bare <pre>).
           Two selectors cover class="codehilite" AND class="language-X codehilite"
           (pymdownx.superfences emits the language-prefixed form; QTextBrowser's
           CSS engine doesn't support [class^=] partial matching, so both are
           listed explicitly). */
        .codehilite,
        [class*="codehilite"] {{
            background-color: {theme['code_bg']};
            border-radius: 10px;
            border: 1px solid {theme['code_border']};
            overflow: hidden;
            margin: 1em 0;
        }}
        /* <pre> inside the pane: no double border/radius/margin */
        .codehilite pre,
        [class*="codehilite"] pre {{
            background-color: {theme['code_bg']};
            margin: 0;
            border: none;
            border-radius: 0;
            padding: 14px 18px;
            line-height: 1.5;
        }}
        table {{ border-collapse: collapse; margin: 1em 0; }}
        th, td {{ border: 1px solid {theme['table_border']}; padding: 2px 12px; vertical-align: top; text-align: left; line-height: 1.2; }}
        /* Code blocks are rendered as a table with one row per line, so the
           blanket "th, td" border above would draw a rule between every code
           line (only visible in the browser/PDF export; Qt ignores it). Reset
           borders on the code pane's own tables/cells. The ".codehilite td"
           selector outranks "th, td" by specificity, so this wins even against
           the export's later print stylesheet. */
        .codehilite table, .codehilite tr, .codehilite td, .codehilite th,
        [class*="codehilite"] table, [class*="codehilite"] tr,
        [class*="codehilite"] td, [class*="codehilite"] th {{
            border: none;
            text-align: left;   /* never justify code lines */
        }}
        /* The label table and the code-line table are two sibling <table>s
           inside the .codehilite pane. The blanket "table" margin rule
           above adds a 1em gap above/below EACH of them; since that gap sits
           inside the dark pane background it renders as a solid dark bar
           between the label and the code (Qt ignores table margins, so this
           only shows up in the browser/PDF export). Zero it out here. */
        .codehilite table, [class*="codehilite"] table {{
            margin: 0;
        }}
        th {{ background-color: {theme['quote_bg']}; font-weight: 600; }}
        .table-wrapper {{ margin: 1em 0; }}
        hr {{ border: none; border-top: 1px solid {theme['hr']}; margin: 1.5em 0; }}
        img {{ max-width: 100%; }}
        .task-list-item {{ list-style-type: none; }}
        .emoji {{ font-size: 1.05em; }}

    </style>
    """


# Bundled Pygments color schemes per theme, so fenced code blocks get real syntax coloring
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, TextLexer
from pygments.util import ClassNotFound as PygmentsClassNotFound


@functools.lru_cache(maxsize=64)
def _get_lexer_cached(lang: str):
    try:
        return get_lexer_by_name(lang, stripall=False)
    except PygmentsClassNotFound:
        return TextLexer()


@functools.lru_cache(maxsize=32)
def _get_formatter_cached(style: str) -> HtmlFormatter:
    return HtmlFormatter(
        style=style,
        nowrap=True,       # no wrapping <div>/<pre> — we emit our own
        noclasses=True,    # inline style= so QTextBrowser doesn't need a stylesheet
    )


def highlight_source(code: str, lang: str, style: str) -> str:
    """Return a codehilite-wrapped HTML snippet for a raw source file.

    Produces the same ``<div class="language-{lang} codehilite">…</div>``
    structure that the Markdown pipeline emits, so post_process_html can
    convert it to a per-row table with rounded borders unchanged.
    """
    # Lexer/formatter construction is pure w.r.t. (lang, style) and was being
    # rebuilt from scratch on every render (i.e. every debounced keystroke);
    # cache them since re-renders reuse the same handful of combinations.
    lexer = _get_lexer_cached(lang)
    formatter = _get_formatter_cached(style)
    inner_spans = pygments_highlight(code, lexer, formatter)
    # Mimic what pymdownx.highlight emits so post_process_html's regex matches.
    return (
        f'<div class="language-{lang} codehilite">'
        f'<pre><code>{inner_spans}</code></pre>'
        f'</div>'
    )


def pygments_css(style: str, code_bg: str = "", code_fg: str = "") -> str:
    base = _get_formatter_cached(style).get_style_defs('.codehilite')
    # Force the theme's code_bg on the wrapper so Pygments' own background
    # (which varies wildly per style) never bleeds through. Also reset the
    # default text color so uncolored tokens stay readable on any theme bg.
    overrides = ""
    if code_bg:
        overrides += f".codehilite {{ background-color: {code_bg} !important; }}\n"
    if code_fg:
        overrides += f".codehilite {{ color: {code_fg}; }}\n"
    return f"<style>{base}\n{overrides}</style>"


PYGMENTS_STYLE = {
    "Light":            "monokai",   # dark code_bg -> dark Pygments style
    "Dark":             "monokai",
    "Sepia":            "monokai",   # dark code_bg -> dark Pygments style
    "Tokyo Night":      "one-dark",  # vibrant modern tokens matching the palette
    "Nord":             "nord",
    "Catppuccin Latte": "one-dark",  # dark code panel -> dark Pygments style
}


# --------------------------------------------------------------------------------- #
#  Sample document shown on first launch
# --------------------------------------------------------------------------------- #

SAMPLE_MD = """# Lightweight Markdown Viewer 🪶

Welcome! This little app renders **Markdown** with full *color* and emoji support.

## Features ✨

- 🎨 Light / Dark themes
- 🧩 Tables, task lists, and fenced code
- 🚀 Emoji shortcodes like `:rocket:` and raw unicode like 🐍
- 🖍️ ==highlighted text==, ~~strikethrough~~, and ^^inserted^^ text

### Task list

- [x] Parse Markdown
- [x] Render emoji
- [ ] Take over the world 🌍

### Code block

```python
def greet(name: str) -> str:
    return f"Hello, {name}! 👋"

print(greet("World"))
```

### Table

| Language | Typed? | Emoji |
|----------|--------|-------|
| Python   | No*    | 🐍    |
| Rust     | Yes    | 🦀    |
| Go       | Yes    | 🐹    |

### Quote

> "Simplicity is the soul of efficiency." — Austin Freeman 💡

---

Open a `.md` file (**Ctrl+O**) or drag one onto this window to get started.
"""


# --------------------------------------------------------------------------------- #
#  Markdown syntax highlighter for the editor pane (IDE-style)
# --------------------------------------------------------------------------------- #

class MarkdownHighlighter(QSyntaxHighlighter):
    """Lightweight Markdown syntax highlighter for QPlainTextEdit.

    Theme-aware: the editor background is light for light themes and dark for
    dark themes, so two color palettes are kept. The dark palette uses light,
    high-contrast tokens so nothing goes dark-on-dark (which the light palette's
    deep colors would). Call set_dark() when the theme changes."""

    # (role) -> (light-editor color, dark-editor color). Dark values are the
    # light-editor hues lifted to stay readable on a dark editor background.
    _PALETTE = {
        "heading":  ("#1a237e", "#8ab4ff"),   # navy   -> sky blue
        "bold":     ("#bf360c", "#ff8a65"),   # rust   -> coral
        "italic":   ("#6a1a6e", "#d7a3ff"),   # plum   -> light purple
        "code":     ("#1b5e20", "#7ee787"),   # green  -> light green
        "quote":    ("#795548", "#c8a789"),   # brown  -> tan
        "rule":     ("#9e9e9e", "#8b93a7"),   # grey   -> mid grey
        "link":     ("#0969da", "#79c0ff"),   # blue   -> light blue
        "image":    ("#0277bd", "#7cc7ff"),   # blue   -> light blue
        "list":     ("#e65100", "#ffb86c"),   # orange -> light orange
        "checkbox": ("#2e7d32", "#7ee787"),   # green  -> light green
        "fence":    ("#546e7a", "#9aa7b4"),   # slate  -> light slate
        "strike":   ("#9e9e9e", "#8b93a7"),   # grey   -> mid grey
        "html":     ("#0d47a1", "#89b4ff"),   # navy   -> light blue
        "url":      ("#0969da", "#79c0ff"),   # blue   -> light blue
    }

    def __init__(self, parent=None, dark: bool = False):
        super().__init__(parent)
        self._dark = dark
        self._rules: list[tuple[re.Pattern, QTextCharFormat]] = []
        self._build_rules()

    def set_dark(self, dark: bool):
        """Switch the token palette between light- and dark-editor variants and
        re-highlight the whole document."""
        if dark == self._dark:
            return
        self._dark = dark
        self._rules.clear()
        self._build_rules()
        self.rehighlight()

    def _c(self, role: str) -> str:
        light, dark = self._PALETTE[role]
        return dark if self._dark else light

    @staticmethod
    def _fmt(color: str, bold=False, italic=False, size_factor: float = 1.0) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(700)
        if italic:
            fmt.setFontItalic(True)
        return fmt

    def _build_rules(self):
        add = self._rules.append
        c = self._c
        # ATX headings  # ## ###
        add((re.compile(r"^(#{1,6})\s+(.*)"), self._fmt(c("heading"), bold=True)))
        # Bold  **text** or __text__
        add((re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1"), self._fmt(c("bold"), bold=True)))
        # Italic  *text* or _text_
        add((re.compile(r"(\*|_)(?=\S)(.+?)(?<=\S)\1"), self._fmt(c("italic"), italic=True)))
        # Inline code  `code`
        add((re.compile(r"`[^`]+`"), self._fmt(c("code"))))
        # Blockquote  > text
        add((re.compile(r"^>\s+.*"), self._fmt(c("quote"), italic=True)))
        # Horizontal rule --- or *** or ___
        add((re.compile(r"^[-*_]{3,}\s*$"), self._fmt(c("rule"))))
        # Links  [text](url)
        add((re.compile(r"\[([^\]]+)\]\([^\)]+\)"), self._fmt(c("link"))))
        # Images  ![alt](url)
        add((re.compile(r"!\[([^\]]*)\]\([^\)]+\)"), self._fmt(c("image"))))
        # Unordered list markers  - * +
        add((re.compile(r"^(\s*[-*+])\s"), self._fmt(c("list"), bold=True)))
        # Ordered list markers  1. 2.
        add((re.compile(r"^\s*\d+\.\s"), self._fmt(c("list"), bold=True)))
        # Task list checkboxes  - [ ]  - [x]
        add((re.compile(r"\[(x| )\]"), self._fmt(c("checkbox"), bold=True)))
        # Fenced code fence  ``` or ~~~
        add((re.compile(r"^(`{3,}|~{3,}).*"), self._fmt(c("fence"))))
        # Strikethrough  ~~text~~
        add((re.compile(r"~~.+?~~"), self._fmt(c("strike"))))
        # HTML tags
        add((re.compile(r"<[^>]+>"), self._fmt(c("html"))))
        # URLs (bare)
        add((re.compile(r"https?://\S+"), self._fmt(c("url"))))

    def highlightBlock(self, text: str):
        for pattern, fmt in self._rules:
            for m in pattern.finditer(text):
                self.setFormat(m.start(), m.end() - m.start(), fmt)


# --------------------------------------------------------------------------------- #
#  PDF export helper — WeasyPrint
# --------------------------------------------------------------------------------- #


def _pdf_via_weasyprint(html_content: str, pdf_path: str, base_url: str | None = None) -> None:
    """
    Render *html_content* to an A4 PDF at *pdf_path* using WeasyPrint, tuned
    for the best output quality WeasyPrint can produce:

      - presentational_hints: honours any HTML presentational attributes
        (e.g. width/height/bgcolor) in author-supplied HTML sources, not
        just CSS.
      - full_fonts: embeds complete font files rather than subsetting them,
        so every glyph renders correctly in any PDF viewer even if it needs
        a character our own text-extraction/subsetting pass didn't predict.
      - hinting: enables font hinting for crisper glyph rendering.
      - optimize_images + jpeg_quality: recompresses embedded raster images
        losslessly where possible and at high quality otherwise, trimming
        file size without a visible quality hit.
      - custom_metadata: carries the document's <title>/<meta> tags through
        into the PDF's own metadata (so it doesn't show a blank title in
        PDF viewers).
      - pdf_version: targets PDF 1.7 for the widest modern-viewer support.

    WeasyPrint is a pure-Python HTML/CSS -> PDF converter that honours the
    full CSS3 box model, @page rules, backgrounds, borders, and Pygments
    syntax-highlight classes -- producing output that closely matches the
    on-screen preview.

    *base_url* should be the directory the source file lives in (as a
    file:// URL or plain path) so any relative image references in the
    Markdown/HTML resolve correctly during export.

    Install:  pip install weasyprint
    """
    try:
        from weasyprint import HTML as WeasyprintHTML
    except ImportError:
        raise RuntimeError(
            "WeasyPrint is not installed.\n"
            "Run:  pip install weasyprint"
        )

    WeasyprintHTML(string=html_content, base_url=base_url).write_pdf(
        pdf_path,
        presentational_hints=True,
        full_fonts=True,
        hinting=True,
        optimize_images=True,
        jpeg_quality=95,
        custom_metadata=True,
        pdf_version="1.7",
    )


# pymdownx.tasklist (custom_checkbox=True) emits:
#   <label class="task-list-control">
#     <input type="checkbox" disabled checked/><span class="task-list-indicator"></span>
#   </label>
# Browsers draw a real native checkbox widget for that bare <input>, which is
# why printing the HTML from a browser looks fine. WeasyPrint has no native
# widget renderer, so the same markup falls back to a plain filled/empty
# square with no checkmark. Swapping it for a plain-text glyph sidesteps that
# renderer gap entirely instead of depending on CSS/pseudo-class tricks
# WeasyPrint may or may not support.
_TASK_CHECKBOX_RE = re.compile(
    r'<label class="task-list-control">.*?</label>', re.S
)


def _replace_task_checkboxes_for_pdf(html_content: str) -> str:
    def _glyph(match: "re.Match") -> str:
        return "\u2611 " if "checked" in match.group(0) else "\u2610 "  # ☑ / ☐
    return _TASK_CHECKBOX_RE.sub(_glyph, html_content)

# --------------------------------------------------------------------------------- #
#  Preview widget: QTextBrowser that can load remote (http/https) images
# --------------------------------------------------------------------------------- #

class NetworkImageTextBrowser(QTextBrowser):
    """A QTextBrowser whose document can display remote images.

    QTextBrowser only resolves images through its synchronous ``loadResource``
    hook, which natively handles local files but NOT network URLs — so plain
    ``![](https://…)`` markdown shows a broken-image icon. This subclass:

      * fetches http/https images asynchronously via QNetworkAccessManager,
      * caches them in memory (and on disk, so re-opens are instant),
      * hands a 1x1 transparent placeholder back on the first (cache-miss) call,
        then emits ``imageLoaded`` once the download finishes so the view can
        re-render and show the real image.
    """

    imageLoaded = Signal()
    # Emitted with +1 (zoom in) or -1 (zoom out) on Ctrl+wheel, so the main
    # window can drive its CSS-based zoom. Without this, QTextBrowser's built-in
    # Ctrl+wheel zoom intercepts the gesture and only scales point-sized text,
    # leaving our pixel-sized body/table text unchanged.
    zoomRequested = Signal(int)

    # On-disk cache shared across instances/sessions.
    _DISK_CACHE = Path(tempfile.gettempdir()) / "md_viewer_img_cache"
    # Caps on the in-memory decoded-image cache and the on-disk cache directory,
    # so a long session (or many image-heavy documents) can't grow either without
    # bound. Oldest entries are evicted first once the cap is exceeded.
    _MAX_MEMORY_CACHE_ENTRIES = 200
    _MAX_DISK_CACHE_FILES = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self._net = QNetworkAccessManager(self)
        self._net.finished.connect(self._on_finished)
        self._cache: OrderedDict[str, QImage] = OrderedDict()  # url -> decoded QImage, LRU-ordered
        self._failed: set[str] = set()           # urls that failed to load this session
        self._inflight: set[str] = set()         # urls currently downloading
        self._DISK_CACHE.mkdir(parents=True, exist_ok=True)
        # We coalesce many "image arrived" signals into a single re-render.
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(120)
        self._reload_timer.timeout.connect(self.imageLoaded.emit)

    # -- helpers ----------------------------------------------------------------
    @staticmethod
    def _is_remote(url: QUrl) -> bool:
        return url.scheme().lower() in ("http", "https")

    def _disk_path(self, url_str: str) -> Path:
        h = hashlib.sha256(url_str.encode("utf-8")).hexdigest()[:32]
        return self._DISK_CACHE / h

    # -- Qt resource hook -------------------------------------------------------
    def _remember(self, key: str, img: QImage) -> None:
        self._cache[key] = img
        self._cache.move_to_end(key)
        while len(self._cache) > self._MAX_MEMORY_CACHE_ENTRIES:
            self._cache.popitem(last=False)

    def _prune_disk_cache(self) -> None:
        try:
            files = sorted(self._DISK_CACHE.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        excess = len(files) - self._MAX_DISK_CACHE_FILES
        for p in files[:max(excess, 0)]:
            try:
                p.unlink()
            except OSError:
                pass

    def loadResource(self, type_, url: QUrl):
        if isinstance(url, QUrl) and self._is_remote(url):
            key = url.toString()

            # 1) already decoded in memory
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]

            # 2) on disk from a previous session
            dp = self._disk_path(key)
            if dp.exists():
                img = QImage()
                if img.load(str(dp)) and not img.isNull():
                    self._remember(key, img)
                    return img

            placeholder = QImage(1, 1, QImage.Format.Format_ARGB32)
            placeholder.fill(Qt.GlobalColor.transparent)

            # 3) a previous attempt this session already failed — don't hammer
            #    the network again on every re-render.
            if key in self._failed:
                return placeholder

            # 4) not here yet — kick off a download (once) and return the
            #    placeholder so layout doesn't break.
            if key not in self._inflight:
                self._inflight.add(key)
                req = QNetworkRequest(url)
                req.setAttribute(
                    QNetworkRequest.Attribute.RedirectPolicyAttribute,
                    QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
                )
                req.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader,
                              "MarkdownViewer/1.0")
                self._net.get(req)
            return placeholder

        # Local files: if the referenced file doesn't exist on disk (very common
        # when a README pulls in repo-relative images like ./.github/images/x.png
        # that aren't shipped alongside it), Qt would render a bare object-
        # replacement glyph (￼) with no explanation. Return a labelled
        # "missing image" placeholder instead so it's clear what happened.
        # NOTE: Qt passes `type_` as a plain int (2 == ImageResource), so compare
        # by int value rather than against the enum member.
        if isinstance(url, QUrl) and int(type_) == int(QTextDocument.ResourceType.ImageResource):
            local = url.toLocalFile()
            candidates = []
            if local:
                candidates.append(Path(local))
            else:
                # Relative path: resolve against the browser's search paths
                # (set to the source file's folder in _finish_render).
                rel = url.toString()
                if rel.startswith("./"):
                    rel = rel[2:]
                for base in self.searchPaths():
                    candidates.append(Path(base) / rel)
            if candidates and not any(c.exists() for c in candidates):
                name = candidates[0].name
                return self._missing_image_placeholder(name)

        # qrc / data URIs / existing local files: default behaviour.
        return super().loadResource(type_, url)

    @staticmethod
    def _missing_image_placeholder(name: str) -> QImage:
        """A small grey card reading 'image not found: <name>' so a missing
        local image is visible and self-explanatory instead of a bare ￼ glyph."""
        from PySide6.QtGui import QPainter, QColor, QFont, QPen
        label = f"⤫ image not found: {name}"
        # Size the card to the text.
        probe = QImage(1, 1, QImage.Format.Format_ARGB32)
        fm_font = QFont("sans-serif", 9)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(fm_font)
        text_w = fm.horizontalAdvance(label)
        w = min(max(text_w + 24, 140), 420)
        h = 30
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(QColor("#f0ede6"))
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#c8c2b6")))
        p.setBrush(Qt.NoBrush)
        p.drawRect(0, 0, w - 1, h - 1)
        p.setPen(QColor("#8a8477"))
        p.setFont(fm_font)
        p.drawText(img.rect().adjusted(10, 0, -10, 0),
                   Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(label, Qt.ElideMiddle, w - 20))
        p.end()
        return img

    def _on_finished(self, reply: QNetworkReply):
        key = reply.url().toString()
        self._inflight.discard(key)
        try:
            ok = False
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                img = QImage()
                if img.loadFromData(data) and not img.isNull():
                    self._remember(key, img)
                    try:
                        self._disk_path(key).write_bytes(data)
                        self._prune_disk_cache()
                    except OSError:
                        pass  # cache write is best-effort
                    self._reload_timer.start()
                    ok = True
            if not ok:
                self._failed.add(key)
        finally:
            reply.deleteLater()

    # -- rounded code pane borders ----------------------------------------------
    def set_code_pane_style(self, code_bg: str, border_color: str, page_bg: str):
        self._code_bg_qcolor     = QColor(code_bg)
        self._code_border_qcolor = QColor(border_color)
        self._page_bg_qcolor     = QColor(page_bg)
        self._code_px            = 13  # updated per-render; default matches base zoom
        self._code_frame_cache = None  # bg color changed: force re-identification
        self.viewport().update()

    def _code_frames(self):
        """Identify (code_frame, label_frame) pairs once per document, instead
        of re-walking the whole frame tree and re-matching siblings on every
        single paintEvent (which fires on every scroll/resize tick and made
        scrolling documents with many code blocks visibly stutter).

        Identifying "is this table a code pane" by matching its bgcolor=
        against the current theme's code background is the only hook available:
        the table is produced by Qt's HTML importer from a plain
        <table bgcolor="..."> string (see post_process_html), so there's no
        custom QTextFrameFormat property to stamp on it directly.
        """
        doc = self.document()
        cache = getattr(self, '_code_frame_cache', None)
        if cache is not None and cache[0] == doc.revision():
            return cache[1]
        bg_rgb = self._code_bg_qcolor.rgb()
        pairs: list = []

        # Walk each parent's children ONCE, pairing every code table with the
        # table that immediately precedes it in document order (its language
        # header). A code block's label is always the table right before it, so
        # a single pass suffices. The previous approach re-scanned *all* sibling
        # frames for every code frame: when a document has thousands of code
        # blocks that are all top-level siblings (e.g. a big diff report) that
        # is O(n^2) and froze the GUI for ~13s on the first paint. This is O(n).
        def walk(parent):
            prev_table = None
            for child in parent.childFrames():
                if isinstance(child, QTextTable):
                    frame_bg = child.format().background().color()
                    if frame_bg.isValid() and frame_bg.rgb() == bg_rgb:
                        label = prev_table if (
                            prev_table is not None
                            and prev_table.lastPosition() < child.firstPosition()
                        ) else None
                        pairs.append((child, label))
                    prev_table = child
                else:
                    walk(child)

        walk(doc.rootFrame())
        self._code_frame_cache = (doc.revision(), pairs)
        return pairs

    def _paint_rounded_code_borders(self):
        if not hasattr(self, '_code_bg_qcolor'):
            return
        doc = self.document()
        layout = doc.documentLayout()
        offset = QPointF(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())
        radius = 10.0
        border_w = 2.0
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        code_px = getattr(self, '_code_px', 13)
        # Only paint panes that intersect the visible viewport. Building a
        # QPainterPath and filling it for every code block (thousands, in a big
        # diff report) on every scroll tick makes scrolling crawl; culling
        # off-screen panes keeps it smooth since only a handful are ever visible.
        visible_top = self.verticalScrollBar().value()
        visible_bottom = visible_top + self.viewport().height()
        for frame, label_frame in self._code_frames():
            table_rect = layout.frameBoundingRect(frame)
            if table_rect.bottom() < visible_top or table_rect.top() > visible_bottom:
                continue
            # Qt table frameBoundingRect.top() = outer_top + first_cell_pad_top.
            # Subtract that padding to get the true outer boundary of the table.
            pane_x      = doc.documentMargin()
            pane_width  = table_rect.width()
            pane_top    = table_rect.top() - code_px   # fallback if no label
            pane_bottom = table_rect.bottom() - code_px

            # label_frame (the code-block's language-name header table, if any)
            # was already identified in _code_frames(); frameBoundingRect.top()
            # for it = outer_top + _LABEL_PAD_TOP, so subtract that to recover
            # the true outer top of the pane.
            if label_frame is not None:
                lr = layout.frameBoundingRect(label_frame)
                pane_top = lr.top() - _LABEL_PAD_TOP  # label table outer boundary

                # The outer .codehilite div injects an empty block BEFORE the
                # label table. Cover everything above pane_top with page_bg
                # so that pre-block doesn't show as a dark strip.
                cur2 = QTextCursor(doc)
                lpos = label_frame.firstPosition()
                if lpos > 0:
                    cur2.setPosition(lpos - 1)
                    pre = cur2.block()
                    if pre.isValid():
                        pre_rect = layout.blockBoundingRect(pre)
                        # Fill the empty pre-block above the label table with page_bg
                        # so it doesn't show as a dark strip, but do NOT extend
                        # pane_top up to it — the pane border starts at lr.top().
                        # Cover only the area outside (above) the pane border
                        if pre_rect.top() < pane_top:
                            pad_strip = QRectF(
                                pane_x, pre_rect.top(), pane_width,
                                pane_top - pre_rect.top()
                            ).translated(offset)
                            painter.fillRect(pad_strip, self._page_bg_qcolor)

            pane_rect = QRectF(
                pane_x, pane_top, pane_width, pane_bottom - pane_top
            ).translated(offset)
            inner = pane_rect.adjusted(border_w / 2, border_w / 2, -border_w / 2, -border_w / 2)
            rounded_path = QPainterPath()
            rounded_path.addRoundedRect(inner, radius, radius)
            bounding_path = QPainterPath()
            bounding_path.addRect(pane_rect)
            painter.fillPath(bounding_path.subtracted(rounded_path), self._page_bg_qcolor)
            pen = QPen(self._code_border_qcolor, border_w)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(rounded_path)
        painter.end()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    def paintEvent(self, event):
        super().paintEvent(event)
        self._paint_rounded_code_borders()

    # -- zoom -------------------------------------------------------------------
    def wheelEvent(self, event):
        """Intercept Ctrl+wheel so the app drives CSS zoom, not Qt's native
        (point-size-only) zoom which would leave pixel-sized text unchanged."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoomRequested.emit(+1)
            elif delta < 0:
                self.zoomRequested.emit(-1)
            event.accept()
            return
        super().wheelEvent(event)


# --------------------------------------------------------------------------------- #
#  Background render worker
# --------------------------------------------------------------------------------- #

class _RenderWorker(QObject):
    """Runs a zero-arg callable on whatever thread it's moved to and reports
    the result back via a signal, so large-document markdown/Pygments/regex
    post-processing doesn't block the GUI thread."""

    finished = Signal(object)

    def __init__(self, work):
        super().__init__()
        self._work = work

    def run(self):
        self.finished.emit(self._work())


# --------------------------------------------------------------------------------- #
#  Main window
# --------------------------------------------------------------------------------- #

class MarkdownViewer(QMainWindow):
    RENDER_DELAY_MS = 250

    def __init__(self, initial_path: str | None = None):
        super().__init__()
        self.setWindowTitle("Markdown Viewer [*]")
        self.resize(1212, 760)   # narrowed to match the compact font-size control
        self.setAcceptDrops(True)

        self.current_path: Path | None = None
        self._dirty = False              # unsaved edits in the editor pane?
        self._source_mode = "markdown"   # "markdown" | "html" | "source"
        self._source_lang = None          # pygments lexer alias for source mode
        self.theme_name = "Sepia"
        self._zoom_percent = 100
        self._search_matches: list = []
        self._search_index: int = 0

        self._build_ui()
        self._build_toolbar()

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.RENDER_DELAY_MS)
        self._debounce.timeout.connect(self._render)

        if initial_path and Path(initial_path).exists():
            self.open_file(Path(initial_path))
        else:
            self.editor.setPlainText(SAMPLE_MD)
            self._mark_clean()

        self.apply_theme("Sepia")

    def _confirm_discard_changes(self) -> bool:
        """If there are unsaved edits, ask the user what to do. Returns True if
        it's safe to proceed (nothing unsaved, saved successfully, or the user
        chose to discard) and False to abort the pending action (Cancel, or a
        save that failed/was cancelled). Shared by closeEvent and open_file."""
        if not self._dirty:
            return True
        name = self.current_path.name if self.current_path else "this document"
        reply = QMessageBox.warning(
            self, "Unsaved Changes",
            f"You have unsaved changes in {name}.\n\nSave them before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if reply == QMessageBox.Save:
            return self.action_save()   # False if the save failed / was cancelled
        if reply == QMessageBox.Discard:
            return True
        return False   # Cancel

    def closeEvent(self, event):
        # Prompt before losing unsaved edits; abort the close if the user cancels.
        if not self._confirm_discard_changes():
            event.ignore()
            return
        # A background render worker may still be running (large documents);
        # let it finish so Qt doesn't tear down its QThread mid-run, which
        # aborts the process instead of shutting down cleanly.
        for thread, _worker in list(getattr(self, "_active_render_threads", [])):
            thread.wait(2000)
        super().closeEvent(event)

    # ---------------------------------------------------------------- UI build
    def _build_ui(self):
        self.splitter = QSplitter(Qt.Horizontal)

        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._editor_font_family = _default_editor_font()
        self._editor_font_size = 9
        self._apply_editor_font()
        self.editor.textChanged.connect(self._schedule_render)
        # Drive the unsaved-changes state off the document's own modification
        # tracking rather than a one-way "any edit = dirty" flag. QTextDocument
        # fires modificationChanged(False) when the user undoes back to the
        # last-saved content, so the Save button clears itself automatically.
        self.editor.document().modificationChanged.connect(self._set_dirty)
        self.editor.setTabStopDistance(28)
        # Attach Markdown syntax highlighter (IDE-style)
        self._md_highlighter = MarkdownHighlighter(self.editor.document())

        self.preview = NetworkImageTextBrowser()
        # We handle link clicks ourselves so we can (a) open web/mailto links in
        # the system browser/mail client and (b) scroll to in-document anchors.
        self.preview.setOpenLinks(False)
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(self._on_anchor_clicked)
        # When a remote image finishes downloading, re-render to display it.
        self.preview.imageLoaded.connect(self._render)
        # Show the target URL in the status bar while hovering a link.
        self.preview.highlighted.connect(self._on_link_hovered)
        # Ctrl+wheel over the preview drives CSS zoom (see widget.wheelEvent).
        self.preview.zoomRequested.connect(self._on_zoom_requested)

        self.splitter.addWidget(self.editor)
        self.splitter.addWidget(self.preview)

        # Give the splitter breathing room so rounded corners are visible
        self.splitter.setContentsMargins(8, 4, 8, 8)
        self.splitter.setHandleWidth(10)

        # Single-pane default: hide editor
        self.editor.hide()
        self.splitter.setSizes([0, 1])
        self._editor_visible = False

        self.setCentralWidget(self.splitter)

        self.status = self.statusBar()
        self.status.setStyleSheet("QStatusBar::item { border: none; }")
        self.status.setSizeGripEnabled(False)
        self.word_count_label = QLabel("0 words")
        self.status.addPermanentWidget(self.word_count_label)

        # NOTE: the "Enable Text Wrap" checkbox lives in the top toolbar now
        # (after the font-size control) — see _build_toolbar.

        # Text-search controls, placed in the status bar to the right of the
        # Text Wrap checkbox (moved here from the top toolbar). These use
        # addPermanentWidget, NOT addWidget: _on_search_changed calls
        # status.showMessage(), and a normal (non-permanent) status-bar widget
        # gets hidden while a temporary message is shown -- which would hide the
        # search box and "Go To Next" button the moment a search reports its
        # result. Permanent widgets are immune to that and sit on the right.
        search_label = QLabel("  🔍 Search: ")
        self.status.addPermanentWidget(search_label)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Filter text…")
        self.search_box.setFixedWidth(180)
        self.search_box.setFixedHeight(22)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setStyleSheet(
            "QLineEdit {"
            "  background-color: white; color: black;"
            "  border: 1px solid #cccccc; border-radius: 6px;"
            "  padding: 1px 4px;"
            "}"
        )
        self.search_box.textChanged.connect(self._on_search_changed)
        self.status.addPermanentWidget(self.search_box)

        self.search_next_btn = QPushButton("Go To Next ▼")
        self.search_next_btn.setStyleSheet(
            "QPushButton {"
            "  border: 1px solid #cccccc; border-radius: 6px;"
            "  padding: 2px 8px;"
            "  background: transparent;"
            "}"
            "QPushButton:hover { background: #ececec; }"
        )
        self.search_next_btn.setFocusPolicy(Qt.NoFocus)
        self.search_next_btn.clicked.connect(self._search_go_next)
        self.search_next_btn.setVisible(False)
        self.status.addPermanentWidget(self.search_next_btn)

        self.search_no_results_label = QLabel("  No Results")
        self.search_no_results_label.setStyleSheet("color: red; font-style: italic;")
        self.search_no_results_label.setVisible(False)
        self.status.addPermanentWidget(self.search_no_results_label)

        # Equal vertical spacing between the pane's border above the status bar
        # and the main window's bottom edge below it. This MUST be set after
        # every addPermanentWidget() call above, not before: Qt's QStatusBar
        # resets its internal QHBoxLayout's margins to (0,0,0,0) on each
        # addPermanentWidget() call, so anything set earlier is silently wiped
        # out. CSS padding and setContentsMargins() on the bar itself are both
        # ignored outright -- this is the only thing that actually has an effect.
        #
        # top=0 because the splitter's own bottom content margin (see
        # self.splitter.setContentsMargins above, ~8-9px of "breathing room"
        # below the rounded pane) already supplies the entire visual gap above
        # the status bar's contents; adding more here would double it. bottom
        # is set to match that same total gap so the space below the status
        # bar's contents (down to the window's outer edge) looks equal.
        #
        # left/right = 8 to match the splitter's 8px side margins, so the search
        # box's right edge lines up with the preview pane's right edge above it
        # (a 4px margin left the box sticking out past the rounded pane).
        self.status.layout().setContentsMargins(8, 0, 8, 9)


    # ---------------------------------------------------------------- dirty state
    def _set_dirty(self, dirty: bool):
        """Slot for the editor document's modificationChanged signal (also
        called directly after loads/saves). Syncs the Save button highlight and
        the window's modified mark to the document's own dirty state, which
        already accounts for undo-to-saved."""
        self._dirty = dirty
        self._update_save_button_state()
        self.setWindowModified(dirty)

    def _mark_clean(self):
        """Declare the current editor content as the saved baseline: clears the
        document's modified flag (so a later undo-to-here reads as clean) and
        resets the UI. Used after opening a file, loading the sample, or saving."""
        self.editor.document().setModified(False)
        self._set_dirty(False)

    def _update_save_button_state(self):
        """Style the toolbar Save button to signal unsaved changes: red text +
        red border when dirty, normal otherwise. The button is created from a
        QAction via the toolbar, so we style its backing QToolButton widget
        (looked up with widgetForAction). No-op until the toolbar is built."""
        toolbar = getattr(self, "_main_toolbar", None)
        act = getattr(self, "_save_act", None)
        if toolbar is None or act is None:
            return
        btn = toolbar.widgetForAction(act)
        if btn is None:
            return
        if self._dirty:
            # Mirror the shared button's frame so only the colours change; the
            # outer height stays pinned by setFixedHeight in
            # _equalize_toolbar_heights.
            btn.setStyleSheet(
                "QToolButton {"
                "  color: #d00000;"
                "  border: 1px solid #d00000;"
                "  border-radius: 6px;"
                "  padding: 2px 8px;"
                "  margin: 1px 2px;"
                "}"
            )
            act.setToolTip("Save — unsaved changes (Ctrl+S)")
        else:
            btn.setStyleSheet("")   # revert to the shared toolbar-button style
            act.setToolTip("Save (Ctrl+S)")

    def _set_title(self, text: str):
        """Set the window title, always including the '[*]' placeholder Qt needs
        to render the unsaved-changes marker (setWindowModified is a no-op and
        warns without it). Qt replaces '[*]' with '*' when modified, or removes
        it when clean, so it never shows literally."""
        self.setWindowTitle(f"{text} [*]")

    def _apply_editor_font(self):
        font = QFont(self._editor_font_family, self._editor_font_size)
        # The editor pane must always render as a monospace font, regardless of
        # which family name is selected. setFixedPitch + the Monospace style
        # hint tell Qt to substitute a mono font if the requested family isn't
        # actually fixed-width (or isn't installed on this system).
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.editor.setFont(font)

    def _build_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("File")
        # Kept on self so _build_toolbar can reuse these same QAction objects
        # instead of creating new ones bound to the same shortcut — two
        # separate QActions both claiming QKeySequence.Open/Save makes Qt's
        # ambiguous-shortcut resolution non-deterministic.
        self._open_act = QAction("Open…", self)
        self._open_act.setShortcut(QKeySequence.Open)
        self._open_act.triggered.connect(self.action_open)
        file_menu.addAction(self._open_act)

        self._save_act = QAction("Save", self)
        self._save_act.setShortcut(QKeySequence.Save)
        self._save_act.triggered.connect(self.action_save)
        file_menu.addAction(self._save_act)

        self.export_menu = file_menu.addMenu("Export")
        self._rebuild_export_menu()

        view_menu = menubar.addMenu("View")

        zoom_in_act = QAction("Zoom In", self)
        zoom_in_act.setShortcut(QKeySequence.ZoomIn)
        zoom_in_act.triggered.connect(self.action_zoom_in)
        view_menu.addAction(zoom_in_act)

        zoom_out_act = QAction("Zoom Out", self)
        zoom_out_act.setShortcut(QKeySequence.ZoomOut)
        zoom_out_act.triggered.connect(self.action_zoom_out)
        view_menu.addAction(zoom_out_act)

        zoom_reset_act = QAction("Reset Zoom", self)
        zoom_reset_act.setShortcut(QKeySequence("Ctrl+0"))
        zoom_reset_act.triggered.connect(self.action_zoom_reset)
        view_menu.addAction(zoom_reset_act)

        view_menu.addSeparator()

        toggle_theme_act = QAction("Toggle Theme", self)
        toggle_theme_act.setShortcut(QKeySequence("Ctrl+T"))
        toggle_theme_act.triggered.connect(self.action_toggle_theme)
        view_menu.addAction(toggle_theme_act)

        theme_menu = view_menu.addMenu("Theme")
        self._theme_actions: dict[str, QAction] = {}
        for tname in THEMES:
            act = QAction(tname, self)
            act.setCheckable(True)
            act.setChecked(tname == self.theme_name)
            act.triggered.connect(lambda checked, n=tname: self.apply_theme(n))
            theme_menu.addAction(act)
            self._theme_actions[tname] = act

    def _refresh_export_options(self):
        """Sync both the Export menu and toolbar combo to the source mode."""
        self._rebuild_export_menu()
        if hasattr(self, "export_combo"):
            self.export_combo.blockSignals(True)
            self.export_combo.clear()
            self.export_combo.addItem("Select Format")     # placeholder (index 0)
            self.export_combo.addItems(self._export_formats())
            self.export_combo.setCurrentIndex(0)
            self.export_combo.blockSignals(False)

    def _on_export_combo_activated(self, index: int):
        """Run the chosen export, ignoring the 'Select Format' placeholder, then
        snap the dropdown back to the placeholder."""
        if index <= 0:
            return
        fmt = self.export_combo.itemText(index)
        self.export_combo.setCurrentIndex(0)   # reset label to placeholder
        self.action_export(fmt)

    def _export_formats(self) -> list[str]:
        """Export targets depend on the source: a Markdown file can be exported
        to HTML; an HTML file can be exported to Markdown (MD). The rest are
        common to both."""
        if getattr(self, "_source_mode", "markdown") == "html":
            return ["MD", "JSON", "PDF"]
        return ["HTML", "JSON", "PDF"]

    def _rebuild_export_menu(self):
        """Repopulate the Export submenu to match the current source mode."""
        if not hasattr(self, "export_menu"):
            return
        self.export_menu.clear()
        for fmt in self._export_formats():
            act = QAction(f"Export as {fmt}…", self)
            act.triggered.connect(lambda checked, f=fmt: self.action_export(f))
            self.export_menu.addAction(act)

    def _build_toolbar(self):
        self._build_menu()
        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        # Kept so _update_save_button_state can reach the Save button's widget.
        self._main_toolbar = tb
        # Shared look for every toolbar control: rounded corners with a
        # light-grey 1px border, a subtle hover, and a blue/white fill when a
        # checkable button is selected (e.g. the Editor View toggle). The input
        # widgets (spinbox, combos, search box) get the SAME border/radius and a
        # matching min/max-height so they line up with the buttons -- otherwise
        # the buttons' padding makes them taller than the unstyled inputs. The
        # Save button's dirty-state red styling is applied per-widget on top of
        # this and wins while set, reverting to this shared style when cleared.
        style = (
            "QToolButton {"
            "  border: 1px solid #cccccc;"
            "  border-radius: 6px;"
            "  padding: 2px 8px;"
            "  margin: 1px 2px;"
            "  background: transparent;"
            "}"
            "QToolButton:hover { background: #ececec; }"
            "QToolButton:pressed { background: #e0e0e0; }"
            "QToolButton:checked {"
            "  background: #1565c0;"
            "  color: #ffffff;"
            "  border-color: #1565c0;"
            "}"
            # Input controls: same frame as the buttons so the whole toolbar row
            # reads as one family. Their *outer* height is pinned uniformly via
            # setFixedHeight below (CSS min/max-height is content-box height and
            # each widget type adds its own frame, so it can't equalize them).
            "QComboBox, QSpinBox, QLineEdit {"
            "  border: 1px solid #cccccc;"
            "  border-radius: 6px;"
            "  padding: 1px 4px;"
            "  margin: 1px 2px;"
            "  background: #ffffff;"
            "}"
            "QComboBox::drop-down {"
            "  subcontrol-origin: padding;"
            "  subcontrol-position: center right;"
            "  width: 18px;"
            "  border: none;"
            "}"
            # NOTE: the dropdown popup's hover/selection highlight is made
            # transparent per-combo below (see the QPalette tweak after the
            # combos are created) -- a stylesheet ::item rule here can't do it
            # because native styles paint the highlight via the palette, not the
            # item background.
        )
        # Combo drop-arrow: a dark-grey downward triangle from a generated PNG
        # (referenced by file path, not an inline data-URI -- see
        # _down_triangle_icon_path for why). Only added when the icon exists, so
        # a failure can't break the rest of the stylesheet.
        arrow = _down_triangle_icon_path()
        if arrow:
            style += (
                f"QComboBox::down-arrow {{ image: url({arrow});"
                "  width: 10px; height: 10px; }"
            )
        tb.setStyleSheet(style)

        # Reuse the menu's QActions instead of creating new ones bound to the
        # same shortcut — a QAction can be added to both a menu and a toolbar.
        self._open_act.setText("📂 Open")
        self._open_act.setToolTip("Open… (Ctrl+O)")
        tb.addAction(self._open_act)

        self._save_act.setText("💾 Save")
        self._save_act.setToolTip("Save (Ctrl+S)")
        tb.addAction(self._save_act)

        export_label = QLabel("  📤 Export: ")
        tb.addWidget(export_label)

        self.export_combo = QComboBox()
        self.export_combo.addItem("Select Format")          # placeholder (index 0)
        self.export_combo.addItems(self._export_formats())
        self.export_combo.setFixedWidth(120)
        self.export_combo.setFocusPolicy(Qt.NoFocus)
        self.export_combo.activated.connect(self._on_export_combo_activated)
        tb.addWidget(self.export_combo)

        tb.addSeparator()

        theme_label = QLabel("  🎨 Theme: ")
        tb.addWidget(theme_label)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(list(THEMES.keys()))
        self.theme_combo.setCurrentText(self.theme_name)
        self.theme_combo.setFixedWidth(130)
        self.theme_combo.setFocusPolicy(Qt.NoFocus)
        self.theme_combo.currentTextChanged.connect(self.apply_theme)
        tb.addWidget(self.theme_combo)

        tb.addSeparator()

        # NOTE: the text-search controls (Search box, "Go To Next", "No Results")
        # live in the bottom status bar now — see _build_ui, after the Text Wrap
        # checkbox — not in this toolbar.

        # Editor View toggle — sits directly left of the font dropdown it controls
        self.editor_view_act = QAction("📝 Editor View", self)
        self.editor_view_act.setShortcut("Ctrl+E")
        self.editor_view_act.setCheckable(True)
        self.editor_view_act.setChecked(False)
        self.editor_view_act.triggered.connect(self.action_toggle_editor_view)
        tb.addAction(self.editor_view_act)

        # Font family selector (editor pane) — plain QComboBox matching theme_combo style
        font_label = QLabel(" 🇦 Font: ")
        tb.addWidget(font_label)

        # Editor font choices are restricted to monospace families only — the
        # editor pane is source code/markdown text, so a fixed-width font is
        # enforced regardless of what's picked here (see _apply_editor_font).
        _common_fonts = [
            "Cascadia Code", "JetBrains Mono", "Fira Code", "Consolas",
            "Courier New", "DejaVu Sans Mono", "Liberation Mono",
            "Noto Sans Mono", "Ubuntu Mono", "Source Code Pro",
        ]
        self.font_combo = QComboBox()
        self.font_combo.addItems(_common_fonts)
        current = self._editor_font_family
        if current in _common_fonts:
            self.font_combo.setCurrentText(current)
        else:
            self.font_combo.insertItem(0, current)
            self.font_combo.setCurrentIndex(0)
        self.font_combo.setFixedWidth(180)
        self.font_combo.setFocusPolicy(Qt.NoFocus)
        self.font_combo.currentTextChanged.connect(self._on_editor_font_changed)
        tb.addWidget(self.font_combo)

        size_label = QLabel("  Size: ")
        tb.addWidget(size_label)

        tb.addWidget(self._build_font_size_control())

        tb.addSeparator()

        # "Enable Text Wrap" toggle — moved here from the bottom status bar, so
        # it sits directly after the font-size control.
        self._wrap_checkbox = QCheckBox("Enable Text Wrap")
        self._wrap_checkbox.setChecked(True)
        self._wrap_checkbox.stateChanged.connect(self._on_wrap_changed)
        tb.addWidget(self._wrap_checkbox)

        # Make each combo's popup highlight transparent so the item text stays
        # visible in its normal color. A ::item background rule alone isn't
        # enough -- native styles (Fusion, when no GTK platform theme is loaded)
        # paint the selection via the palette's Highlight role, not the item
        # background. So neutralize that role on the popup view: Highlight ==
        # the view's own Base (transparent-looking), HighlightedText == the
        # normal Text color. This defeats the opaque highlight band entirely
        # while leaving the text untouched, independent of the active style.
        for combo in (self.export_combo, self.theme_combo, self.font_combo):
            view = combo.view()
            pal = view.palette()
            pal.setColor(QPalette.Highlight, pal.color(QPalette.Base))
            pal.setColor(QPalette.HighlightedText, pal.color(QPalette.Text))
            view.setPalette(pal)

        self._equalize_toolbar_heights(tb)

        # The editor starts hidden, so the editor-only font controls start
        # disabled; they enable when Editor View is toggled on.
        self._update_editor_font_controls_enabled()

    def _build_font_size_control(self) -> QWidget:
        """A single rounded control reading as one button: [ 🔻 | size | 🔺 ].

        It's a container QWidget with one rounded border; the 🔻 / size / 🔺 zones
        inside are borderless and vertically centred, so visually it's one pill,
        not three separate buttons. The spinbox owns the value and the 6–48
        range (free clamping); its native arrows are removed and the 🔻 / 🔺 zones
        drive it via stepBy()."""
        container = QWidget()
        container.setObjectName("fontSizeControl")
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        def _step_btn(text: str, tip: str, delta: int) -> QToolButton:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedWidth(20)   # compact square-ish zone
            b.clicked.connect(lambda: self.font_size_spin.stepBy(delta))
            return b

        # Plain geometric triangles (U+25BC/25B2), not the 🔻/🔺 emoji: the
        # emoji need a colour-emoji font that QToolButton text rendering can't
        # reliably reach on Windows, so they showed as "..." tofu. These solid
        # triangles exist in standard fonts and honour the #555 grey below.
        self.font_size_dec_btn = _step_btn("▼", "Decrease editor font size", -1)  # U+25BC down
        self.font_size_inc_btn = _step_btn("▲", "Increase editor font size", +1)  # U+25B2 up

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(6, 48)
        self.font_size_spin.setValue(self._editor_font_size)
        self.font_size_spin.setAlignment(Qt.AlignCenter)
        self.font_size_spin.setButtonSymbols(QAbstractSpinBox.NoButtons)  # no native arrows
        # 30px: the widest value "48" needs ~24px of glyphs plus cursor/frame
        # room, so two digits never clip while staying compact.
        self.font_size_spin.setFixedWidth(30)
        self.font_size_spin.setFocusPolicy(Qt.NoFocus)
        self.font_size_spin.valueChanged.connect(self._on_editor_size_changed)

        for w in (self.font_size_dec_btn, self.font_size_spin, self.font_size_inc_btn):
            row.addWidget(w, 0, Qt.AlignVCenter)

        # Keep the container itself from expanding beyond its contents so the
        # whole pill stays compact (~72px) instead of stretching in the toolbar.
        container.setFixedWidth(20 + 30 + 20 + 2)  # buttons + spin + border

        # One rounded border on the container; children are flat/transparent so
        # the whole thing looks like a single pill. Styled by objectName so this
        # doesn't leak onto the toolbar's other QToolButton/QSpinBox widgets.
        # Tight padding on the ➕/➖ zones keeps the control compact.
        # The toolbar's global "QSpinBox/QToolButton" rules (border, margin,
        # padding) also cascade onto these children, drawing extra frames/gaps
        # that broke the single-pill look. Explicitly reset margin AND border to
        # zero here so nothing leaks through -- the pill border is the
        # container's alone.
        container.setStyleSheet(
            "#fontSizeControl {"
            "  border: 1px solid #cccccc;"
            "  border-radius: 6px;"
            "  background: #ffffff;"
            "}"
            "#fontSizeControl QToolButton {"
            "  color: #555555; font-weight: bold; font-size: 13px;"
            "  border: none; margin: 0; background: transparent;"
            "  border-radius: 2px; padding: 2px;"   # rounded, equal padding all sides
            "}"
            "#fontSizeControl QToolButton:hover { background: #ececec; }"
            "#fontSizeControl QSpinBox {"
            "  border: none; margin: 0; padding: 0; background: transparent;"
            "}"
        )
        return container

    def _equalize_toolbar_heights(self, tb):
        """Pin every interactive toolbar control to one outer height so the row
        lines up. Done with setFixedHeight on the widgets rather than CSS
        min/max-height, because that CSS sizes the content box and each widget
        type (button vs combo vs spinbox) adds a different frame on top, so the
        outer heights wouldn't match. QLabel separators are left alone, and the
        font-size composite is sized as a whole (its inner − / size / + zones
        must NOT be pinned individually, or they'd break the single-pill look)."""
        from PySide6.QtWidgets import QToolButton, QComboBox, QSpinBox, QLineEdit, QPushButton
        H = 28
        size_ctrl = self.font_size_spin.parentWidget()  # the composite container
        size_ctrl.setFixedHeight(H)
        skip = set(size_ctrl.findChildren(QWidget))     # its inner zones
        for cls in (QToolButton, QComboBox, QSpinBox, QLineEdit, QPushButton):
            for widget in tb.findChildren(cls):
                if widget in skip:
                    continue
                widget.setFixedHeight(H)

    # ---------------------------------------------------------------- editor view toggle
    def action_toggle_editor_view(self, checked: bool):
        self._editor_visible = checked
        if checked:
            self.editor.show()
            self.splitter.setSizes([480, 700])
        else:
            self.editor.hide()
            self.splitter.setSizes([0, 1])
        self._update_editor_font_controls_enabled()

    def _update_editor_font_controls_enabled(self):
        """The font family + size controls only affect the editor pane, so they
        are disabled (greyed out) whenever the editor view is hidden."""
        enabled = getattr(self, "_editor_visible", False)
        if hasattr(self, "font_combo"):
            self.font_combo.setEnabled(enabled)
        # The font-size composite pill: disable the whole container so its
        # ▼/size/▲ zones all grey out together.
        if hasattr(self, "font_size_spin"):
            self.font_size_spin.parentWidget().setEnabled(enabled)

    # ---------------------------------------------------------------- font controls
    def _on_editor_font_changed(self, font_name: str):
        self._editor_font_family = font_name
        self._apply_editor_font()

    def _on_editor_size_changed(self, size: int):
        self._editor_font_size = size
        self._apply_editor_font()

    # ---------------------------------------------------------------- preview zoom
    def _apply_zoom(self):
        """Apply current zoom to both the preview (via CSS font-size) and the editor font."""
        self._render()
        # Also scale editor font proportionally from its base size
        scaled_editor_size = max(6, round(self._editor_font_size * self._zoom_percent / 100))
        font = QFont(self._editor_font_family, scaled_editor_size)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
        self.editor.setFont(font)
        # Sync the spinbox display without triggering a cascading size change
        self.font_size_spin.blockSignals(True)
        self.font_size_spin.setValue(self._editor_font_size)
        self.font_size_spin.blockSignals(False)
        self.status.showMessage(f"Zoom: {self._zoom_percent}%", 1500)

    def _on_zoom_requested(self, direction: int):
        if direction > 0:
            self.action_zoom_in()
        elif direction < 0:
            self.action_zoom_out()

    def action_zoom_in(self):
        self._zoom_percent = min(200, self._zoom_percent + 10)
        self._apply_zoom()

    def action_zoom_out(self):
        self._zoom_percent = max(50, self._zoom_percent - 10)
        self._apply_zoom()

    def action_zoom_reset(self):
        self._zoom_percent = 100
        self._apply_zoom()

    def _base_font_px(self) -> int:
        return round(15 * self._zoom_percent / 100)

    @staticmethod
    def _code_label_px(base_px: int) -> tuple[int, int]:
        """(lang_px, code_px) for a given base font size — the one formula
        every render/export call site should share, so exported documents use
        the same font sizing as the live preview instead of drifting out of
        sync with hand-copied literals."""
        return max(8, base_px - 4), base_px - 2

    # ---------------------------------------------------------------- text search
    def _on_search_changed(self, text: str):
        """Highlight all occurrences of *text* in the preview pane."""
        from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor

        # Reset state
        self._search_matches = []
        self._search_index = 0
        self.preview.setExtraSelections([])
        self.search_next_btn.setVisible(False)
        self.search_no_results_label.setVisible(False)

        if not text:
            self.status.showMessage("", 500)
            return

        highlight_fmt = QTextCharFormat()
        highlight_fmt.setBackground(QColor("#ffeb3b"))
        highlight_fmt.setForeground(QColor("#000000"))

        document = self.preview.document()
        cursor = QTextCursor(document)

        while True:
            cursor = document.find(text, cursor)
            if cursor.isNull():
                break
            sel = QTextBrowser.ExtraSelection()
            sel.cursor = cursor
            sel.format = highlight_fmt
            self._search_matches.append(sel)

        self.preview.setExtraSelections(self._search_matches)
        count = len(self._search_matches)

        if count:
            # Scroll to first match
            self.preview.setTextCursor(self._search_matches[0].cursor)
            self.search_next_btn.setText(f"Go To Next ▼  (1/{count})")
            self.search_next_btn.setVisible(True)
            self.search_no_results_label.setVisible(False)
            self.status.showMessage(f"Found {count} match{'es' if count != 1 else ''}", 2000)
        else:
            self.search_next_btn.setVisible(False)
            self.search_no_results_label.setVisible(True)
            self.status.showMessage("No matches found", 2000)

    def _search_go_next(self):
        """Advance to the next search match and scroll to it."""
        if not self._search_matches:
            return
        count = len(self._search_matches)
        self._search_index = (self._search_index + 1) % count
        self.preview.setTextCursor(self._search_matches[self._search_index].cursor)
        self.search_next_btn.setText(f"Go To Next ▼  ({self._search_index + 1}/{count})")

    def wheelEvent(self, event):
        """Ctrl + scroll wheel zooms all text in and out."""
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.action_zoom_in()
            elif delta < 0:
                self.action_zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    # ---------------------------------------------------------------- rendering
    # Documents at or above this size run the markdown/Pygments parse and the
    # regex-heavy post_process_html pass on a worker thread instead of the GUI
    # thread, so typing a pause into a large file doesn't freeze the UI for the
    # duration of a full re-render. Small documents render synchronously since
    # thread hand-off overhead isn't worth it below this size.
    _THREADED_RENDER_THRESHOLD_CHARS = 50_000

    def _schedule_render(self):
        self._debounce.start()

    @staticmethod
    def _compute_body_html(text: str, mode: str, source_lang: str | None,
                            theme_name: str, wrap_code: bool,
                            lang_px: int, code_px: int) -> str:
        """Pure computation half of rendering: markdown/Pygments parsing plus
        the HTML post-processing pass. Holds no Qt widget references so it can
        run safely on a worker thread; the caller does the `setHtml(...)`."""
        theme = THEMES[theme_name]
        if mode == "source":
            lang = source_lang or "text"
            # Use Pygments directly — never touch the Markdown pipeline so
            # comment markers, headings, dashes etc. are never misinterpreted.
            raw_snippet = highlight_source(text, lang, PYGMENTS_STYLE[theme_name])
            return post_process_html(
                raw_snippet,
                stripe_color=theme['table_stripe'],
                code_bg=theme['code_bg'],
                code_fg=theme['code_fg'],
                lang_px=lang_px,
                code_px=code_px,
                border_color=theme['code_border'],
                page_bg=theme['bg'],
                label_fg=theme['fg'],
                wrap_code=wrap_code,
            )
        processed = post_process_html(
            render_markdown(text, pygments_style=PYGMENTS_STYLE[theme_name]),
            stripe_color=theme['table_stripe'],
            code_bg=theme['code_bg'],
            code_fg=theme['code_fg'],
            lang_px=lang_px,
            code_px=code_px,
            border_color=theme['code_border'],
            page_bg=theme['bg'],
            label_fg=theme['fg'],
            wrap_code=wrap_code,
        )
        # Stamp inline styles on <mark>/<ins>/<del> and inline <code> — the
        # preview stylesheet's rules for them are unreliable in QTextBrowser
        # (see style_inline_marks).
        return style_inline_marks(processed, theme, code_px)

    def _render(self):
        text = self.editor.toPlainText()
        theme = THEMES[self.theme_name]
        css = build_css(theme, self._base_font_px()) + pygments_css(PYGMENTS_STYLE[self.theme_name], code_bg=theme['code_bg'], code_fg=theme['code_fg'])
        mode = getattr(self, "_source_mode", "markdown")

        # --- HTML file: rendered verbatim (author CSS intact) via QTextBrowser --
        if mode == "html":
            full_html = self._compose_html_document(text, css)
            self._finish_render(text, full_html)
            return

        wrap_code = self._wrap_checkbox.isChecked()
        _bfp = self._base_font_px()
        _lang_px, _code_px = self._code_label_px(_bfp)
        self.preview._code_px = _bfp - 2

        # Bump the generation on every call so a slow in-flight worker result
        # that arrives after a newer edit was already rendered gets discarded
        # instead of clobbering the preview with stale content.
        self._render_generation = getattr(self, "_render_generation", 0) + 1
        generation = self._render_generation
        source_lang = getattr(self, "_source_lang", None)

        if len(text) < self._THREADED_RENDER_THRESHOLD_CHARS:
            body_html = self._compute_body_html(
                text, mode, source_lang, self.theme_name, wrap_code, _lang_px, _code_px
            )
            full_html = f"<html><head>{css}</head><body>{body_html}</body></html>"
            self._finish_render(text, full_html)
            return

        def _work():
            return self._compute_body_html(
                text, mode, source_lang, self.theme_name, wrap_code, _lang_px, _code_px
            )

        def _on_done(body_html: str):
            if generation != self._render_generation:
                return  # superseded by a newer render request
            full_html = f"<html><head>{css}</head><body>{body_html}</body></html>"
            self._finish_render(text, full_html)

        self._run_in_background(_work, _on_done)

    def _run_in_background(self, work, on_done):
        """Run *work* (a zero-arg callable) on a worker thread and deliver its
        result to *on_done* back on the GUI thread. Keeps a Python-level
        reference to both the thread AND the worker on self — without it,
        nothing holds a strong reference to the `_RenderWorker` instance once
        this method returns (moveToThread only affects Qt-side thread
        affinity, not Python refcounting), so it can be garbage collected
        before `run()` fires and the signal is silently dropped."""
        thread = QThread(self)
        worker = _RenderWorker(work)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # IMPORTANT: connect directly to self._on_background_result (a bound
        # method of self, a QObject living on the GUI thread) — NOT to a
        # lambda/partial wrapping it. Qt's auto-connection only detects
        # "sender/receiver are on different threads, so queue this" from the
        # *receiver callable's* own identity; a lambda or partial has no
        # thread affinity of its own (even though it calls a bound method
        # internally), so PySide6 silently delivers it in-place on the
        # emitting (worker) thread instead of queuing to the GUI thread —
        # verified experimentally, since Qt.ConnectionType.QueuedConnection
        # passed explicitly does not fix it either for a wrapped callable.
        # Running _finish_render's Qt widget calls on a worker thread crashes
        # Qt with "Cannot create children for a parent in a different thread".
        self._pending_callbacks = getattr(self, "_pending_callbacks", {})
        self._pending_callbacks[worker] = (on_done, thread)
        # worker.finished -> _on_background_result is a queued (cross-thread)
        # call. quit()/deleteLater() must NOT be connected directly to
        # worker.finished too: those connections would run immediately
        # in-thread during emit() (thread/worker live on the worker thread),
        # deleting the worker before our queued handler is even dispatched on
        # the GUI thread's event loop, so `self.sender()` would see a
        # dangling/deleted QObject. Instead, _on_background_result triggers
        # quit()/deleteLater() itself once it's actually run.
        worker.finished.connect(self._on_background_result)
        thread.finished.connect(thread.deleteLater)
        # Keep strong references until the thread actually finishes so neither
        # is torn down (by Qt or by Python's GC) while still running.
        self._active_render_threads = getattr(self, "_active_render_threads", [])
        self._active_render_threads.append((thread, worker))
        thread.finished.connect(
            lambda: self._active_render_threads.remove((thread, worker))
        )
        thread.start()

    def _on_background_result(self, body_html):
        """Bound method of self (MarkdownViewer, a QObject on the GUI thread)
        so Qt's auto-connection correctly queues delivery here instead of
        running it directly on the worker thread — see _run_in_background."""
        worker = self.sender()
        entry = self._pending_callbacks.pop(worker, None)
        if entry is None:
            return
        on_done, thread = entry
        try:
            on_done(body_html)
        finally:
            thread.quit()
            worker.deleteLater()

    def _finish_render(self, text: str, full_html: str):
        # Preserve relative image/link resolution against the open file's folder
        if self.current_path:
            self.preview.setSearchPaths([str(self.current_path.parent)])

        # Belt-and-suspenders: set the document's default font size so any
        # element not covered by an explicit CSS rule still scales with zoom.
        # Build a fresh QFont from the family only and set an explicit pixel
        # size, rather than mutating the widget's current font in place. A
        # font carried over from elsewhere can already have pointSize()==-1
        # (it was pixel-sized), and re-applying it makes Qt emit
        # "QFont::setPointSize: Point size <= 0 (-1)" during layout.
        base_px = max(6, self._base_font_px())
        doc_font = QFont(self.preview.font().family())
        doc_font.setPixelSize(base_px)
        self.preview.document().setDefaultFont(doc_font)

        scroll = self.preview.verticalScrollBar().value()
        self.preview.setHtml(full_html)

        # Justify prose. QTextBrowser IGNORES the CSS `text-align: justify` rule
        # in the stylesheet (its rich-text CSS supports only left/right/center),
        # so justification has to be applied through the block-format API after
        # the document is loaded -- see _justify_prose_blocks.
        self._justify_prose_blocks()

        # HTML-source fallback: QTextBrowser is Qt's rich-text engine, not a full
        # browser, so a complex modern page (heavy flex/grid CSS, script-built
        # DOM -- e.g. a saved Google results page) lays out to NOTHING and the
        # pane is silently blank. When that happens for an HTML file, show a
        # warning card with a button to open it in the real default browser.
        if (getattr(self, "_source_mode", "markdown") == "html"
                and not self.preview.toPlainText().strip()
                and text.strip()):
            self._show_html_too_complex_notice()

        self.preview.verticalScrollBar().setValue(scroll)
        words = len(self.preview.toPlainText().split())
        self.word_count_label.setText(f"{words} words")

    def _justify_prose_blocks(self):
        """Set justified alignment on running-prose blocks via the block-format
        API, because QTextBrowser doesn't honor CSS `text-align: justify`.

        Only paragraphs and list items are justified. Headings, code lines and
        table cells are left as-is:
          * code lines and inline-code use a monospace font -> skip blocks whose
            font is fixed-pitch (justifying code would stretch its spacing);
          * headings render larger than the base body size -> skip bigger blocks;
          * table cells live inside a QTextTable -> skip blocks whose frame is a
            table (checked via the block's position in the document layout).
        """
        from PySide6.QtGui import QTextBlockFormat
        doc = self.preview.document()
        base_px = max(6, self._base_font_px())
        cursor = QTextCursor(doc)
        block = doc.begin()
        while block.isValid():
            if self._block_is_prose(block, base_px):
                cursor.setPosition(block.position())
                bf = block.blockFormat()
                if bf.alignment() != Qt.AlignJustify:
                    bf.setAlignment(Qt.AlignJustify)
                    cursor.setBlockFormat(bf)
            block = block.next()

    @staticmethod
    def _block_is_prose(block, base_px: int) -> bool:
        """True if a text block is running prose (paragraph / list item) that
        should be justified, False for headings, code, or table cells."""
        text = block.text().strip()
        if not text:
            return False
        # Skip blocks inside tables (data tables AND the code-line tables).
        if block.textList() is None and _block_in_table(block):
            # (list items also have no textList sometimes; the table check below
            #  is the authoritative skip for cells)
            pass
        if _block_in_table(block):
            return False
        # Inspect the dominant char format of the block.
        it = block.begin()
        if it.atEnd():
            return True
        fmt = it.fragment().charFormat()
        font = fmt.font()
        # Skip code (monospace) blocks.
        if font.fixedPitch() or font.styleHint() == QFont.StyleHint.Monospace:
            return False
        # Skip headings: they render larger than the base body size.
        fpx = font.pixelSize()
        if fpx > 0 and fpx > base_px + 1:
            return False
        return True

    def _show_html_too_complex_notice(self):
        """Replace a blank HTML preview with a warning + 'Open in browser' link.

        The link targets the file's own path with a file:// URL; clicking it
        routes through _on_anchor_clicked, which hands non-markdown files to
        QDesktopServices (the OS default browser). If there's no path on disk
        yet, the link is omitted."""
        theme = THEMES[self.theme_name]
        open_link = ""
        if self.current_path:
            href = QUrl.fromLocalFile(str(self.current_path)).toString()
            open_link = (
                f"<p style='margin-top:22px;'>"
                f"<a href='{href}' style='display:inline-block;"
                f"background:{theme['link']};color:#ffffff;text-decoration:none;"
                f"padding:9px 18px;border-radius:8px;font-weight:600;'>"
                f"Open in default web browser</a></p>"
            )
        self.preview.setHtml(
            "<html><body>"
            f"<div style='text-align:center;color:{theme['fg']};"
            "font-family:sans-serif;margin-top:12%;padding:0 24px;'>"
            "<div style='font-size:40px;line-height:1;'>🌐</div>"
            "<h2 style='margin:16px 0 8px;font-weight:600;'>"
            "HTML file too complex to render here</h2>"
            f"<p style='color:{theme['fg']};opacity:0.8;max-width:460px;"
            "margin:0 auto;line-height:1.5;'>"
            "This page uses modern web layout and scripts that the built-in "
            "viewer can't display. Open it in your default web browser to see "
            "it properly."
            "</p>"
            f"{open_link}"
            "</div></body></html>"
        )

    def _html_to_markdown(self, html_text: str) -> str:
        """Convert an HTML document to Markdown for MD export.

        Prefers the `markdownify` library (best fidelity for tables, lists,
        links, emphasis). Falls back to a minimal stdlib tag-stripper if it
        isn't installed, so the feature still works (just plainer)."""
        try:
            from markdownify import markdownify as _md
            # ATX-style headings; keep it close to GitHub-flavoured markdown.
            return _md(html_text, heading_style="ATX").strip() + "\n"
        except ImportError:
            # Lightweight fallback: strip tags, keep heading/list/para breaks.
            from html.parser import HTMLParser
            from html import unescape

            class _Strip(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.out: list[str] = []
                    self.skip = False
                def handle_starttag(self, tag, attrs):
                    if tag in ("style", "script"):
                        self.skip = True
                    elif tag in ("p", "div", "br", "tr", "li"):
                        self.out.append("\n")
                    elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                        self.out.append("\n\n" + "#" * int(tag[1]) + " ")
                    elif tag in ("td", "th"):
                        self.out.append(" | ")
                def handle_endtag(self, tag):
                    if tag in ("style", "script"):
                        self.skip = False
                    elif tag in ("p", "div", "tr"):
                        self.out.append("\n")
                def handle_data(self, data):
                    if not self.skip and data.strip():
                        self.out.append(data)

            p = _Strip()
            p.feed(html_text)
            text = unescape("".join(p.out))
            # collapse excess blank lines
            return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

    def _compose_html_document(self, html_text: str, css: str) -> str:
        """Prepare a user-supplied .html file for the preview.

        QTextBrowser renders an HTML subset (Qt rich text), not a full browser
        engine, so scripts and advanced CSS (flexbox/grid/var()) won't apply —
        but headings, paragraphs, lists, tables, images, links and basic inline
        styling display fine.

        Styling policy:
          * If the file carries its OWN <style>, that styling is the author's
            intent, so we respect it: we inject only a minimal base (zoom font
            size) BEFORE the document's styles, so the file's rules win.
          * If the file has NO styles, it would render bare, so we apply our
            full theme CSS to make it readable and on-brand.
        In both cases we add the legacy table border attributes (Qt ignores CSS
        `border-collapse`, so without them tables look half-gridded).
        """
        _bfp = self._base_font_px()
        _lang_px, _code_px = self._code_label_px(_bfp)
        styled = post_process_html(
            html_text,
            stripe_color=THEMES[self.theme_name]['table_stripe'],
            code_bg=THEMES[self.theme_name]['code_bg'],
            code_fg=THEMES[self.theme_name]['code_fg'],
            lang_px=_lang_px,
            code_px=_code_px,
            border_color=THEMES[self.theme_name]['code_border'],
            page_bg=THEMES[self.theme_name]['bg'],
            label_fg=THEMES[self.theme_name]['fg'],
        )  # adds table borders + wrappers
        lower = styled.lower()
        has_own_style = ("<style" in lower) or ('style="' in lower and "<body" in lower)

        if has_own_style:
            # Minimal base only: drive zoom via the document default font (set in
            # _render). Don't impose our colours/sizes over the author's design.
            base_px = self._base_font_px()
            base_css = (
                f"<style>body {{ font-size: {base_px}px; }}</style>"
            )
            inject = base_css
        else:
            inject = css  # no author styling — apply our full theme

        if "<head" in lower:
            idx = lower.find("<head")
            # insert right AFTER <head ...> so author styles that follow win
            m = re.search(r"(?i)<head[^>]*>", styled)
            if m:
                pos = m.end()
                return styled[:pos] + inject + styled[pos:]
            # fall through if malformed
        if "<html" in lower:
            m = re.search(r"(?i)<html[^>]*>", styled)
            if m:
                pos = m.end()
                return styled[:pos] + f"<head>{inject}</head>" + styled[pos:]

        # Bare fragment: wrap it. If it had inline styles we still give it a
        # readable base; otherwise the full theme.
        return f"<html><head>{inject}</head><body>{styled}</body></html>"

    # ---------------------------------------------------------------- hyperlinks
    def _on_anchor_clicked(self, url: QUrl):
        """Open web/mail links externally; jump to in-document #anchors."""
        if url.isRelative() and url.hasFragment() and not url.path():
            # Pure in-document anchor like  #section  -> scroll to it.
            self.preview.scrollToAnchor(url.fragment())
            return

        scheme = url.scheme().lower()
        if scheme in ("http", "https", "mailto", "ftp"):
            QDesktopServices.openUrl(url)
            return

        if scheme == "file" or url.isRelative():
            # A relative link to a sibling markdown/file: open .md in-app,
            # otherwise hand off to the OS.
            target = url
            if url.isRelative() and self.current_path:
                target = QUrl.fromLocalFile(
                    str((self.current_path.parent / url.path()).resolve())
                )
            local = Path(target.toLocalFile())
            if local.suffix.lower() in (".md", ".markdown", ".mdown", ".txt") and local.exists():
                self.open_file(local)
            else:
                QDesktopServices.openUrl(target)
            return

        # Unknown scheme — best-effort hand-off.
        QDesktopServices.openUrl(url)

    def _on_wrap_changed(self, state):
        wrap = Qt.CheckState(state) == Qt.CheckState.Checked
        self.preview.setLineWrapMode(
            QTextBrowser.LineWrapMode.WidgetWidth if wrap
            else QTextBrowser.LineWrapMode.NoWrap
        )
        self._render()

    def _on_link_hovered(self, url):
        """Show the link target in the status bar on hover (clears on leave)."""
        text = url.toString() if isinstance(url, QUrl) else str(url)
        if text:
            self.status.showMessage(text)
        else:
            self.status.clearMessage()

    # ---------------------------------------------------------------- theming
    def apply_theme(self, name: str):
        self.theme_name = name
        theme = THEMES[name]
        # Sync toolbar dropdown
        if hasattr(self, "theme_combo"):
            self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentText(name)
            self.theme_combo.blockSignals(False)
        # Sync menu checkmarks
        if hasattr(self, "_theme_actions"):
            for tname, act in self._theme_actions.items():
                act.setChecked(tname == name)
        radius = "10px"
        # Editor always gets a very light grey IDE-style background (overrides theme for light modes)
        # For dark themes we honour the theme bg so text stays readable.
        _dark_themes = {"Dark", "Tokyo Night", "Nord"}
        editor_is_dark = name in _dark_themes
        editor_bg = theme["editor_bg"] if editor_is_dark else "#f0f2f5"
        editor_fg = theme["editor_fg"]
        # Switch the editor's Markdown syntax-highlight palette to match: the
        # default (light) token colors are dark hues that vanish on a dark editor
        # background, so dark themes use the light-token variant.
        if hasattr(self, "_md_highlighter"):
            self._md_highlighter.set_dark(editor_is_dark)
        editor_style = f"""
            QPlainTextEdit {{
                background-color: {editor_bg};
                color: {editor_fg};
                border: 1px solid {theme['quote_border']};
                border-radius: {radius};
                padding: 10px;
                selection-background-color: {theme['link']};
                selection-color: {editor_bg};
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: #9ca3af;
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #6b7280;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """
        self.editor.setStyleSheet(editor_style)
        self.preview.setStyleSheet(f"""
            QTextBrowser {{
                border: 1px solid {theme['quote_border']};
                border-radius: {radius};
                background-color: {theme['bg']};
                color: {theme['fg']};
                alternate-background-color: {theme['bg']};
                padding: 4px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 0px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: #9ca3af;
                border-radius: 4px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #6b7280;
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)
        # Kill Qt's internal alternating-row palette so QTextBrowser never
        # applies a different shade to alternate paragraphs inside <pre> blocks
        # (which produces the "zebra stripe" effect on code blocks).
        palette = self.preview.palette()
        palette.setColor(palette.ColorRole.Base, QColor(theme['bg']))
        palette.setColor(palette.ColorRole.AlternateBase, QColor(theme['bg']))
        palette.setColor(palette.ColorRole.Text, QColor(theme['fg']))
        # QTextBrowser colors <a> links from the palette's Link role, which
        # overrides the CSS "a { color }" rule -- without this it paints every
        # link in Qt's default dark blue regardless of theme. Set it to the
        # theme's link color so links (incl. the light-blue dark-theme ones) show.
        palette.setColor(palette.ColorRole.Link, QColor(theme['link']))
        self.preview.setPalette(palette)
        self.preview.set_code_pane_style(theme['code_bg'], theme['code_border'], theme['bg'])
        self._render()

    def action_toggle_theme(self):
        """Cycle through all available themes in order."""
        theme_names = list(THEMES.keys())
        next_index = (theme_names.index(self.theme_name) + 1) % len(theme_names)
        self.apply_theme(theme_names[next_index])

    # ---------------------------------------------------------------- file ops
    def action_open(self):
        src_exts = " ".join(f"*{e}" for e in _SOURCE_LEXERS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open File", "",
            f"All Supported (*.md *.markdown *.mdown *.txt *.html *.htm {src_exts});;"
            "Markdown Files (*.md *.markdown *.mdown *.txt);;"
            "HTML Files (*.html *.htm);;"
            f"Source Files ({src_exts});;"
            "All Files (*)"
        )
        if path:
            self.open_file(Path(path))

    # Files larger than this trigger a confirmation prompt before loading, since
    # the full read + markdown/Pygments render + Qt document layout all run
    # synchronously on the GUI thread and can freeze the app for very large files.
    _LARGE_FILE_WARN_BYTES = 5 * 1024 * 1024

    def open_file(self, path: Path):
        # Loading a new file replaces the buffer, so guard unsaved edits first.
        if not self._confirm_discard_changes():
            return
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > self._LARGE_FILE_WARN_BYTES:
            reply = QMessageBox.question(
                self, "Large File",
                f"This file is {size / (1024 * 1024):.1f} MB and may take a while "
                "to render and could make the app unresponsive.\n\nOpen anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_bytes().decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = path.read_bytes().decode("utf-16")
                except UnicodeDecodeError:
                    text = path.read_bytes().decode("latin-1")
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not open file:\n{exc}")
            return
        self.current_path = path
        # Decide render mode based on extension.
        suffix = path.suffix.lower()
        if suffix in (".html", ".htm"):
            self._source_mode = "html"
            self._source_lang = None
        elif suffix in _SOURCE_LEXERS or path.name.lower() == "dockerfile":
            self._source_mode = "source"
            if path.name.lower() == "dockerfile":
                self._source_lang = "docker"
            else:
                self._source_lang = _SOURCE_LEXERS[suffix][0]
        else:
            self._source_mode = "markdown"
            self._source_lang = None
        self._refresh_export_options()
        self.editor.setPlainText(text)
        # A freshly loaded file is the saved baseline, not an unsaved edit.
        self._mark_clean()
        if self._source_mode == "html":
            kind = "HTML"
        elif self._source_mode == "source":
            entry = _SOURCE_LEXERS.get(suffix)
            label = entry[1] if entry else "Source"
            kind = f"Source · {label}"
        else:
            kind = "Markdown"
        self._set_title(f"Markdown Viewer — {path.name}  [{kind}]")
        self._render()

    def action_save(self) -> bool:
        """Save the editor buffer. Returns True on success, False if it failed
        or the user cancelled the Save-As dialog (the unsaved-changes prompt
        relies on this to decide whether to abort a close/open)."""
        if self.current_path is None:
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Markdown File", "untitled.md", "Markdown Files (*.md)"
            )
            if not path:
                return False
            self.current_path = Path(path)
        try:
            self.current_path.write_text(self.editor.toPlainText(), encoding="utf-8")
            self.status.showMessage(f"Saved {self.current_path}", 3000)
            self._set_title(f"Markdown Viewer — {self.current_path.name}")
            # Saved content is the new baseline; undo-to-here now reads as clean.
            self._mark_clean()
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not save file:\n{exc}")
            return False

    def action_export(self, fmt: str = "HTML"):
        fmt = fmt.upper()
        base_name = self.current_path.stem if self.current_path else "untitled"
        filters = {
            "HTML": ("Export Rendered HTML", f"{base_name}.html", "HTML Files (*.html)"),
            "MD":   ("Export as Markdown",   f"{base_name}.md",   "Markdown Files (*.md)"),
            "JSON": ("Export as JSON",        f"{base_name}.json", "JSON Files (*.json)"),
            "PDF":  ("Export as PDF",          f"{base_name}.pdf",  "PDF Files (*.pdf)"),
        }
        title, default_name, file_filter = filters.get(fmt, filters["HTML"])
        path, _ = QFileDialog.getSaveFileName(self, title, default_name, file_filter)
        if not path:
            return

        src_text = self.editor.toPlainText()
        is_html_src = getattr(self, "_source_mode", "markdown") == "html"

        # MD export (only meaningful for an HTML source): convert HTML -> Markdown
        if fmt == "MD":
            try:
                md_out = self._html_to_markdown(src_text)
                Path(path).write_text(md_out, encoding="utf-8")
                self.status.showMessage(f"Exported {path}", 3000)
            except Exception as exc:
                QMessageBox.critical(self, "Error", f"Could not export MD:\n{exc}")
            return

        # For all other formats we need a styled HTML document as the basis.
        # - Markdown source: run the markdown pipeline.
        # - HTML source: use the file's own HTML as-is (author styling intact).
        theme = THEMES[self.theme_name]
        # Use the currently-displayed zoom level so exported documents match
        # what's on screen, instead of a hardcoded base size that ignored zoom.
        _bfp = self._base_font_px()
        _lang_px, _code_px = self._code_label_px(_bfp)

        # PDF is a white-paper medium. Every on-screen theme uses a DARK code
        # pane (dark code_bg + a dark Pygments style with light tokens), which
        # looks right on screen but renders as unreadable dark-on-white once
        # the page background is forced white for print. Rather than fight the
        # inline bgcolor/style attributes post_process_html stamps onto the
        # code tables (inline styles beat stylesheet !important), we render the
        # code pane LIGHT from the start for PDF: a light code_bg, dark code_fg
        # and a light Pygments style. Other formats (HTML/JSON) keep the live
        # theme so exported HTML still matches the app. Screen rendering is
        # untouched -- this only affects the export document.
        pdf_light = fmt == "PDF"
        # PDF is always white paper, so a dark theme's light body text/links/
        # headings would be washed out or invisible. Force the light "Light"
        # theme's colors for the whole PDF document (not just the code pane), so
        # everything uses dark, readable ink regardless of the on-screen theme.
        # HTML/JSON export keeps the live theme so it matches the app.
        if pdf_light:
            theme = THEMES["Light"]
        code_bg   = "#f6f8fa" if pdf_light else theme['code_bg']
        code_fg   = "#24292f" if pdf_light else theme['code_fg']
        code_brd  = "#d0d7de" if pdf_light else theme['code_border']
        pyg_style = "default" if pdf_light else PYGMENTS_STYLE[self.theme_name]
        label_fg  = "#24292f" if pdf_light else theme['fg']
        # Language-label header background. On screen the label sits on the page
        # color, which reads as a lighter strip over the dark pane. For the light
        # PDF pane that would blend into the page, so give the header a shade
        # DARKER than the light code body (GitHub-style) with a divider line
        # underneath, so it stays a distinct header. Empty strings ->
        # post_process_html falls back to page_bg / no divider (screen behavior).
        label_bg   = "#e1e4e8" if pdf_light else ""
        label_brd  = "#d0d7de" if pdf_light else ""

        css = build_css(theme, _bfp) + pygments_css(pyg_style, code_bg=code_bg, code_fg=code_fg)
        if is_html_src:
            body_html = src_text  # already a complete/styled HTML document
        else:
            body_html = post_process_html(
                render_markdown(src_text, pygments_style=pyg_style),
                stripe_color=theme['table_stripe'],
                code_bg=code_bg,
                code_fg=code_fg,
                lang_px=_lang_px,
                code_px=_code_px,
                border_color=code_brd,
                page_bg=theme['bg'],
                label_fg=label_fg,
                label_bg=label_bg,
                label_border=label_brd,
            )
        # JSON metadata expects Markdown-shaped text; an HTML source needs to
        # be converted first or it would scan raw tags as plain paragraph
        # text (headings/tables silently lost).
        if is_html_src:
            try:
                md_text = self._html_to_markdown(src_text)
            except Exception:
                md_text = src_text
        else:
            md_text = src_text
        # High-quality print/export CSS. Table border/header shading and the
        # footnote-link color follow the active theme so exported HTML/PDF
        # matches what's on screen instead of always looking like Light theme.
        print_css = f"""
        <style>
            @page {{ margin: 2.5cm 2cm; }}
            body {{
                width: 100%;
                max-width: 100%;
                margin: 0;
                padding: 20px 32px;
                box-sizing: border-box;
                -webkit-print-color-adjust: exact;
                print-color-adjust: exact;
            }}
            table {{ width: 100%; border-collapse: collapse; page-break-inside: avoid; }}
            th, td {{ border: 1px solid {theme['table_border']}; padding: 7px 12px; text-align: left; vertical-align: top; }}
            th {{ background-color: {theme['table_stripe']} !important; font-weight: 600; }}
            pre {{ page-break-inside: avoid; white-space: pre-wrap; word-break: break-word; }}
            code {{ white-space: pre-wrap; word-break: break-word; }}
            .table-wrapper {{ overflow: visible; }}
            h1, h2, h3 {{ page-break-after: avoid; }}
            img {{ max-width: 100%; height: auto; }}
            @media print {{
                body {{ font-size: 11pt; }}
                a[href]:after {{ content: " (" attr(href) ")"; font-size: 0.8em; color: {theme['fg']}; }}
            }}
        </style>"""
        if is_html_src:
            # Author HTML is already a full document. For PDF we want the print
            # CSS applied, so inject it into the existing <head>; otherwise use
            # the document verbatim.
            lower = body_html.lower()
            if "</head>" in lower:
                idx = lower.find("</head>")
                full_html = body_html[:idx] + print_css + body_html[idx:]
            elif "<body" in lower:
                m = re.search(r"(?i)<body[^>]*>", body_html)
                pos = m.start()
                full_html = body_html[:pos] + f"<head>{print_css}</head>" + body_html[pos:]
            else:
                full_html = f"<!DOCTYPE html><html><head>{print_css}</head><body>{body_html}</body></html>"
        else:
            full_html = (
                f"<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
                f"<meta charset='utf-8'>\n"
                f"<meta name='viewport' content='width=device-width, initial-scale=1.0'>\n"
                f"<title>{self.current_path.stem if self.current_path else 'Export'}</title>\n"
                f"{css}{print_css}\n"
                f"</head>\n<body>\n{body_html}\n</body>\n</html>"
            )

        try:
            if fmt == "HTML":
                Path(path).write_text(full_html, encoding="utf-8")

            elif fmt == "JSON":
                import json as _json
                import re as _re
                from html.parser import HTMLParser
                from datetime import datetime

                class _RichExtractor(HTMLParser):
                    """Extract structured content: plain text, headings outline, code blocks, tables."""
                    def __init__(self):
                        super().__init__()
                        self._plain_lines: list[str] = []
                        self._buf: list[str] = []
                        self._skip_tags = {"style", "script"}
                        self._skip = False
                        self._current_tag = ""
                        # table state
                        self._in_table = False
                        self._row: list[str] = []
                        self._cell: list[str] = []
                        self._table_rows: list[list[str]] = []
                        # structured output
                        self.headings: list[dict] = []
                        self.code_blocks: list[dict] = []
                        self._in_code = False
                        self._code_lang = ""
                        self._code_buf: list[str] = []

                    def handle_starttag(self, tag, attrs):
                        self._current_tag = tag
                        if tag in self._skip_tags:
                            self._skip = True
                            return
                        attrs_d = dict(attrs)
                        if tag == "table":
                            self._in_table = True
                            self._table_rows = []
                        elif tag in ("tr",):
                            self._row = []
                        elif tag in ("td", "th"):
                            self._cell = []
                        elif tag == "br":
                            self._buf.append("\n")
                        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
                            self._flush_buf()
                        elif tag == "pre":
                            self._flush_buf()
                        elif tag == "code":
                            parent_class = attrs_d.get("class", "")
                            if "codehilite" not in parent_class:
                                pass  # inline code, handled via data
                        # detect code lang from div.language-X
                        if tag == "div":
                            cls = attrs_d.get("class", "")
                            m = _re.search(r"language-([\w+.#-]+)", cls)
                            if m:
                                self._in_code = True
                                self._code_lang = m.group(1)
                                self._code_buf = []

                    def handle_endtag(self, tag):
                        if tag in self._skip_tags:
                            self._skip = False
                            return
                        if tag in ("td", "th"):
                            self._row.append(" ".join("".join(self._cell).split()))
                            self._cell = []
                        elif tag == "tr":
                            if self._row:
                                self._table_rows.append(self._row[:])
                            self._row = []
                        elif tag == "table":
                            self._in_table = False
                            if self._table_rows:
                                # render as aligned text
                                widths = [max(len(r[c]) if c < len(r) else 0
                                              for r in self._table_rows)
                                          for c in range(max(len(r) for r in self._table_rows))]
                                for ri, row in enumerate(self._table_rows):
                                    cells = [str(row[c]).ljust(widths[c]) if c < len(row) else " " * widths[c]
                                             for c in range(len(widths))]
                                    self._plain_lines.append(" | ".join(cells))
                                    if ri == 0:
                                        self._plain_lines.append("-+-".join("-" * w for w in widths))
                            self._plain_lines.append("")
                        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                            txt = "".join(self._buf).strip()
                            level = int(tag[1])
                            if txt:
                                self.headings.append({"level": level, "text": txt})
                                self._plain_lines.append(("#" * level) + " " + txt)
                            self._buf = []
                        elif tag in ("p", "li"):
                            self._flush_buf()
                        elif tag == "div":
                            if self._in_code:
                                code_text = "".join(self._code_buf).strip()
                                if code_text:
                                    self.code_blocks.append({
                                        "language": self._code_lang if self._code_lang != "text" else "plain",
                                        "code": code_text,
                                    })
                                    self._plain_lines.append(f"```{self._code_lang}")
                                    self._plain_lines.append(code_text)
                                    self._plain_lines.append("```")
                                self._in_code = False
                                self._code_lang = ""
                                self._code_buf = []

                    def handle_data(self, data):
                        if self._skip:
                            return
                        if self._in_table and self._cell is not None:
                            self._cell.append(data)
                        elif self._in_code:
                            self._code_buf.append(data)
                        else:
                            self._buf.append(data)

                    def _flush_buf(self):
                        txt = "".join(self._buf).strip()
                        if txt:
                            self._plain_lines.append(txt)
                        self._buf = []

                    def get_plain(self):
                        self._flush_buf()
                        return "\n".join(self._plain_lines)

                extractor = _RichExtractor()
                extractor.feed(body_html)
                plain = extractor.get_plain()

                # Build headings tree
                def _build_tree(headings):
                    tree, stack = [], []
                    for h in headings:
                        node = {"level": h["level"], "text": h["text"], "children": []}
                        while stack and stack[-1]["level"] >= h["level"]:
                            stack.pop()
                        if stack:
                            stack[-1]["children"].append(node)
                        else:
                            tree.append(node)
                        stack.append(node)
                    return tree

                word_count = len(_re.findall(r"\S+", md_text))
                line_count = md_text.count("\n") + 1
                char_count = len(md_text)

                payload = {
                    "metadata": {
                        "title": self.current_path.stem if self.current_path else "Untitled",
                        "source_file": str(self.current_path) if self.current_path else None,
                        "theme": self.theme_name,
                        "exported_at": datetime.now().isoformat(timespec="seconds"),
                        "stats": {
                            "word_count": word_count,
                            "line_count": line_count,
                            "char_count": char_count,
                            "heading_count": len(extractor.headings),
                            "code_block_count": len(extractor.code_blocks),
                        },
                    },
                    "outline": _build_tree(extractor.headings),
                    "headings": extractor.headings,
                    "code_blocks": extractor.code_blocks,
                    "source_markdown": md_text,
                    "rendered_html": full_html,
                    "plain_text": plain,
                }
                Path(path).write_text(
                    _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            elif fmt == "PDF":
                # Inject print-friendly CSS that WeasyPrint can fully honour:
                # @page for A4 size/margins, preserved backgrounds for syntax
                # highlighting, and clean table/blockquote/code styling.
                pdf_css = """
                <style>
                    @page {
                        size: A4;
                        margin: 20mm 18mm 24mm 18mm;
                    }
                    html, body {
                        background-color: #ffffff !important;
                    }
                    body {
                        font-size: 10pt;
                        font-family: sans-serif;
                        max-width: 100%;
                        margin: 0;
                        padding: 0;
                    }
                    h1, h2, h3, h4 { page-break-after: avoid; }
                    pre, code {
                        white-space: pre-wrap;
                        word-break: break-word;
                        font-size: 8.5pt;
                    }
                    table {
                        page-break-inside: avoid;
                        width: 100%;
                        border-collapse: collapse;
                    }
                    th, td {
                        border: 1px solid #ccc;
                        padding: 5px 8px;
                    }
                    th { font-weight: bold; }
                    img { max-width: 100%; height: auto; }
                    a { text-decoration: underline; }
                    blockquote {
                        border-left: 3px solid #ccc;
                        margin-left: 0;
                        padding-left: 12px;
                        color: #555;
                    }
                </style>
                """
                if "</head>" in full_html:
                    pdf_html = full_html.replace("</head>", pdf_css + "</head>", 1)
                else:
                    pdf_html = pdf_css + full_html

                # WeasyPrint has no native checkbox widget renderer (see
                # _replace_task_checkboxes_for_pdf), so task-list checkboxes
                # are swapped for plain-text glyphs before rendering.
                pdf_html = _replace_task_checkboxes_for_pdf(pdf_html)

                # Resolve relative image paths against the source file's own
                # directory, so `![](images/foo.png)`-style references work.
                pdf_base_url = self.current_path.parent.as_uri() if self.current_path else None
                _pdf_via_weasyprint(pdf_html, path, base_url=pdf_base_url)

            self.status.showMessage(f"Exported {path}", 3000)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Could not export {fmt}:\n{exc}")

    # ---------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            local = urls[0].toLocalFile()
            if local:
                self.open_file(Path(local))
                event.acceptProposedAction()


# --------------------------------------------------------------------------------- #
def _install_qt_message_filter():
    """Silence one specific, harmless Qt warning while forwarding all others.

    Windows' *native* Save/Open file dialog (QFileDialog.getSaveFileName)
    builds an internal font with an unset point size, so Qt prints
    "QFont::setPointSize: Point size <= 0 (-1), must be greater than 0" every
    time the dialog opens. It's cosmetic -- the dialog and the save work fine
    -- but it clutters the console on each save. We drop just that line and
    pass everything else through to Qt's default handler, so real warnings are
    still visible.
    """
    from PySide6.QtCore import qInstallMessageHandler

    def _filter(mode, ctx, msg):
        if "Point size <= 0" in msg:
            return  # swallow this known-harmless native-dialog warning only
        # Re-emit anything else on stderr (Qt's default sink), preserving it.
        sys.stderr.write(msg + "\n")

    qInstallMessageHandler(_filter)


def main():
    _install_qt_message_filter()
    app = QApplication(sys.argv)
    app.setApplicationName("Markdown Viewer")
    # Register any bundled fonts before building the UI, so the editor/UI can
    # use them regardless of what's installed system-wide.
    _load_bundled_fonts()
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    win = MarkdownViewer(initial)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
