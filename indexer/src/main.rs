//! searchway-indexer: fast parallel filesystem walker for Searchway.
//!
//! No external crates. It walks the given roots with several threads and
//! streams records to stdout; the Searchway front-end ingests them into SQLite.
//!
//! Every record is five NUL-terminated UTF-8 fields: dir, name, a, b, c
//!
//!   entry   (name != "")  a = is_dir (0/1), b = size in bytes, c = mtime
//!   marker  (name == "")  a = "S"  the dir is being re-read: drop its old rows
//!                         a = "E"  the dir is finished: store mtime (in c)
//!                         a = "G"  the dir is gone: drop its rows and record
//!
//! Incremental mode: `--known FILE` holds `path\0mtime\0` pairs for every
//! directory indexed before. A directory whose mtime did not change is not
//! stat'ed entry by entry, it is only listed to find its sub-directories.

use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{BufWriter, Write};
use std::os::unix::fs::MetadataExt;
use std::os::unix::io::FromRawFd;
use std::process;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Condvar, Mutex};
use std::thread;

const FLUSH_BYTES: usize = 1 << 20;

/// Works with `cargo build` and with a bare `rustc -O src/main.rs`.
fn version() -> &'static str {
    option_env!("CARGO_PKG_VERSION").unwrap_or("dev")
}

struct Config {
    roots: Vec<String>,
    skip_names: HashSet<String>,
    skip_paths: HashSet<String>,
    known_file: Option<String>,
    threads: usize,
}

struct Queue {
    stack: Vec<String>,
    active: usize,
}

struct Shared {
    queue: Mutex<Queue>,
    cv: Condvar,
    /// dir -> (mtime at last index, visited during this run)
    known: Mutex<HashMap<String, (i64, bool)>>,
    out: Mutex<BufWriter<File>>,
    abort: AtomicBool,
    dirs: AtomicU64,
    entries: AtomicU64,
}

impl Shared {
    fn trigger_abort(&self) {
        self.abort.store(true, Ordering::SeqCst);
        // Taking the lock once guarantees no worker is between its abort
        // check and its wait(), so nobody misses the wake-up below.
        drop(self.queue.lock().unwrap());
        self.cv.notify_all();
    }

    fn emit(&self, buf: &mut Vec<u8>) {
        if buf.is_empty() {
            return;
        }
        let ok = {
            let mut w = self.out.lock().unwrap();
            w.write_all(buf).is_ok()
        };
        buf.clear();
        if !ok {
            // stdout closed (the front-end went away): stop walking.
            self.trigger_abort();
        }
    }
}

fn usage() {
    println!(
        "searchway-indexer {}\n\n\
         usage: searchway-indexer --root PATH [--root PATH ...]\n\
         \x20       [--known FILE] [--skip-name NAME ...] [--skip-path PATH ...]\n\
         \x20       [--threads N]\n\n\
         Streams filesystem records to stdout. Meant to be run by Searchway.",
        version()
    );
}

fn need(args: &mut impl Iterator<Item = String>, flag: &str) -> String {
    match args.next() {
        Some(v) => v,
        None => {
            eprintln!("{} needs a value", flag);
            process::exit(2);
        }
    }
}

fn parse_args() -> Config {
    let mut cfg = Config {
        roots: Vec::new(),
        skip_names: HashSet::new(),
        skip_paths: HashSet::new(),
        known_file: None,
        threads: 0,
    };
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--root" => cfg.roots.push(need(&mut args, "--root")),
            "--skip-name" => {
                cfg.skip_names.insert(need(&mut args, "--skip-name"));
            }
            "--skip-path" => {
                cfg.skip_paths.insert(need(&mut args, "--skip-path"));
            }
            "--known" => cfg.known_file = Some(need(&mut args, "--known")),
            "--threads" => cfg.threads = need(&mut args, "--threads").parse().unwrap_or(0),
            "-h" | "--help" => {
                usage();
                process::exit(0);
            }
            "--version" => {
                println!("searchway-indexer {}", version());
                process::exit(0);
            }
            other => {
                eprintln!("unknown argument: {}", other);
                process::exit(2);
            }
        }
    }
    cfg
}

fn load_known(path: &str) -> HashMap<String, (i64, bool)> {
    let mut map = HashMap::new();
    let data = match fs::read(path) {
        Ok(d) => d,
        Err(_) => return map,
    };
    let mut it = data.split(|b| *b == 0);
    loop {
        let raw_path = match it.next() {
            Some(p) if !p.is_empty() => p,
            _ => break,
        };
        let raw_mtime = match it.next() {
            Some(m) => m,
            None => break,
        };
        let p = match std::str::from_utf8(raw_path) {
            Ok(s) => s.to_string(),
            Err(_) => continue,
        };
        let mtime = std::str::from_utf8(raw_mtime)
            .ok()
            .and_then(|s| s.parse::<i64>().ok())
            .unwrap_or(0);
        map.insert(p, (mtime, false));
    }
    map
}

fn join(dir: &str, name: &str) -> String {
    let mut s = String::with_capacity(dir.len() + name.len() + 1);
    s.push_str(dir);
    if !dir.ends_with('/') {
        s.push('/');
    }
    s.push_str(name);
    s
}

fn under_roots(path: &str, roots: &[String]) -> bool {
    roots.iter().any(|r| {
        let r = r.trim_end_matches('/');
        path == r
            || (path.len() > r.len()
                && path.starts_with(r)
                && path.as_bytes()[r.len()] == b'/')
    })
}

fn put_marker(buf: &mut Vec<u8>, dir: &str, kind: u8, mtime: i64) {
    buf.extend_from_slice(dir.as_bytes());
    buf.push(0);
    buf.push(0); // empty name
    buf.push(kind);
    buf.push(0);
    buf.push(0); // empty b
    let _ = write!(buf, "{}", mtime);
    buf.push(0);
}

fn put_entry(buf: &mut Vec<u8>, dir: &str, name: &str, is_dir: bool, size: u64, mtime: i64) {
    buf.extend_from_slice(dir.as_bytes());
    buf.push(0);
    buf.extend_from_slice(name.as_bytes());
    buf.push(0);
    buf.push(if is_dir { b'1' } else { b'0' });
    buf.push(0);
    let _ = write!(buf, "{}", size);
    buf.push(0);
    let _ = write!(buf, "{}", mtime);
    buf.push(0);
}

fn process_dir(
    dir: &str,
    sh: &Shared,
    cfg: &Config,
    subdirs: &mut Vec<String>,
    buf: &mut Vec<u8>,
) {
    let dir_meta = match fs::metadata(dir) {
        Ok(m) => m,
        Err(_) => return,
    };
    let mtime = dir_meta.mtime();

    let changed = {
        let mut known = sh.known.lock().unwrap();
        match known.get_mut(dir) {
            Some(entry) => {
                entry.1 = true;
                entry.0 != mtime
            }
            None => true,
        }
    };
    sh.dirs.fetch_add(1, Ordering::Relaxed);

    let rd = match fs::read_dir(dir) {
        Ok(r) => r,
        Err(_) => return,
    };
    if changed {
        put_marker(buf, dir, b'S', 0);
    }

    for ent in rd {
        if sh.abort.load(Ordering::Relaxed) {
            return;
        }
        let ent = match ent {
            Ok(e) => e,
            Err(_) => continue,
        };
        let name_os = ent.file_name();
        let name = match name_os.to_str() {
            Some(n) => n,
            None => continue, // not valid UTF-8: skip, like the Python walker
        };
        // file_type() uses d_type from readdir: no extra syscall on Linux.
        let is_dir = match ent.file_type() {
            Ok(t) => t.is_dir(), // symlinks are NOT followed
            Err(_) => continue,
        };
        if is_dir && !cfg.skip_names.contains(name) {
            let full = join(dir, name);
            if !cfg.skip_paths.contains(&full) {
                subdirs.push(full);
            }
        }
        if changed {
            let md = match ent.metadata() {
                Ok(m) => m,
                Err(_) => continue,
            };
            let size = if is_dir { 0 } else { md.size() };
            put_entry(buf, dir, name, is_dir, size, md.mtime());
            sh.entries.fetch_add(1, Ordering::Relaxed);
            if buf.len() >= FLUSH_BYTES {
                sh.emit(buf);
            }
        }
    }
    if changed {
        put_marker(buf, dir, b'E', mtime);
    }
}

fn worker(sh: &Shared, cfg: &Config) {
    let mut buf: Vec<u8> = Vec::with_capacity(1 << 16);
    loop {
        let dir = {
            let mut q = sh.queue.lock().unwrap();
            loop {
                if sh.abort.load(Ordering::SeqCst) {
                    return;
                }
                if let Some(d) = q.stack.pop() {
                    q.active += 1;
                    break d;
                }
                if q.active == 0 {
                    // Nothing queued and nobody working: the walk is over.
                    sh.cv.notify_all();
                    return;
                }
                q = sh.cv.wait(q).unwrap();
            }
        };

        let mut subdirs: Vec<String> = Vec::new();
        process_dir(&dir, sh, cfg, &mut subdirs, &mut buf);
        sh.emit(&mut buf);

        let mut q = sh.queue.lock().unwrap();
        q.active -= 1;
        let pushed = !subdirs.is_empty();
        q.stack.extend(subdirs);
        if pushed || q.active == 0 {
            sh.cv.notify_all();
        }
    }
}

fn main() {
    let cfg = parse_args();
    if cfg.roots.is_empty() {
        eprintln!("searchway-indexer: at least one --root is required");
        process::exit(2);
    }

    let known = match &cfg.known_file {
        Some(p) => load_known(p),
        None => HashMap::new(),
    };
    let threads = if cfg.threads > 0 {
        cfg.threads
    } else {
        thread::available_parallelism()
            .map(|n| n.get())
            .unwrap_or(4)
            .min(8)
            .max(2)
    };

    // Raw handle on fd 1 so our own buffering is the only buffering.
    let stdout_file = unsafe { File::from_raw_fd(1) };
    let shared = Shared {
        queue: Mutex::new(Queue {
            stack: cfg.roots.clone(),
            active: 0,
        }),
        cv: Condvar::new(),
        known: Mutex::new(known),
        out: Mutex::new(BufWriter::with_capacity(1 << 18, stdout_file)),
        abort: AtomicBool::new(false),
        dirs: AtomicU64::new(0),
        entries: AtomicU64::new(0),
    };

    thread::scope(|s| {
        for _ in 0..threads {
            s.spawn(|| worker(&shared, &cfg));
        }
    });

    let mut gone = 0u64;
    if !shared.abort.load(Ordering::SeqCst) {
        let mut buf: Vec<u8> = Vec::new();
        {
            let known = shared.known.lock().unwrap();
            for (path, state) in known.iter() {
                if !state.1 && under_roots(path, &cfg.roots) {
                    put_marker(&mut buf, path, b'G', 0);
                    gone += 1;
                }
            }
        }
        shared.emit(&mut buf);
    }

    let flushed = shared.out.lock().unwrap().flush().is_ok();
    eprintln!(
        "dirs={} entries={} gone={} threads={}",
        shared.dirs.load(Ordering::Relaxed),
        shared.entries.load(Ordering::Relaxed),
        gone,
        threads
    );
    if shared.abort.load(Ordering::SeqCst) || !flushed {
        process::exit(1);
    }
}
