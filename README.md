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

Optional, only if you build the Rust indexer yourself (not needed with the prebuilt binary from Releases): Rust 1.63+ (`sudo apt install cargo` on Debian/Ubuntu, `sudo dnf install cargo` on Fedora, `sudo pacman -S rust` on Arch). The Rust part has **no crate dependencies**, so building needs no network access beyond installing the toolchain.

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

**3. (Optional) Install the fast Rust indexer**

The Rust indexer makes the first scan much faster. Without it Searchway uses its built-in Python indexer, so this step is optional.

*Option A: prebuilt binary (no Rust needed)*

Download `searchway-indexer-x86_64-linux` from the [Releases](../../releases) page, then install it:

```bash
cd ~/Downloads
install -Dm755 searchway-indexer-x86_64-linux ~/.local/bin/searchway-indexer
searchway-indexer --version
```

`install -Dm755` creates `~/.local/bin` if needed and sets the executable bit (browsers drop it from downloaded files). The binary is statically linked and works on any x86_64 Linux. On other architectures (ARM, ...) use Option B.

If you downloaded the `.zip` / `.tar.gz` archive instead, the indexer is already inside, named `searchway-indexer`, next to `searchway.py`: nothing more to install, Searchway finds it there.

*Option B: build from source*

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

### Browsing

With an empty search box the window lists the first 1,000 items of the whole index, sorted by the selected column (name by default). Click a header to change the order, for example **Modified** for a "recently changed files" view. The status bar always shows how many items are indexed.

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
searchway -x node_modules    do not index this folder (repeatable, see below)
```

Run `searchway --help` any time for this list plus the search syntax, keyboard shortcuts and environment variables, and `searchway --version` to see the version.

### Excluding folders

Searchway indexes **everything** under your home folder (hidden folders, `.git`, `node_modules`, caches, the Trash), except virtual filesystems (`/proc`, `/sys`, `/dev`, `/run`). To skip folders:

```bash
searchway -x node_modules -x ~/Games     # on the command line
```

or list them in `~/.config/searchway/exclude.txt`, one per line:

```text
# a name skips every folder with that name, anywhere
node_modules
.cache
# an absolute path (or ~/...) skips just that folder
~/.local/share/Trash
```

The excluded folder itself stays searchable, only its contents are left out. Items that were indexed earlier are removed on the next scan. Use the `-x` flag in the command of your KDE shortcut too, or prefer the exclude file, which every launch picks up.

## How it works

Searchway walks the filesystem and stores names, sizes and modification times in a SQLite database with an FTS5 trigram index, which makes substring search fast. A background thread rescans every 60 seconds and re-reads only the folders whose modification time changed.

Two interchangeable indexers walk the disk:

- **Rust** (`indexer/`, optional): a multi-threaded walker with zero dependencies. It receives the list of known folders, skips unchanged ones and streams only the changes to Searchway, which writes them to SQLite in batches.
- **Python** (built in): the same algorithm, single-threaded. Used when the Rust binary is missing or fails.

Nothing is skipped by default except virtual filesystems (`/proc`, `/sys`, `/dev`, `/run`), see [Excluding folders](#excluding-folders).

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
| Fewer items than another tool (FSearch, `find`) | older versions skipped `.git`, `node_modules`, `.cache` and the Trash; update to the latest version, then check your exclude list |
| `searchway-indexer: Permission denied` | the executable bit is missing, reinstall it with `install -Dm755 ...` as shown above |
| `Exec format error` when running the binary | it is built for x86_64 only, build it from source (Option B) |
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
