#!/usr/bin/env python3
"""
Searchway - instant "search as you type" file finder for Linux.

An Everything-style tool that works on Wayland and X11 (GTK3 + SQLite).
Index = SQLite FTS5 trigram table, so substring search is instant.
Updates are incremental: only directories whose mtime changed are re-read.

Usage:
  searchway                    start the GUI (indexes in the background)
  searchway -r /data -r ~      index specific roots (default: your home)
  searchway -q "report pdf"    search from the terminal
  searchway --reindex          wipe the index and rebuild it
  searchway --install          create a .desktop launcher

Search syntax: words are ANDed, case-insensitive substring match on the
file name. A word containing "/" is matched against the full path.

Debian/Ubuntu deps:  sudo apt install python3-gi gir1.2-gtk-3.0
"""

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_ID = "io.github.searchway"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "searchway"
DB_PATH = CACHE_DIR / "index.db"
DEFAULT_ROOTS = [str(Path.home())]
RESCAN_INTERVAL = 60       # seconds between background incremental scans
RESULT_LIMIT = 1000
FLUSH_EVERY = 20000        # rows per DB transaction while indexing

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
    return con


def _like(expr, word):
    """ASCII needles use fast native LIKE; others use the Unicode-aware contains()."""
    if word.isascii():
        esc = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"{expr} LIKE ? ESCAPE '\\'", "%" + esc + "%"
    return f"contains({expr}, ?)", word


def build_query(text, sort="relevance", desc=False, limit=RESULT_LIMIT):
    words = text.lower().split()
    if not words:
        return None
    fts, where, wargs = [], [], []
    for w in words:
        if "/" in w:
            clause, arg = _like("(f.dir || '/' || f.name)", w)
            where.append(clause); wargs.append(arg)
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

class Indexer(threading.Thread):
    def __init__(self, roots):
        super().__init__(daemon=True)
        self.roots = [os.path.abspath(r) for r in roots]
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

    def _under_roots(self, path):
        return any(path == r or path.startswith(r.rstrip("/") + "/") for r in self.roots)

    def scan(self, con):
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

def run_gui(roots):
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gio, GLib, Gtk

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
            parts.append(f"{ix.total:,} items indexed ({state})")
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
                self.view.set_cursor(Gtk.TreePath.new_first())
            self.open_selected()

        def on_entry_key(self, widget, event):
            if event.keyval == Gdk.KEY_Down and len(self.store):
                self.view.grab_focus()
                self.view.set_cursor(Gtk.TreePath.new_first())
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
            view.set_cursor(hit[0])
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

def main():
    ap = argparse.ArgumentParser(description="Searchway - instant file search")
    ap.add_argument("-r", "--root", action="append", help="directory to index (repeatable)")
    ap.add_argument("-q", "--query", help="search from the terminal and exit")
    ap.add_argument("--reindex", action="store_true", help="wipe the index first")
    ap.add_argument("--install", action="store_true", help="create a .desktop launcher")
    args = ap.parse_args()

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
        print(f"# index ready: {ix.total:,} items ({time.perf_counter() - t0:.1f}s)", file=sys.stderr)
        for d, name, is_dir, _size, _mtime in search(con, args.query):
            print(os.path.join(d, name) + ("/" if is_dir else ""))
        return

    run_gui(roots)


if __name__ == "__main__":
    main()
