# Searchway

Instant "search as you type" file finder for Linux, inspired by [Everything](https://www.voidtools.com/) on Windows.
Native on **Wayland** and X11 (GTK3), tested with the KDE Plasma.

- Search results update as you type, even with millions of indexed files
- Case-insensitive substring search, Unicode-aware (Cyrillic and other scripts work)
- Incremental background indexing: only changed folders are re-read
- Single file, no pip packages, no daemon, no root

> Searchway searches **file and folder names**, not file contents.

## Requirements

- Python 3.8+
- SQLite 3.34+ with FTS5 (any current distro ships this)
- PyGObject and GTK 3

| Distro | Install dependencies |
| --- | --- |
| Debian / Ubuntu | `sudo apt install python3-gi gir1.2-gtk-3.0` |
| Fedora | `sudo dnf install python3-gobject gtk3` |
| Arch | `sudo pacman -S python-gobject gtk3` |

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

**3. (Optional) Add an app-menu launcher**

```bash
searchway --install
```

This creates `~/.local/share/applications/searchway.desktop`.

**4. (Optional) Bind a global hotkey on KDE Plasma**

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
```

## How it works

Searchway walks the filesystem and stores names, sizes and modification times in a SQLite database with an FTS5 trigram index, which makes substring search fast. A background thread rescans every 60 seconds and re-reads only the folders whose modification time changed.

Skipped by default: `.git`, `node_modules`, `__pycache__`, `.cache`, the Trash, and virtual filesystems (`/proc`, `/sys`, `/dev`, `/run`). Edit `SKIP_NAMES` and `SKIP_PATHS` at the top of the script to change this.

Searchway only **reads** your files. It never modifies, moves or deletes anything outside its own cache folder.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `ValueError: Namespace Gtk not available` | install GTK 3 and PyGObject (see Requirements) |
| `no such tokenizer: trigram` | your SQLite is older than 3.34, update your system |
| A new file does not show up yet | wait up to a minute or press `F5` |
| Results look stale or broken | run `searchway --reindex` |
| Searching one or two characters feels slower | expected: the trigram index needs 3+ characters, so short queries scan the table |

## Uninstall

Searchway installs nothing outside your home folder, so removal is four commands.

```bash
# 1. Stop it if it is running (or just close the window)
pkill -f searchway

# 2. Remove the program and the launcher
rm -f ~/.local/bin/searchway
rm -f ~/.local/share/applications/searchway.desktop

# 3. Remove the index database (safe, it is only a cache)
rm -rf "${XDG_CACHE_HOME:-$HOME/.cache}/searchway"
```

Then, if you set one up:

4. Delete the shortcut in **System Settings → Keyboard → Shortcuts**

Dependencies such as `python3-gi` are shared with many other apps, so it is usually best to leave them installed. If you installed the script somewhere else, delete that file instead.

To only reset the index and keep Searchway installed, run `searchway --reindex`.
