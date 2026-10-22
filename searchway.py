#!/usr/bin/env python3
"""
Searchway - instant "search as you type" file finder for Linux.

An Everything-style tool that works on Wayland and X11 (GTK 4 or GTK 3 + SQLite).
Index = SQLite FTS5 trigram table, so substring search is instant.
Updates are incremental: only directories whose mtime changed are re-read.

Usage:
  searchway                    start the GUI (indexes in the background)
  searchway -r /data -r ~      index specific roots (default: your home)
  searchway -q "report pdf"    search from the terminal
  searchway --reindex          wipe the index and rebuild it
  searchway --no-rust          force the pure-Python scanner
  searchway --gtk 3            force GTK 3 (default: GTK 4 if installed)
  searchway --install          create a .desktop launcher

Search syntax: words are ANDed, case-insensitive substring match on the
file name. A word containing "/" is matched against the full path.
Masks: "*" = any run of characters, "?" = exactly one. A mask must match the
whole name, so "*.pdf" = names ending in .pdf, "img_??.jpg" = img_01.jpg ...
Use "*word*" for "contains". A path mask matches the end of the full path.

Optional speed-up: build the Rust walker in ./indexer (see README) and put
"searchway-indexer" next to this script or in ~/.local/bin.

Debian/Ubuntu deps:  sudo apt install python3-gi gir1.2-gtk-4.0   (or gir1.2-gtk-3.0)
"""

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path

VERSION = "0.2.1"
APP_ID = "io.github.searchway"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "searchway"
DB_PATH = CACHE_DIR / "index.db"
DEFAULT_ROOTS = [str(Path.home())]
RESCAN_INTERVAL = 60       # seconds between background incremental scans
RESULT_LIMIT = 1000
FLUSH_EVERY = 20000        # rows per DB transaction while indexing
USE_RUST = not os.environ.get("SEARCHWAY_NO_RUST")

SKIP_NAMES = {".git", "node_modules", "__pycache__", ".cache"}
SKIP_PATHS = {
    "/proc", "/sys", "/dev", "/run", "/lost+found",
    str(Path.home() / ".local/share/Trash"),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS dirs(path TEXT PRIMARY KEY, mtime INTEGER) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    dir TEXT NOT NULL, name TEXT NOT NULL,
    is_dir INTEGER NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS files_dir ON files(dir);
CREATE VIRTUAL TABLE IF NOT EXISTS names USING fts5(
    name, content='files', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO names(rowid, name) VALUES (new.id, new.name);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO names(names, rowid, name) VALUES ('delete', old.id, old.name);
END;
"""

SORTS = {
    "relevance": ("length(f.name)", "f.name"),
    "name": ("f.name COLLATE NOCASE",),
    "path": ("f.dir COLLATE NOCASE", "f.name COLLATE NOCASE"),
    "size": ("f.size",),
    "mtime": ("f.mtime",),
}


# --------------------------------------------------------------------------- #
# Database / search
# --------------------------------------------------------------------------- #

def connect():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.executescript(SCHEMA)
    # Unicode-aware, case-insensitive substring test (SQLite LIKE is ASCII-only).
    con.create_function("contains", 2, lambda h, n: int(n in h.lower()), deterministic=True)
    # Whole-string wildcard match for non-ASCII patterns (see _wild_like).
    con.create_function("wmatch", 2, lambda h, p: int(_rx(p).fullmatch(h.lower()) is not None),
                        deterministic=True)
    return con


@lru_cache(maxsize=256)
def _rx(pattern):
    return re.compile(pattern, re.DOTALL)


def _like(expr, word):
    """ASCII needles use fast native LIKE; others use the Unicode-aware contains()."""
    if word.isascii():
        esc = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"{expr} LIKE ? ESCAPE '\\'", "%" + esc + "%"
    return f"contains({expr}, ?)", word


def _wild_like(expr, word, tail_only):
    """Wildcard word (* = any run, ? = one char), anchored to the whole value.

    With tail_only (path words) the start is free, only the end is anchored.
    ASCII patterns use native LIKE; others use the Unicode-aware wmatch().
    """
    if word.isascii():
        pat = "".join("%" if c == "*" else "_" if c == "?" else
                      "\\" + c if c in "\\%_" else c for c in word)
        return f"{expr} LIKE ? ESCAPE '\\'", ("%" + pat if tail_only else pat)
    rx = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in word)
    return f"wmatch({expr}, ?)", (".*" + rx if tail_only else rx)


def build_query(text, sort="relevance", desc=False, limit=RESULT_LIMIT):
    words = text.lower().split()
    if not words:
        return None
    fts, where, wargs = [], [], []
    for w in words:
        wild = "*" in w or "?" in w
        if "/" in w:
            full = "(f.dir || '/' || f.name)"
            clause, arg = _wild_like(full, w, True) if wild else _like(full, w)
            where.append(clause); wargs.append(arg)
        elif wild:
            clause, arg = _wild_like("f.name", w, False)
            where.append(clause); wargs.append(arg)
            # Literal pieces of the mask can still use the trigram index.
            for seg in re.split(r"[*?]+", w):
                if len(seg) >= 3:
                    fts.append('"' + seg.replace('"', '""') + '"')
        elif len(w) >= 3:                      # trigram index needs >= 3 chars
            fts.append('"' + w.replace('"', '""') + '"')
        else:
            clause, arg = _like("f.name", w)
            where.append(clause); wargs.append(arg)

    cols = "f.dir, f.name, f.is_dir, f.size, f.mtime"
    args = []
    if fts:
        sql = f"SELECT {cols} FROM names JOIN files f ON f.id = names.rowid WHERE names MATCH ?"
        args.append(" AND ".join(fts))
    else:
        sql = f"SELECT {cols} FROM files f WHERE 1=1"
    for clause in where:
        sql += " AND " + clause
    args += wargs

    exprs = SORTS.get(sort, SORTS["relevance"])
    direction = " DESC" if desc and sort != "relevance" else ""
    sql += " ORDER BY " + ", ".join(e + direction for e in exprs) + " LIMIT ?"
    args.append(limit)
    return sql, args


def search(con, text, **kw):
    q = build_query(text, **kw)
    if q is None:
        return []
    return con.execute(*q).fetchall()


# --------------------------------------------------------------------------- #
# Indexer
# --------------------------------------------------------------------------- #

def find_scanner():
    """Locate the optional Rust walker (searchway-indexer), or return None."""
    candidates = [
        os.environ.get("SEARCHWAY_INDEXER"),
        str(Path(__file__).resolve().parent / "searchway-indexer"),
        shutil.which("searchway-indexer"),
        str(Path.home() / ".local/bin/searchway-indexer"),
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


class Indexer(threading.Thread):
    def __init__(self, roots):
        super().__init__(daemon=True)
        absroots = sorted({os.path.abspath(r) for r in roots})
        # Drop roots nested inside another root so nothing is walked twice.
        self.roots = [r for r in absroots if not any(
            r != o and r.startswith(o.rstrip("/") + "/") for o in absroots)]
        # Shown in the status bar: report the walker that is (about to be) used,
        # not the one that finished last.
        self.backend = "rust" if (USE_RUST and find_scanner()) else "python"
        self.proc = None
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()
        self.scanning = False
        self.total = 0
        self.error = ""

    def run(self):
        con = connect()
        while not self.stop_evt.is_set():
            self.scanning = True
            try:
                self.scan(con)
                self.error = ""
            except Exception as exc:  # keep the thread alive
                self.error = str(exc)
            self.scanning = False
            self.wake_evt.wait(RESCAN_INTERVAL)
            self.wake_evt.clear()

    def rescan(self):
        self.wake_evt.set()

    def stop(self):
        self.stop_evt.set()
        self.wake_evt.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def _under_roots(self, path):
        return any(path == r or path.startswith(r.rstrip("/") + "/") for r in self.roots)

    def scan(self, con):
        """Prefer the Rust walker; fall back to the pure-Python one."""
        exe = find_scanner() if USE_RUST else None
        if exe:
            self.backend = "rust"
            try:
                self._scan_rust(con, exe)
                return
            except (OSError, RuntimeError) as exc:
                self.error = f"rust scanner failed ({exc}), using Python"
        self.backend = "python"
        self._scan_python(con)

    def _scan_rust(self, con, exe):
        """Run searchway-indexer and apply its records to the database.

        Record = 5 NUL-terminated fields: dir, name, a, b, c
          name != "": entry (a=is_dir, b=size, c=mtime)
          name == "": marker (a=S start dir / E end dir, c=mtime / G gone dir)
        Records of different directories may interleave, so a directory is only
        written once its E marker has arrived (an interrupted walk never leaves
        half a directory behind).
        """
        known = con.execute("SELECT path, mtime FROM dirs").fetchall()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd, known_path = tempfile.mkstemp(prefix="known-", dir=CACHE_DIR)
        rows, clear, dir_rows, gone = [], [], [], []
        open_units, done = {}, set()
        complete = False

        def flush():
            if not (rows or clear or dir_rows or gone):
                return
            clear.extend(gone)
            con.executemany("DELETE FROM files WHERE dir=?", [(d,) for d in clear])
            con.executemany(
                "INSERT INTO files(dir, name, is_dir, size, mtime) VALUES (?,?,?,?,?)", rows)
            con.executemany("INSERT OR REPLACE INTO dirs(path, mtime) VALUES (?,?)", dir_rows)
            con.executemany("DELETE FROM dirs WHERE path=?", [(g,) for g in gone])
            con.commit()
            rows.clear(); clear.clear(); dir_rows.clear(); gone.clear()
            self.total = con.execute("SELECT count(*) FROM files").fetchone()[0]

        try:
            with os.fdopen(fd, "wb") as fh:
                for path, mtime in known:
                    fh.write(path.encode("utf-8") + b"\0" + str(mtime).encode() + b"\0")
            cmd = [exe, "--known", known_path]
            for r in self.roots:
                cmd += ["--root", r]
            for name in sorted(SKIP_NAMES):
                cmd += ["--skip-name", name]
            for path in sorted(SKIP_PATHS):
                cmd += ["--skip-path", path]
            self.proc = proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            tail, fields = b"", []
            while True:
                chunk = proc.stdout.read1(1 << 20)
                if not chunk:
                    break
                parts = (tail + chunk).split(b"\0")
                tail = parts.pop()                     # possibly incomplete last field
                fields.extend(parts)
                usable = len(fields) - len(fields) % 5
                for i in range(0, usable, 5):
                    d, name, a, b, c = fields[i:i + 5]
                    try:
                        d = d.decode("utf-8")
                        if name:
                            unit = open_units.get(d)
                            if unit is not None:
                                unit.append((d, name.decode("utf-8"), int(a == b"1"), int(b), int(c)))
                        elif a == b"S":
                            open_units[d] = []
                        elif a == b"E":
                            entries = open_units.pop(d, None)
                            if entries is not None and d not in done:
                                done.add(d)
                                clear.append(d)
                                dir_rows.append((d, int(c)))
                                rows.extend(entries)
                                if len(rows) >= FLUSH_EVERY:
                                    flush()
                        elif a == b"G":
                            gone.append(d)
                    except (UnicodeDecodeError, ValueError):
                        continue
                del fields[:usable]
                if self.stop_evt.is_set():
                    proc.terminate()
                    break

            err = proc.stderr.read().decode("utf-8", "replace").strip()
            rc = proc.wait()
            complete = rc == 0
            flush()                                    # only fully finished directories
            if self.stop_evt.is_set():
                return
            if not complete:
                raise RuntimeError(f"scanner exited with code {rc}: {err}")
        finally:
            self.proc = None
            try:
                os.unlink(known_path)
            except OSError:
                pass
        self.total = con.execute("SELECT count(*) FROM files").fetchone()[0]

    def _scan_python(self, con):
        known = dict(con.execute("SELECT path, mtime FROM dirs"))
        seen = set()
        rows, clear, dir_rows = [], [], []

        def flush():
            if not (rows or clear or dir_rows):
                return
            con.executemany("DELETE FROM files WHERE dir=?", [(d,) for d in clear])
            con.executemany(
                "INSERT INTO files(dir, name, is_dir, size, mtime) VALUES (?,?,?,?,?)", rows)
            con.executemany("INSERT OR REPLACE INTO dirs(path, mtime) VALUES (?,?)", dir_rows)
            con.commit()
            rows.clear(); clear.clear(); dir_rows.clear()
            self.total = con.execute("SELECT count(*) FROM files").fetchone()[0]

        stack = list(self.roots)
        while stack:
            if self.stop_evt.is_set():
                flush()
                return
            d = stack.pop()
            if d in seen:
                continue
            try:
                d.encode("utf-8")
                st = os.stat(d)
            except (OSError, UnicodeEncodeError):
                continue
            seen.add(d)
            mtime = int(st.st_mtime)
            changed = known.get(d) != mtime
            entries = []
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                            if is_dir and e.name not in SKIP_NAMES and e.path not in SKIP_PATHS:
                                stack.append(e.path)
                            if changed:
                                e.name.encode("utf-8")
                                s = e.stat(follow_symlinks=False)
                                entries.append((d, e.name, int(is_dir),
                                                0 if is_dir else s.st_size, int(s.st_mtime)))
                        except (OSError, UnicodeEncodeError):
                            continue
            except OSError:
                continue
            if changed:
                clear.append(d)
                dir_rows.append((d, mtime))
                rows.extend(entries)
                if len(rows) >= FLUSH_EVERY:
                    flush()

        # Directories that vanished (only those under the roots we scanned).
        for gone in [p for p in known if p not in seen and self._under_roots(p)]:
            clear.append(gone)
            con.execute("DELETE FROM dirs WHERE path=?", (gone,))
        flush()
        self.total = con.execute("SELECT count(*) FROM files").fetchone()[0]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def install_desktop():
    target = Path.home() / ".local/share/applications/searchway.desktop"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Searchway\n"
        "Comment=Instant file search\n"
        f"Exec={sys.executable} {Path(__file__).resolve()}\n"
        "Icon=system-search\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )
    print(f"Wrote {target}")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #


def pick_gtk(pref=None):
    """Return "4" or "3" depending on what is installed (GTK 4 is preferred)."""
    try:
        import gi
    except ImportError:
        sys.exit("Searchway needs PyGObject: sudo apt install python3-gi gir1.2-gtk-4.0")
    for v in ([pref] if pref else ["4", "3"]):
        try:
            gi.require_version("Gtk", f"{v}.0")
            return v
        except ValueError:
            continue
    if pref:
        sys.exit(f"GTK {pref} is not installed: sudo apt install gir1.2-gtk-{pref}.0")
    sys.exit("No GTK found: sudo apt install python3-gi gir1.2-gtk-4.0  (or gir1.2-gtk-3.0)")


def run_gui(roots, pref=None):
    version = pick_gtk(pref if pref in ("3", "4") else None)
    print(f"searchway: using GTK {version}", file=sys.stderr)
    (_run_gtk4 if version == "4" else _run_gtk3)(roots)


def make_helpers(Gio, GLib):
    """Icon lookup and file launching, shared by the GTK 3 and GTK 4 windows."""
    icon_cache = {}

    def icon_for(name, is_dir):
        if is_dir:
            return "folder"
        ext = os.path.splitext(name)[1].lower()
        if ext not in icon_cache:
            ctype, _ = Gio.content_type_guess(name, None)
            icon = Gio.content_type_get_icon(ctype)
            names = icon.get_names() if hasattr(icon, "get_names") else []
            icon_cache[ext] = names[0] if names else "text-x-generic"
        return icon_cache[ext]

    def open_path(path):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error:
            subprocess.Popen(["xdg-open", path])

    def reveal(path):
        if shutil.which("dolphin"):
            subprocess.Popen(["dolphin", "--select", path])
        else:
            open_path(os.path.dirname(path))
    return icon_for, open_path, reveal


def _run_gtk4(roots):
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, Pango

    icon_for, open_path, reveal = make_helpers(Gio, GLib)

    class Window(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Searchway")
            self.set_default_size(980, 620)
            self.indexer = app.indexer
            self.con = connect()
            self.sort_key, self.sort_desc = "relevance", False
            self.last_total, self.was_scanning = -1, True

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for side in ("top", "start", "end", "bottom"):
                getattr(box, f"set_margin_{side}")(8)
            self.set_child(box)

            self.entry = Gtk.SearchEntry()
            try:
                self.entry.set_property("placeholder-text", "Type to search...")
            except TypeError:
                pass                      # property only exists in GTK >= 4.10
            self.entry.connect("search-changed", lambda *_: self.refresh())
            self.entry.connect("activate", self.on_entry_activate)
            box.append(self.entry)

            # icon, name, dir, size, date, fullpath
            self.store = Gtk.ListStore(str, str, str, str, str, str)
            self.view = Gtk.TreeView(model=self.store)
            self.view.set_headers_clickable(True)
            self.view.connect("row-activated", lambda v, p, c: self.open_selected())
            self.columns = {}

            right_click = Gtk.GestureClick()
            right_click.set_button(3)
            right_click.connect("pressed", self.on_right_click)
            self.view.add_controller(right_click)

            col = Gtk.TreeViewColumn(title="Name")
            pix = Gtk.CellRendererPixbuf()
            txt = Gtk.CellRendererText()
            col.pack_start(pix, False)
            col.pack_start(txt, True)
            col.add_attribute(pix, "icon-name", 0)
            col.add_attribute(txt, "text", 1)
            self._setup_column(col, "name", expand=True, width=320)

            for title, idx, key, align, width in (
                ("Path", 2, "path", 0.0, 380),
                ("Size", 3, "size", 1.0, 90),
                ("Modified", 4, "mtime", 0.0, 140),
            ):
                r = Gtk.CellRendererText()
                r.set_property("xalign", align)
                r.set_property("ellipsize", Pango.EllipsizeMode.END)
                c = Gtk.TreeViewColumn(title=title)
                c.pack_start(r, True)
                c.add_attribute(r, "text", idx)
                self._setup_column(c, key, expand=(key == "path"), width=width)

            sw = Gtk.ScrolledWindow()
            sw.set_child(self.view)
            sw.set_vexpand(True)
            sw.set_hexpand(True)
            box.append(sw)

            self.status = Gtk.Label(xalign=0)
            box.append(self.status)

            # Capture phase: we see Esc / Down / Ctrl+... before the focused widget does.
            keys = Gtk.EventControllerKey()
            keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            keys.connect("key-pressed", self.on_key)
            self.add_controller(keys)

            GLib.timeout_add(500, self.tick)
            self.tick()

        def _setup_column(self, col, key, expand, width):
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_min_width(60)
            col.set_fixed_width(width)
            col.set_clickable(True)
            col.connect("clicked", lambda c, k=key: self.on_header(k))
            self.view.append_column(col)
            self.columns[key] = col

        # ---- searching ----
        def on_header(self, key):
            if self.sort_key == key:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_key, self.sort_desc = key, key in ("size", "mtime")
            for k, c in self.columns.items():
                c.set_sort_indicator(k == key)
                c.set_sort_order(Gtk.SortType.DESCENDING if self.sort_desc
                                 else Gtk.SortType.ASCENDING)
            self.refresh()

        def refresh(self):
            text = self.entry.get_text()
            self.view.set_model(None)
            self.store.clear()
            if not text.strip():
                self.view.set_model(self.store)
                self.set_status("")
                return
            t0 = time.perf_counter()
            try:
                rows = search(self.con, text, sort=self.sort_key, desc=self.sort_desc)
            except sqlite3.Error as exc:
                self.view.set_model(self.store)
                self.status.set_text(f"Search error: {exc}")
                return
            ms = (time.perf_counter() - t0) * 1000
            for d, name, is_dir, size, mtime in rows:
                self.store.append([
                    icon_for(name, is_dir), name, d,
                    "" if is_dir else human_size(size), human_time(mtime),
                    os.path.join(d, name),
                ])
            self.view.set_model(self.store)
            more = "+" if len(rows) >= RESULT_LIMIT else ""
            self.set_status(f"{len(rows)}{more} results in {ms:.0f} ms")

        def set_status(self, prefix):
            ix = self.indexer
            state = "indexing..." if ix.scanning else "up to date"
            parts = [prefix] if prefix else []
            parts.append(f"{ix.total:,} items indexed ({state}, {ix.backend} scanner)")
            if ix.error:
                parts.append(f"indexer error: {ix.error}")
            self.status.set_text("  |  ".join(parts))

        def tick(self):
            ix = self.indexer
            if self.was_scanning and not ix.scanning and ix.total != self.last_total:
                self.refresh()          # re-run the query once indexing finishes
            self.was_scanning, self.last_total = ix.scanning, ix.total
            text = self.status.get_text()
            prefix = text.split("  |  ")[0] if "results" in text else ""
            self.set_status(prefix)
            return True

        # ---- actions ----
        def selected_path(self):
            model, it = self.view.get_selection().get_selected()
            return model[it][5] if it else None

        def open_selected(self):
            path = self.selected_path()
            if path:
                open_path(path)

        def on_entry_activate(self, *_):
            if self.selected_path() is None and len(self.store):
                self.view.set_cursor(Gtk.TreePath.new_first(), None, False)
            self.open_selected()

        def on_key(self, ctrl_ctl, keyval, keycode, state):
            ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            if keyval == Gdk.KEY_Escape:
                if self.entry.get_text():
                    self.entry.set_text("")
                else:
                    self.close()
                return True
            if keyval == Gdk.KEY_Down and len(self.store):
                focus = self.get_focus()
                if focus is not None and (focus == self.entry or focus.is_ancestor(self.entry)):
                    self.view.grab_focus()
                    self.view.set_cursor(Gtk.TreePath.new_first(), None, False)
                    return True
                return False
            if ctrl and keyval in (Gdk.KEY_l, Gdk.KEY_L):
                self.entry.grab_focus()
                return True
            if keyval == Gdk.KEY_F5 or (ctrl and keyval in (Gdk.KEY_r, Gdk.KEY_R)):
                self.indexer.rescan()
                return True
            if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                path = self.selected_path()
                if path:
                    reveal(path)
                return True
            if ctrl and shift and keyval in (Gdk.KEY_c, Gdk.KEY_C):
                self.copy_path()
                return True
            return False

        def copy_path(self):
            path = self.selected_path()
            if not path:
                return
            clip = self.get_clipboard()
            try:
                if hasattr(clip, "set_text"):
                    clip.set_text(path)
                else:
                    clip.set_content(Gdk.ContentProvider.new_for_value(path))
            except Exception as exc:
                print(f"searchway: could not copy to clipboard: {exc}", file=sys.stderr)

        def on_right_click(self, gesture, n_press, x, y):
            bx, by = self.view.convert_widget_to_bin_window_coords(int(x), int(y))
            hit = self.view.get_path_at_pos(bx, by)
            if not hit:
                return
            self.view.set_cursor(hit[0], None, False)
            pop = Gtk.Popover()
            pop.set_has_arrow(False)
            items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for label, fn in (
                ("Open", self.open_selected),
                ("Open containing folder", lambda: reveal(self.selected_path())),
                ("Copy full path", self.copy_path),
            ):
                btn = Gtk.Button(label=label)
                btn.add_css_class("flat")
                btn.connect("clicked", lambda _b, f=fn, p=pop: (p.popdown(), f()))
                items.append(btn)
            pop.set_child(items)
            pop.set_parent(self.view)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            pop.set_pointing_to(rect)
            pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
            pop.popup()

    class App(Gtk.Application):
        def __init__(self):
            flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
            super().__init__(application_id=APP_ID, flags=flags)
            self.win = None
            self.indexer = None

        def do_activate(self):
            # A second launch (e.g. from a KDE shortcut) just raises the window.
            if self.win is None:
                self.indexer = Indexer(roots)
                self.indexer.start()
                self.win = Window(self)
            self.win.present()
            self.win.entry.grab_focus()

        def do_shutdown(self):
            if self.indexer:
                self.indexer.stop()
            Gtk.Application.do_shutdown(self)

    sys.exit(App().run([sys.argv[0]]))


def _run_gtk3(roots):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

    icon_for, open_path, reveal = make_helpers(Gio, GLib)

    class Window(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="Searchway")
            self.set_default_size(980, 620)
            self.indexer = app.indexer
            self.con = connect()
            self.sort_key, self.sort_desc = "relevance", False
            self.last_total, self.was_scanning = -1, True

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            for side in ("top", "start", "end", "bottom"):
                getattr(box, f"set_margin_{side}")(8)
            self.add(box)

            self.entry = Gtk.SearchEntry()
            self.entry.set_placeholder_text("Type to search...")
            self.entry.connect("search-changed", lambda *_: self.refresh())
            self.entry.connect("activate", self.on_entry_activate)
            self.entry.connect("key-press-event", self.on_entry_key)
            box.pack_start(self.entry, False, False, 0)

            # icon, name, dir, size, date, fullpath
            self.store = Gtk.ListStore(str, str, str, str, str, str)
            self.view = Gtk.TreeView(model=self.store)
            self.view.set_headers_clickable(True)
            self.view.set_rubber_banding(False)
            self.view.connect("row-activated", lambda v, p, c: self.open_selected())
            self.view.connect("button-press-event", self.on_button)
            self.columns = {}

            col = Gtk.TreeViewColumn("Name")
            pix = Gtk.CellRendererPixbuf()
            txt = Gtk.CellRendererText()
            col.pack_start(pix, False)
            col.pack_start(txt, True)
            col.add_attribute(pix, "icon-name", 0)
            col.add_attribute(txt, "text", 1)
            self._setup_column(col, "name", expand=True, width=320)

            for title, idx, key, align, width in (
                ("Path", 2, "path", 0.0, 380),
                ("Size", 3, "size", 1.0, 90),
                ("Modified", 4, "mtime", 0.0, 140),
            ):
                r = Gtk.CellRendererText()
                r.set_property("xalign", align)
                r.set_property("ellipsize", 3)  # Pango.EllipsizeMode.END
                c = Gtk.TreeViewColumn(title, r, text=idx)
                self._setup_column(c, key, expand=(key == "path"), width=width)

            sw = Gtk.ScrolledWindow()
            sw.add(self.view)
            box.pack_start(sw, True, True, 0)

            self.status = Gtk.Label(xalign=0)
            box.pack_start(self.status, False, False, 0)

            self.connect("key-press-event", self.on_key)
            GLib.timeout_add(500, self.tick)
            self.show_all()
            self.tick()

        def _setup_column(self, col, key, expand, width):
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_min_width(60)
            col.set_fixed_width(width)
            col.set_clickable(True)
            col.connect("clicked", lambda c, k=key: self.on_header(k))
            self.view.append_column(col)
            self.columns[key] = col

        # ---- searching ----
        def on_header(self, key):
            if self.sort_key == key:
                self.sort_desc = not self.sort_desc
            else:
                self.sort_key, self.sort_desc = key, key in ("size", "mtime")
            for k, c in self.columns.items():
                c.set_sort_indicator(k == key)
                c.set_sort_order(Gtk.SortType.DESCENDING if self.sort_desc
                                 else Gtk.SortType.ASCENDING)
            self.refresh()

        def refresh(self):
            text = self.entry.get_text()
            self.view.set_model(None)
            self.store.clear()
            if not text.strip():
                self.view.set_model(self.store)
                self.set_status("")
                return
            t0 = time.perf_counter()
            try:
                rows = search(self.con, text, sort=self.sort_key, desc=self.sort_desc)
            except sqlite3.Error as exc:
                self.view.set_model(self.store)
                self.status.set_text(f"Search error: {exc}")
                return
            ms = (time.perf_counter() - t0) * 1000
            for d, name, is_dir, size, mtime in rows:
                self.store.append([
                    icon_for(name, is_dir), name, d,
                    "" if is_dir else human_size(size), human_time(mtime),
                    os.path.join(d, name),
                ])
            self.view.set_model(self.store)
            more = "+" if len(rows) >= RESULT_LIMIT else ""
            self.set_status(f"{len(rows)}{more} results in {ms:.0f} ms")

        def set_status(self, prefix):
            ix = self.indexer
            state = "indexing..." if ix.scanning else "up to date"
            parts = [prefix] if prefix else []
            parts.append(f"{ix.total:,} items indexed ({state}, {ix.backend} scanner)")
            if ix.error:
                parts.append(f"indexer error: {ix.error}")
            self.status.set_text("  |  ".join(parts))

        def tick(self):
            ix = self.indexer
            if self.was_scanning and not ix.scanning and ix.total != self.last_total:
                self.refresh()          # re-run the query once indexing finishes
            self.was_scanning, self.last_total = ix.scanning, ix.total
            text = self.status.get_text()
            prefix = text.split("  |  ")[0] if "results" in text else ""
            self.set_status(prefix)
            return True

        # ---- actions ----
        def selected_path(self):
            model, it = self.view.get_selection().get_selected()
            return model[it][5] if it else None

        def open_selected(self):
            path = self.selected_path()
            if path:
                open_path(path)

        def on_entry_activate(self, *_):
            if self.selected_path() is None and len(self.store):
                self.view.set_cursor(Gtk.TreePath.new_first(), None, False)
            self.open_selected()

        def on_entry_key(self, widget, event):
            if event.keyval == Gdk.KEY_Down and len(self.store):
                self.view.grab_focus()
                self.view.set_cursor(Gtk.TreePath.new_first(), None, False)
                return True
            return False

        def on_key(self, widget, event):
            ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
            shift = event.state & Gdk.ModifierType.SHIFT_MASK
            k = event.keyval
            if k == Gdk.KEY_Escape:
                if self.entry.get_text():
                    self.entry.set_text("")
                else:
                    self.close()
                return True
            if ctrl and k in (Gdk.KEY_l, Gdk.KEY_L):
                self.entry.grab_focus()
                return True
            if k == Gdk.KEY_F5 or (ctrl and k in (Gdk.KEY_r, Gdk.KEY_R)):
                self.indexer.rescan()
                return True
            if ctrl and k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                path = self.selected_path()
                if path:
                    reveal(path)
                return True
            if ctrl and shift and k in (Gdk.KEY_c, Gdk.KEY_C):
                self.copy_path()
                return True
            return False

        def copy_path(self):
            path = self.selected_path()
            if path:
                Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(path, -1)

        def on_button(self, view, event):
            if event.button != 3:
                return False
            hit = view.get_path_at_pos(int(event.x), int(event.y))
            if not hit:
                return False
            view.set_cursor(hit[0], None, False)
            menu = Gtk.Menu()
            for label, fn in (
                ("Open", self.open_selected),
                ("Open containing folder", lambda: reveal(self.selected_path())),
                ("Copy full path", self.copy_path),
            ):
                item = Gtk.MenuItem(label=label)
                item.connect("activate", lambda _w, f=fn: f())
                menu.append(item)
            menu.show_all()
            menu.popup_at_pointer(event)
            return True

    class App(Gtk.Application):
        def __init__(self):
            flags = getattr(Gio.ApplicationFlags, "DEFAULT_FLAGS", Gio.ApplicationFlags.FLAGS_NONE)
            super().__init__(application_id=APP_ID, flags=flags)
            self.win = None
            self.indexer = None

        def do_activate(self):
            # A second launch (e.g. from a KDE shortcut) just raises the window.
            if self.win is None:
                self.indexer = Indexer(roots)
                self.indexer.start()
                self.win = Window(self)
            self.win.present()
            self.win.entry.grab_focus()

        def do_shutdown(self):
            if self.indexer:
                self.indexer.stop()
            Gtk.Application.do_shutdown(self)

    sys.exit(App().run([sys.argv[0]]))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

HELP_EPILOG = f"""\
search syntax (words are ANDed, case-insensitive):
  report pdf        names containing both "report" and "pdf"
  *.pdf             mask: * = any characters, ? = exactly one character
  img_??.jpg        a mask must match the whole name; use *word* for "contains"
  docs/*.txt        a word with "/" is matched against the end of the full path

examples:
  searchway                          open the search window
  searchway -q "*.mp4"               print all .mp4 files and exit
  searchway -r ~ -r /mnt/data        index two folders
  searchway --reindex                rebuild the index from scratch

keys in the window:
  Enter           open the selected file      Ctrl+Enter    show it in Dolphin
  Down            jump to the results         Ctrl+Shift+C  copy the full path
  Ctrl+L          focus the search box        F5 / Ctrl+R   rescan now
  Esc             clear the search, then close; right click for the context menu

environment:
  SEARCHWAY_INDEXER=PATH   use this searchway-indexer binary (Rust)
  SEARCHWAY_NO_RUST=1      always use the Python indexer
  SEARCHWAY_GTK=3|4        force a GTK version (default: 4 if installed, else 3)
  XDG_CACHE_HOME           the index is stored in $XDG_CACHE_HOME/searchway
                           (now: {CACHE_DIR})

Uninstall: see the README ("Uninstall" section).
"""


def main():
    ap = argparse.ArgumentParser(
        prog="searchway",
        description="Searchway - instant file search for Linux (Wayland and X11).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    ap.add_argument("-r", "--root", action="append", metavar="DIR",
                    help="folder to index, can be repeated (default: your home folder)")
    ap.add_argument("-q", "--query", metavar="TEXT",
                    help="search from the terminal: print matching paths and exit")
    ap.add_argument("--reindex", action="store_true",
                    help="delete the index and rebuild it from scratch")
    ap.add_argument("--install", action="store_true",
                    help="create an application-menu launcher (.desktop file)")
    ap.add_argument("--no-rust", action="store_true",
                    help="do not use the Rust indexer, use the built-in Python one")
    ap.add_argument("--gtk", choices=["3", "4"], help="force a GTK version (default: 4 if installed, else 3)")
    ap.add_argument("--version", action="version", version=f"searchway {VERSION}")
    args = ap.parse_args()
    global USE_RUST
    if args.no_rust:
        USE_RUST = False

    if args.install:
        install_desktop()
        return
    if args.reindex:
        for suffix in ("", "-wal", "-shm"):
            Path(str(DB_PATH) + suffix).unlink(missing_ok=True)

    roots = [str(Path(r).expanduser().resolve()) for r in (args.root or DEFAULT_ROOTS)]

    if args.query is not None:
        con = connect()
        ix = Indexer(roots)
        t0 = time.perf_counter()
        ix.scan(con)                     # incremental: fast when nothing changed
        print(f"# index ready: {ix.total:,} items ({time.perf_counter() - t0:.1f}s, "
              f"{ix.backend} scanner)", file=sys.stderr)
        for d, name, is_dir, _size, _mtime in search(con, args.query):
            print(os.path.join(d, name) + ("/" if is_dir else ""))
        return

    run_gui(roots, args.gtk or os.environ.get("SEARCHWAY_GTK"))


if __name__ == "__main__":
    main()
