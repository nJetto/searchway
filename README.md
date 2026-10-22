# Searchway

Instant "search as you type" file finder for Linux, inspired by [Everything](https://www.voidtools.com/) on Windows.
Native on **Wayland** and X11 (GTK 4 or GTK 3), tested with the KDE Plasma workflow in mind.

- Search results update as you type, even with millions of indexed files
- Case-insensitive substring search, Unicode-aware (Cyrillic and other scripts work)
- Wildcard masks: `*.pdf`, `img_??.jpg`, `report*`
- Incremental background indexing: only changed folders are re-read
- Optional multi-threaded **Rust** walker for fast first-time indexing (falls back to pure Python automatically)
- No pip packages, no daemon, no root

> Searchway searches **file and folder names**, not file contents.

## Requirements

- Python 3.8+
- SQLite 3.34+ with FTS5 (any current distro ships this)
- PyGObject and GTK 4 **or** GTK 3 (GTK 4 is used when installed, otherwise GTK 3)

| Distro | Install dependencies |
| --- | --- |
| Debian / Ubuntu | `sudo apt install python3-gi gir1.2-gtk-4.0` (or `gir1.2-gtk-3.0`) |
| Fedora | `sudo dnf install python3-gobject gtk4` (or `gtk3`) |
| Arch | `sudo pacman -S python-gobject gtk4` (or `gtk3`) |

Optional, for the Rust indexer: Rust 1.63+ (`sudo apt install cargo` on Debian/Ubuntu, `sudo dnf install cargo` on Fedora, `sudo pacman -S rust` on Arch). The Rust part has **no crate dependencies**, so building needs no network access beyond installing the toolchain.

## Quick start

**1. Install the script**

```bash
install -Dm755 searchway.py ~/.local/bin/searchway
```

Make sure `~/.local/bin` is in your `PATH` (it is by default on most distros once the folder exists and you log in again).

**2. Run it**

```bash
searchway
```

The first launch indexes your home folder in the background. You can start typing right away; results fill in as the index grows. Later launches are instant, and the index stays up to date on its own.

**3. (Optional) Build the fast Rust indexer**

```bash
cd indexer
cargo build --release
install -Dm755 target/release/searchway-indexer ~/.local/bin/searchway-indexer
cd ..
```

Searchway picks it up automatically (it looks for `SEARCHWAY_INDEXER`, then next to `searchway.py`, then in `PATH` and `~/.local/bin`). To check which indexer is active, look at the status bar (`... rust scanner`) or run `searchway -q zzzz` and read the first line it prints.

**4. (Optional) Add an app-menu launcher**

```bash
searchway --install
```

This creates `~/.local/share/applications/searchway.desktop`.

**5. (Optional) Bind a global hotkey on KDE Plasma**

Wayland does not allow apps to grab global hotkeys themselves, so let KDE do it:

1. Open **System Settings → Keyboard → Shortcuts**
2. Click **Add New → Command or Script**
3. Command: `searchway` (or the full path, e.g. `/home/you/.local/bin/searchway`)
4. Assign a shortcut, for example `Alt+Space`

Launching Searchway a second time does not open a second window. It brings the existing one to the front, so the shortcut works like a toggle.

> If the window does not come to the front, lower **System Settings → Window Management → Window Behavior → Focus stealing prevention**.

## Usage

### Search syntax

| Query | Meaning |
| --- | --- |
| `report` | names containing "report" |
| `report pdf` | names containing both "report" **and** "pdf" |
| `docs/ draft` | a word containing `/` is matched against the full path |
| `*.pdf` | names ending in `.pdf` |
| `report*` | names starting with `report` |
| `img_??.jpg` | `img_01.jpg`, `img_42.jpg`, but not `img_100.jpg` |
| `*.jp*g *2026*` | masks combine like any other words (all must match) |
| `docs/*.txt` | path masks match the **end** of the full path |

**Masks:** `*` matches any run of characters (including none), `?` matches exactly one character. A mask has to match the **whole** name, so `*.pdf` finds `a.pdf` but not `a.pdf.bak`; write `*word*` when you want "contains". Masks are case-insensitive too.

Results are capped at 1,000 rows. Click a column header to sort by name, path, size or date.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `Enter` | open the selected result (or the first one) |
| `Down` | jump from the search box to the results |
| `Ctrl+Enter` | show the selected item in Dolphin |
| `Ctrl+Shift+C` | copy the full path |
| `Ctrl+L` | focus the search box |
| `F5` / `Ctrl+R` | rescan now |
| `Esc` | clear the search, press again to close |
| Right click | open, open containing folder, copy path |

### Command line

```text
searchway                    start the GUI
searchway -r /data -r ~      index specific folders (default: your home folder)
searchway -q "report pdf"    search from the terminal and exit
searchway --reindex          wipe the index and rebuild it from scratch
searchway --install          create the .desktop launcher
searchway --no-rust          ignore the Rust indexer and use the Python one
searchway --gtk 3            force GTK 3 (or `--gtk 4`); default is GTK 4 when installed
```

Run `searchway --help` any time for this list plus the search syntax, keyboard shortcuts and environment variables, and `searchway --version` to see the version.

## How it works

Searchway walks the filesystem and stores names, sizes and modification times in a SQLite database with an FTS5 trigram index, which makes substring search fast. A background thread rescans every 60 seconds and re-reads only the folders whose modification time changed.

Two interchangeable indexers walk the disk:

- **Rust** (`indexer/`, optional): a multi-threaded walker with zero dependencies. It receives the list of known folders, skips unchanged ones and streams only the changes to Searchway, which writes them to SQLite in batches.
- **Python** (built in): the same algorithm, single-threaded. Used when the Rust binary is missing or fails.

Skipped by default: `.git`, `node_modules`, `__pycache__`, `.cache`, the Trash, and virtual filesystems (`/proc`, `/sys`, `/dev`, `/run`). Edit `SKIP_NAMES` and `SKIP_PATHS` at the top of the script to change this.

Searchway only **reads** your files. It never modifies, moves or deletes anything outside its own cache folder.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ValueError: Namespace Gtk not available` or `No GTK found` | install PyGObject and GTK 4 or GTK 3 (see Requirements) |
| Which GTK version is running? | Searchway prints `searchway: using GTK 4` to the terminal on start |
| The GTK 4 window misbehaves | run `searchway --gtk 3` (or set `SEARCHWAY_GTK=3`) and please open an issue |
| `no such tokenizer: trigram` | your SQLite is older than 3.34, update your system |
| A new file does not show up yet | wait up to a minute or press `F5` |
| Results look stale or broken | run `searchway --reindex` |
| Searching one or two characters feels slower | expected: the trigram index needs 3+ characters, so short queries scan the table |
| A mask like `*` or `??` is slow | masks without a literal piece of 3+ characters cannot use the index, add one (`*.pdf` is fast) |
| `cargo build` fails | make sure Rust is 1.63 or newer (`rustc --version`); you can also keep using the Python indexer |
| Want to force the Python indexer | run `searchway --no-rust` or set `SEARCHWAY_NO_RUST=1` |

## Uninstall

Searchway installs nothing outside your home folder, so removal is a handful of commands.

```bash
# 1. Stop it if it is running (or just close the window)
pkill -f searchway

# 2. Remove the program, the optional Rust indexer and the launcher
rm -f ~/.local/bin/searchway ~/.local/bin/searchway-indexer
rm -f ~/.local/share/applications/searchway.desktop

# 3. Remove the index database (safe, it is only a cache)
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/searchway"
```

Then, if you set one up:

4. Delete the shortcut in **System Settings → Keyboard → Shortcuts**

Dependencies such as `python3-gi` are shared with many other apps, so it is usually best to leave them installed. If you installed the script somewhere else, delete that file instead. The Rust toolchain (`cargo`) is only needed for building, remove it with your package manager if you installed it just for this and do not need it.

To only reset the index and keep Searchway installed, run `searchway --reindex`.
