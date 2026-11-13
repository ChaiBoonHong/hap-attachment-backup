#!/usr/bin/env python3
"""HAP attachment backup.

Backs up the files in HAP worksheet attachment fields, which HAP's own app
backup does not include. Each file is saved as

    <backup set>/<App>/<Worksheet>/<record title>_<original file name>.<ext>

so HAP's "Import Attachments" (File Name Matching Field = title field,
delimiter "_") can attach it back to its record.

Reads MongoDB and HAP storage only; it never writes to HAP, except with
--restore-storage, which only puts back files that are missing from storage.

Every run is logged to LOG_FILE (backup.conf). --debug logs each command and
each file. --check tests the server, --status shows the backups, --log shows
the last backup run, --support-bundle packs logs and settings for support.

Python 3.8+ standard library only. See README.md.
"""

import argparse
import csv
import datetime as dt
import errno
import hashlib
import io
import json
import logging
import os
import platform
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback

VERSION = "1.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
MARK = "HAPBK\t"
INFO_MARK = "HAPBK_INFO\t"
SET_RE = re.compile(r"^(full|incr)-(\d{4}-\d{2}-\d{2})_(\d{4}(?:\d{2})?)$")
BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
LOG_LINE_RE = re.compile(r"^\S+ \S+ \[(\w+)\] \[(\d{8}-\d{6}-\d+)\] (.*)$")
CRON_FILE = "/etc/cron.d/hap-attachment-backup"
ROTATE_FILE = "/etc/logrotate.d/hap-attachment-backup"
LOCK_NAME = "hap-attachment-backup.lock"
NOT_IN_STORAGE = re.compile(r"Object does not exist|NoSuchKey|key does not exist", re.I)
ABORT_AFTER = 10     # stop when this many files fail before any file is copied

DEFAULTS = {
    "BACKUP_DIR": "/data/hap-attachment-backup",
    "KEEP_FULL": "4",
    "KEEP_INCR_DAYS": "14",
    "MIN_FREE_PCT": "10",
    "MONGO_CONTAINER": "auto",
    "APP_CONTAINER": "auto",
    "MONGO_SHELL": "auto",
    "MONGO_URI": "",
    "MINIO_ALIAS": "file",
    "DELIMITER": "_",
    "TITLE_MAX_CHARS": "100",
    "NAME_MAX_CHARS": "150",
    "INCLUDE_APPS": "",
    "EXCLUDE_APPS": "",
    "LOG_FILE": "/var/log/hap-attachment-backup.log",
    "LOG_LEVEL": "INFO",
    "EXPORT_TIMEOUT": "3600",
    "COPY_TIMEOUT": "3600",
}
INT_KEYS = ("KEEP_FULL", "KEEP_INCR_DAYS", "MIN_FREE_PCT", "TITLE_MAX_CHARS", "NAME_MAX_CHARS",
            "EXPORT_TIMEOUT", "COPY_TIMEOUT")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Images of the HAP containers (international and China editions); compose service name as fallback.
MONGO_IMAGES = (r"(^|/)sc:", r"mingdaoyun-sc")
APP_IMAGES = (r"(^|/)hap:", r"mingdaoyun-community", r"mingdaoyun-hap")
HAP_DATABASES = ("mdworksheet", "mdwsrows", "mdattachment", "mdapps")

MANIFEST_COLS = ["app", "worksheet", "field", "record_id", "title", "original_name",
                 "backup_file", "size", "sha256", "bucket", "storage_key", "file_id",
                 "app_id", "worksheet_id", "field_id", "uploaded_at", "note"]
MISSING_COLS = ["app", "worksheet", "field", "record_id", "title", "file_id",
                "bucket", "storage_key", "reason"]

EXIT_OK, EXIT_FATAL, EXIT_COPY_FAILED, EXIT_BUSY, EXIT_LOW_DISK = 0, 1, 2, 3, 4
EXIT_NAMES = {0: "OK", 1: "ERROR", 2: "COPY_FAILED", 3: "BUSY", 4: "LOW_DISK"}

RUN_ID = f"{dt.datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}"
RESULT = {}          # facts about this run, written to last_run.json


class ConfigError(Exception):
    pass


class CopyError(RuntimeError):
    """One file could not be read from HAP storage (the run continues)."""


# ---------------------------------------------------------------- logging

log = logging.getLogger("hapbk")


class _RunId(logging.Filter):
    def filter(self, record):
        record.run = RUN_ID
        return True


log.addFilter(_RunId())
FILE_FMT = logging.Formatter("%(asctime)s [%(levelname)s] [%(run)s] %(message)s", "%Y-%m-%d %H:%M:%S")
CONSOLE_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")


class _Buffer(logging.Handler):
    """Keeps this run's lines so they can be copied into the backup set as run.log."""

    def __init__(self):
        super().__init__()
        self.lines = []
        self.setFormatter(FILE_FMT)

    def emit(self, record):
        self.lines.append(self.format(record))


BUFFER = _Buffer()
SET_LOG = []         # file handler of the backup set being written


def setup_logging(level, log_file, console_level):
    log.setLevel(logging.DEBUG)
    log.propagate = False
    lvl = getattr(logging, level)
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(max(lvl, getattr(logging, console_level)))
    con.setFormatter(CONSOLE_FMT)
    log.addHandler(con)
    BUFFER.setLevel(lvl)
    log.addHandler(BUFFER)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            new = not os.path.exists(log_file)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            if new:
                os.chmod(log_file, 0o640)
            fh.setLevel(lvl)
            fh.setFormatter(FILE_FMT)
            log.addHandler(fh)
        except OSError as e:
            log.warning("cannot write log file %s: %s - logging to the screen only", log_file, e)


def file_log(level, msg):
    """Write a line to the log file(s) only, not to the screen."""
    rec = log.makeRecord(log.name, level, __file__, 0, msg, None, None)
    rec.run = RUN_ID
    for h in log.handlers:
        if isinstance(h, logging.FileHandler) and level >= h.level:
            h.handle(rec)


def attach_set_log(folder):
    """Start <set>/run.log with everything logged so far in this run."""
    path = os.path.join(folder, "run.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(BUFFER.lines) + ("\n" if BUFFER.lines else ""))
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(BUFFER.level)
    fh.setFormatter(FILE_FMT)
    log.addHandler(fh)
    SET_LOG.append(fh)


def detach_set_log():
    while SET_LOG:
        fh = SET_LOG.pop()
        log.removeHandler(fh)
        fh.close()


def mask(text):
    """Hide passwords in MongoDB connection strings."""
    return re.sub(r"(mongodb(?:\+srv)?://[^:/@\s]+:)[^@\s]+@", r"\1***@", str(text))


def short(text, n=400):
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-n:]


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def human_time(sec):
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    if sec < 172800:
        return f"{sec // 3600}h {sec % 3600 // 60:02d}m"
    return f"{sec // 86400} days"


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def user_name():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


# ---------------------------------------------------------------- config

def load_conf(path):
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    cfg, warns = dict(DEFAULTS), []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                warns.append(f"line {n} ignored, not KEY=VALUE: {line[:60]}")
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key not in DEFAULTS:
                warns.append(f"line {n}: unknown setting {key} (ignored)")
                continue
            cfg[key] = val
    for key in INT_KEYS:
        try:
            cfg[key] = int(cfg[key])
        except ValueError:
            raise ConfigError(f"{key} must be a whole number, found {cfg[key]!r}")
    cfg["LOG_LEVEL"] = {"WARN": "WARNING"}.get(cfg["LOG_LEVEL"].upper(), cfg["LOG_LEVEL"].upper())
    if cfg["LOG_LEVEL"] not in LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LEVELS)}, found {cfg['LOG_LEVEL']!r}")
    if not cfg["DELIMITER"]:
        raise ConfigError("DELIMITER must not be empty")
    if not os.path.isabs(cfg["BACKUP_DIR"]):
        raise ConfigError(f"BACKUP_DIR must be an absolute path, found {cfg['BACKUP_DIR']!r}")
    for key in ("MONGO_CONTAINER", "APP_CONTAINER", "MONGO_SHELL"):
        cfg[key] = cfg[key] or "auto"
    for key in ("INCLUDE_APPS", "EXCLUDE_APPS"):
        cfg[key] = [x.strip() for x in cfg[key].split(",") if x.strip()]
    return cfg, warns


def cfg_for_log(cfg):
    return {k: (mask(cfg[k]) if isinstance(cfg[k], str) else cfg[k]) for k in DEFAULTS}


# ---------------------------------------------------------------- commands on the host

def run(argv, timeout=None, show=None, stdin=None, binary=False):
    """Run a command; log it, its exit code, time and errors at DEBUG level."""
    shown = mask(show or " ".join(shlex.quote(x) for x in argv))
    log.debug("run: %s", shown)
    t0 = time.monotonic()
    kw = {} if binary else {"encoding": "utf-8", "errors": "replace"}
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, stdin=stdin, **kw)
    except FileNotFoundError:
        raise RuntimeError(f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {shown}")
    err = p.stderr.decode("utf-8", "replace") if binary else p.stderr
    log.debug("  exit %s in %.2fs, %d bytes out%s", p.returncode, time.monotonic() - t0, len(p.stdout or ""),
              f", stderr: {short(mask(err), 300)}" if err.strip() else "")
    return p


def docker_ps():
    p = run(["docker", "ps", "--format", '{{.Names}}\t{{.Image}}\t{{.Label "com.docker.compose.service"}}'],
            timeout=60)
    if p.returncode != 0:
        raise RuntimeError("Docker is not reachable: " + short(p.stderr, 300))
    rows = []
    for line in p.stdout.splitlines():
        if line.strip():
            rows.append(tuple((line.split("\t") + ["", ""])[:3]))
    return rows


def find_containers(rows, patterns, service):
    found = [n for n, img, _ in rows if any(re.search(p, img) for p in patterns)]
    return found or [n for n, _, svc in rows if svc == service]


def resolve(cfg):
    """Replace 'auto' container and shell settings with what is found on this server."""
    if cfg.get("_resolved"):
        return
    rows = None
    for key, pats, svc in (("MONGO_CONTAINER", MONGO_IMAGES, "sc"), ("APP_CONTAINER", APP_IMAGES, "app")):
        if cfg[key].lower() != "auto":
            continue
        rows = rows if rows is not None else docker_ps()
        found = find_containers(rows, pats, svc)
        if len(found) != 1:
            running = ", ".join(n for n, _, _ in rows) or "none"
            what = "no HAP container found" if not found else "more than one candidate: " + ", ".join(found)
            raise RuntimeError(f"{key}=auto: {what}. Running containers: {running}. "
                               f"Set {key} in backup.conf")
        cfg[key] = found[0]
        log.debug("%s=auto -> %s", key, found[0])
    if cfg["MONGO_SHELL"].lower() == "auto":
        p = run(["docker", "exec", cfg["MONGO_CONTAINER"], "sh", "-c",
                 "command -v mongo || command -v mongosh"], timeout=60)
        shell = os.path.basename(p.stdout.strip().splitlines()[0]) if p.returncode == 0 and p.stdout.strip() else ""
        if not shell:
            raise RuntimeError(f"no mongo or mongosh shell in container {cfg['MONGO_CONTAINER']} "
                               f"({short(p.stderr, 200) or 'not found'}). Check MONGO_CONTAINER")
        cfg["MONGO_SHELL"] = shell
        log.debug("MONGO_SHELL=auto -> %s", shell)
    cfg["_resolved"] = True


def hap_info(cfg):
    info = {"image": "", "version": ""}
    p = run(["docker", "inspect", "--format", "{{json .Config}}", cfg["APP_CONTAINER"]], timeout=60)
    if p.returncode == 0:
        try:
            conf = json.loads(p.stdout)
            info["image"] = conf.get("Image") or ""
            env = dict(e.split("=", 1) for e in (conf.get("Env") or []) if "=" in e)
            info["version"] = env.get("ENV_APP_VERSION") or (
                info["image"].rsplit(":", 1)[-1] if ":" in info["image"] else "")
        except ValueError:
            pass
    return info


def mongo_eval(cfg, js, what, timeout):
    cmd = ["docker", "exec", cfg["MONGO_CONTAINER"], cfg["MONGO_SHELL"]]
    if cfg["MONGO_URI"]:
        cmd.append(cfg["MONGO_URI"])
    cmd += ["--quiet", "--norc", "--eval", js]
    show = " ".join(shlex.quote(x) for x in cmd[:-1]) + f" <{what}>"
    p = run(cmd, timeout=timeout, show=show)
    if p.returncode != 0:
        raise RuntimeError(f"MongoDB query ({what}) failed: " + short(mask(p.stderr or p.stdout), 600))
    return p.stdout


def export_refs(cfg):
    """Every attachment reference in active records (read-only MongoDB query)."""
    with open(os.path.join(HERE, "export.js"), encoding="utf-8") as f:
        js = f.read()
    t0 = time.monotonic()
    out = mongo_eval(cfg, js, "export.js", cfg["EXPORT_TIMEOUT"])
    refs, other = [], []
    for line in out.splitlines():
        if line.startswith(MARK):
            try:
                refs.append(json.loads(line[len(MARK):]))
            except ValueError as e:
                raise RuntimeError(f"unreadable export line ({e}): {short(line, 200)}")
        elif line.startswith(INFO_MARK):
            log.debug("export: %s", line[len(INFO_MARK):])
        elif line.strip():
            other.append(line)
    if other:
        log.debug("export: %d other output lines, first: %s", len(other), short(other[0], 200))
    log.info("MongoDB export: %d file references in %.1fs", len(refs), time.monotonic() - t0)
    return refs


def bucket_of(server):
    """'https://host/file/mdpic/' -> 'mdpic'."""
    path = re.sub(r"^[a-zA-Z]+://[^/]+", "", server or "")
    parts = [p for p in path.split("/") if p]
    return parts[-1] if parts else ""


def storage_key(ref):
    return (ref.get("path") or "") + (ref.get("name") or "")


def copy_from_storage(cfg, bucket, key, dest):
    """Stream one object out of HAP storage into dest. Returns (sha256, bytes, seconds)."""
    src = f"{cfg['MINIO_ALIAS']}/{bucket}/{key}"
    argv = ["docker", "exec", cfg["APP_CONTAINER"], "mc", "cat", src]
    tmp = dest + ".part"
    h, n, t0 = hashlib.sha256(), 0, time.monotonic()
    try:
        out = open(tmp, "wb")
    except OSError as e:
        raise RuntimeError(f"cannot write {tmp}: {e}")
    killed = []
    with out:
        try:
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            raise RuntimeError("command not found: docker")
        timer = threading.Timer(cfg["COPY_TIMEOUT"], lambda: (killed.append(1), p.kill()))
        timer.start()
        try:
            for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
                out.write(chunk)
                h.update(chunk)
                n += len(chunk)
            err = p.stderr.read().decode("utf-8", "replace").strip()
            rc = p.wait()
        except OSError as e:
            p.kill()
            p.wait()
            out.close()
            os.remove(tmp)
            what = "disk full" if e.errno == errno.ENOSPC else str(e)
            raise RuntimeError(f"cannot write {dest}: {what}")
        finally:
            timer.cancel()
    if rc != 0:
        os.remove(tmp)
        if killed:
            raise CopyError(f"timed out after {cfg['COPY_TIMEOUT']}s (COPY_TIMEOUT)")
        raise CopyError(err.splitlines()[-1] if err else f"mc cat exited {rc}")
    os.replace(tmp, dest)
    return h.hexdigest(), n, time.monotonic() - t0


def object_exists(cfg, bucket, key):
    p = run(["docker", "exec", cfg["APP_CONTAINER"], "mc", "stat", f"{cfg['MINIO_ALIAS']}/{bucket}/{key}"],
            timeout=120)
    return p.returncode == 0


def put_to_storage(cfg, bucket, key, src):
    with open(src, "rb") as f:
        p = run(["docker", "exec", "-i", cfg["APP_CONTAINER"], "mc", "pipe", f"{cfg['MINIO_ALIAS']}/{bucket}/{key}"],
                timeout=cfg["COPY_TIMEOUT"], stdin=f, binary=True)
    if p.returncode != 0:
        raise RuntimeError(short(p.stderr.decode("utf-8", "replace"), 300))


# ---------------------------------------------------------------- lock (one backup or restore at a time)

def lock_path():
    return os.path.join("/run/lock" if os.path.isdir("/run/lock") else "/tmp", LOCK_NAME)


def acquire_lock():
    """Returns (fd, None) when locked, or (None, holder) when another run holds the lock."""
    import fcntl
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 200).decode("utf-8", "replace").strip() or "unknown process"
        os.close(fd)
        return None, holder
    os.ftruncate(fd, 0)
    os.write(fd, f"pid {os.getpid()}, run {RUN_ID}, started {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n".encode())
    return fd, None


def lock_holder():
    if not os.path.exists(lock_path()):
        return None
    try:
        fd, holder = acquire_lock()
    except OSError:
        return None
    if fd is not None:
        os.close(fd)
    return holder


# ---------------------------------------------------------------- naming

def clean(text, limit):
    text = BAD_CHARS.sub("-", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip().lstrip(".").strip()
    return text[:limit].rstrip(" .")


def fit_bytes(stem, ext, max_bytes=240):
    while stem and len((stem + ext).encode("utf-8")) > max_bytes:
        stem = stem[:-1]
    return stem + ext


def file_name(ref, cfg):
    title = clean(ref.get("title"), cfg["TITLE_MAX_CHARS"]) or ref.get("rowId") or "no-title"
    stored = ref.get("name") or ""
    ext = os.path.splitext(stored)[1]
    orig = (ref.get("oname") or "").strip() or os.path.splitext(stored)[0] or ref["fid"]
    if ext and orig.lower().endswith(ext.lower()):
        orig = orig[: -len(ext)]
    orig = clean(orig, cfg["NAME_MAX_CHARS"]) or ref["fid"]
    return fit_bytes(f"{title}{cfg['DELIMITER']}{orig}", ext)


def unique(name, used):
    stem, ext = os.path.splitext(name)
    cand, n = name, 2
    while cand.lower() in used:
        cand = f"{stem} ({n}){ext}"
        n += 1
    used.add(cand.lower())
    return cand


class Folders:
    """Stable, unique, safe folder names for apps and worksheets."""

    def __init__(self):
        self.apps, self.app_used = {}, set()
        self.ws, self.ws_used = {}, {}

    def app(self, ref):
        aid = ref.get("appId") or "unknown-app"
        if aid not in self.apps:
            name = clean(ref.get("app"), 80) or aid
            self.apps[aid] = unique(name, self.app_used)
        return self.apps[aid]

    def worksheet(self, ref):
        app = self.app(ref)
        wid = ref["wsId"]
        if wid not in self.ws:
            used = self.ws_used.setdefault(app, set())
            name = clean(ref.get("ws"), 80) or wid
            if name.lower() in used:
                name = f"{name} ({wid[-6:]})"
            used.add(name.lower())
            self.ws[wid] = name
        return os.path.join(app, self.ws[wid])


# ---------------------------------------------------------------- state, sets and retention

def ref_key(ref):
    return f"{ref['fid']}|{ref.get('rowId')}|{ref.get('fieldId')}"


def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, data, indent=0):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


def list_sets(base):
    if not os.path.isdir(base):
        return []
    sets = [d for d in os.listdir(base) if SET_RE.match(d) and os.path.isdir(os.path.join(base, d))]
    return sorted(sets, key=lambda d: SET_RE.match(d).group(2, 3) + (d.startswith("incr"),))


def find_set(cfg, name):
    base = cfg["BACKUP_DIR"]
    if name in (None, "", "latest"):
        sets = list_sets(base)
        if not sets:
            raise RuntimeError(f"no backup sets in {base}")
        name = sets[-1]
    path = name if os.path.isabs(name) else os.path.join(base, name)
    if not os.path.isfile(os.path.join(path, "manifest.csv")):
        recent = ", ".join(list_sets(base)[-5:]) or "none"
        raise RuntimeError(f"not a backup set (no manifest.csv): {path}. Recent sets: {recent}")
    return path


def read_summary(path):
    data = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\n").split("=", 1)
                    data[k] = v
    except OSError:
        pass
    return data


def read_last_run(base):
    try:
        with open(os.path.join(base, "last_run.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def good_full(base, name):
    """A full set counts for KEEP_FULL only if it copied files and at most half of them failed."""
    sm = read_summary(os.path.join(base, name, "summary.txt"))
    try:
        copied, failed = int(sm.get("files_copied", "0")), int(sm.get("copy_failures", "0"))
    except ValueError:
        return False
    return copied > 0 and failed <= copied


def prune(cfg):
    base = cfg["BACKUP_DIR"]
    sets = list_sets(base)
    fulls = [d for d in sets if d.startswith("full-")]
    good = [d for d in fulls if good_full(base, d)]
    cutoff = dt.date.today() - dt.timedelta(days=cfg["KEEP_INCR_DAYS"])
    log.debug("retention: %d full sets (%d good), keep the last %d good ones; nightly sets from %s on",
              len(fulls), len(good), cfg["KEEP_FULL"], cutoff)
    if cfg["KEEP_FULL"] > 0 and len(good) > cfg["KEEP_FULL"]:
        oldest_kept = fulls.index(good[-cfg["KEEP_FULL"]])
        for d in fulls[:oldest_kept]:
            shutil.rmtree(os.path.join(base, d))
            log.info("retention: removed %s (KEEP_FULL=%d)", d, cfg["KEEP_FULL"])
    for d in sets:
        m = SET_RE.match(d)
        if m.group(1) == "incr" and dt.date.fromisoformat(m.group(2)) < cutoff:
            shutil.rmtree(os.path.join(base, d))
            log.info("retention: removed %s (KEEP_INCR_DAYS=%d)", d, cfg["KEEP_INCR_DAYS"])
    for d in os.listdir(base):
        p = os.path.join(base, d)
        if d.endswith(".partial") and os.path.isdir(p) and time.time() - os.path.getmtime(p) > 86400:
            shutil.rmtree(p)
            log.info("retention: removed unfinished %s", d)


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel shows non-English names
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


class Progress:
    """INFO line every 500 files or 60 seconds."""

    def __init__(self, what, total_files, total_bytes=0):
        self.what, self.total, self.total_bytes = what, total_files, total_bytes
        self.t0 = self.last = time.monotonic()

    def tick(self, i, done_bytes=0):
        now = time.monotonic()
        if i >= self.total or (i % 500 and now - self.last < 60):
            return
        self.last = now
        rate = done_bytes / (now - self.t0) if now > self.t0 else 0
        left = ""
        if rate and self.total_bytes:
            left = f", about {human_time((self.total_bytes - done_bytes) / rate)} left"
        log.info("%s: %d/%d files (%d%%)%s%s", self.what, i, self.total, i * 100 // max(self.total, 1),
                 f", {human(done_bytes)} at {human(rate)}/s" if done_bytes else "", left)


# ---------------------------------------------------------------- backup

def missing_row(ref, reason):
    return {"app": ref.get("app"), "worksheet": ref.get("ws"), "field": ref.get("field"),
            "record_id": ref.get("rowId"), "title": ref.get("title"), "file_id": ref["fid"],
            "bucket": bucket_of(ref.get("server")), "storage_key": storage_key(ref), "reason": reason}


def log_breakdown(refs):
    if not log.isEnabledFor(logging.DEBUG):
        return
    agg = {}
    for r in refs:
        a = agg.setdefault((str(r.get("app")), str(r.get("ws")), str(r.get("field"))), [0, 0])
        a[0] += 1
        a[1] += bool(r.get("server") and r.get("name") and not r.get("isDel"))
    for (app, ws, field), (n, ok) in sorted(agg.items()):
        log.debug("  %s / %s / %s: %d references, %d with files", app, ws, field, n, ok)


def run_backup(cfg, mode, dry_run):
    base = cfg["BACKUP_DIR"]
    state_path = os.path.join(base, "state.json")
    state = load_state(state_path)
    if mode == "incr" and not state:
        log.warning("no earlier backup found in %s - running a full backup instead", base)
        mode = "full"
    RESULT["mode"] = mode
    resolve(cfg)
    hap = hap_info(cfg)
    RESULT["hap_version"] = hap["version"]
    log.info("HAP %s (%s); MongoDB container %s (%s), app container %s, storage alias %s",
             hap["version"] or "version unknown", hap["image"] or "image unknown", cfg["MONGO_CONTAINER"],
             cfg["MONGO_SHELL"], cfg["APP_CONTAINER"], cfg["MINIO_ALIAS"])

    refs = export_refs(cfg)
    total_refs = len(refs)
    if cfg["INCLUDE_APPS"]:
        refs = [r for r in refs if r.get("appId") in cfg["INCLUDE_APPS"] or r.get("app") in cfg["INCLUDE_APPS"]]
    if cfg["EXCLUDE_APPS"]:
        refs = [r for r in refs if r.get("appId") not in cfg["EXCLUDE_APPS"] and r.get("app") not in cfg["EXCLUDE_APPS"]]
    if len(refs) != total_refs:
        log.info("app filter: %d of %d references kept (INCLUDE_APPS / EXCLUDE_APPS)", len(refs), total_refs)
    log_breakdown(refs)

    missing, candidates = [], []
    for r in refs:
        if not r.get("server") or not r.get("name"):
            missing.append(missing_row(r, "no file details in HAP (mdattachment)"))
        elif r.get("isDel"):
            missing.append(missing_row(r, "file marked deleted in HAP"))
        else:
            candidates.append(r)
    todo = candidates if mode == "full" else [r for r in candidates if ref_key(r) not in state]
    needed = sum(int(r.get("size") or 0) for r in todo)
    log.info("mode=%s%s: %d file references in active records, %d with files, %d without, %d to copy (%s)",
             mode, " (dry run)" if dry_run else "", len(refs), len(candidates), len(missing), len(todo), human(needed))

    if not dry_run:
        os.makedirs(base, mode=0o750, exist_ok=True)
    probe = base if os.path.isdir(base) else os.path.dirname(base.rstrip("/")) or "/"
    du = shutil.disk_usage(probe)
    free_after = (du.free - needed) * 100 / du.total
    log.debug("disk %s: %s free of %s, %.1f%% free after this run (MIN_FREE_PCT=%d)",
              probe, human(du.free), human(du.total), free_after, cfg["MIN_FREE_PCT"])
    if free_after < cfg["MIN_FREE_PCT"]:
        RESULT["error"] = f"not enough disk space: {human(du.free)} free, {human(needed)} needed"
        log.error("not enough disk space on %s: %s free, %s needed, free space would fall below MIN_FREE_PCT=%d%%",
                  probe, human(du.free), human(needed), cfg["MIN_FREE_PCT"])
        return EXIT_LOW_DISK

    folders = Folders()
    if dry_run:
        used, per_ws = {}, {}
        for r in todo:
            rel = folders.worksheet(r)
            name = unique(file_name(r, cfg), used.setdefault(rel, set()))
            per_ws.setdefault(rel, []).append(name)
            log.debug("  would copy %s/%s -> %s/%s", bucket_of(r["server"]), storage_key(r), rel, name)
        for rel, names in sorted(per_ws.items()):
            log.info("  %s/  (%d files)  e.g. %s", rel, len(names), names[0])
        for m in missing[:10]:
            log.warning("  missing: %s / %s / %s (%s): %s", m["app"], m["worksheet"], m["title"], m["file_id"], m["reason"])
        if len(missing) > 10:
            log.warning("  ... and %d more missing", len(missing) - 10)
        log.info("dry run: nothing was written")
        return EXIT_OK

    if mode == "incr" and not todo:
        RESULT["summary"] = f"no new files since the last backup; {len(missing)} without a file in HAP"
        log.info("nothing new to copy - no set written")
        prune(cfg)
        return EXIT_OK

    stamp = dt.datetime.now()
    set_name = f"{mode}-{stamp:%Y-%m-%d_%H%M}"
    if os.path.exists(os.path.join(base, set_name)):           # second run in the same minute
        set_name = f"{mode}-{stamp:%Y-%m-%d_%H%M%S}"
    final_dir = os.path.join(base, set_name)
    if os.path.exists(final_dir):
        raise RuntimeError(f"{final_dir} already exists; run again in a few seconds")
    work = os.path.join(base, f".{set_name}.partial")
    os.makedirs(work, mode=0o750)
    attach_set_log(work)
    log.info("writing %s", work)

    used, manifest, copied_bytes, failures, absent = {}, [], 0, 0, 0
    new_state = {} if mode == "full" else dict(state)
    progress = Progress("progress", len(todo), needed)
    for i, r in enumerate(todo, 1):
        rel = folders.worksheet(r)
        dest_dir = os.path.join(work, rel)
        os.makedirs(dest_dir, exist_ok=True)
        name = unique(file_name(r, cfg), used.setdefault(rel, set()))
        bucket, key = bucket_of(r["server"]), storage_key(r)
        try:
            sha, size, secs = copy_from_storage(cfg, bucket, key, os.path.join(dest_dir, name))
        except CopyError as e:
            where = f"{bucket}/{key} ({r.get('app')} / {r.get('ws')} / {r.get('title')})"
            if NOT_IN_STORAGE.search(str(e)):
                absent += 1
                missing.append(missing_row(r, "file not in HAP storage"))
                log.warning("not in storage [%d/%d]: %s", i, len(todo), where)
                continue
            failures += 1
            missing.append(missing_row(r, f"could not read from storage: {e}"))
            log.warning("copy failed [%d/%d]: %s: %s", i, len(todo), where, e)
            if failures >= ABORT_AFTER and not manifest:
                raise RuntimeError(f"the first {failures} files could not be read from storage, stopping. "
                                   f"Last error: {str(e).rstrip('.')}. Check MINIO_ALIAS and HAP storage with --check")
            continue
        log.debug("copied [%d/%d] %s/%s -> %s/%s, %s, %.2fs, sha256 %s", i, len(todo), bucket, key, rel, name,
                  human(size), secs, sha[:16])
        note = ""
        if r.get("size") and int(r["size"]) != size:
            note = f"size differs from HAP record ({r['size']} bytes)"
            log.debug("  note: %s", note)
        if not clean(r.get("title"), 10):
            note = (note + "; " if note else "") + "empty title, record ID used"
        if cfg["DELIMITER"] in (r.get("title") or ""):
            note = (note + "; " if note else "") + f"title contains '{cfg['DELIMITER']}', use record_id to match"
        if r.get("ctime"):
            try:
                ts = dt.datetime.fromisoformat(r["ctime"].replace("Z", "+00:00")).timestamp()
                os.utime(os.path.join(dest_dir, name), (ts, ts))
            except ValueError:
                pass
        copied_bytes += size
        manifest.append({"app": r.get("app"), "worksheet": r.get("ws"), "field": r.get("field"),
                         "record_id": r.get("rowId"), "title": r.get("title"),
                         "original_name": r.get("oname"), "backup_file": os.path.join(rel, name).replace(os.sep, "/"),
                         "size": size, "sha256": sha, "bucket": bucket, "storage_key": key,
                         "file_id": r["fid"], "app_id": r.get("appId"), "worksheet_id": r.get("wsId"),
                         "field_id": r.get("fieldId"), "uploaded_at": r.get("ctime"), "note": note})
        new_state[ref_key(r)] = {"sha256": sha, "size": size, "set": set_name}
        progress.tick(i, copied_bytes)

    rc = EXIT_COPY_FAILED if failures else EXIT_OK
    if mode == "incr" and not manifest:
        detach_set_log()
        shutil.rmtree(work)
        RESULT["summary"] = (f"no new files copied; {len(missing)} without a file "
                             f"({absent} not in storage, {failures} copy failures)")
        log.info("no new files copied - no set written; %s", RESULT["summary"])
        prune(cfg)
        return rc
    write_csv(os.path.join(work, "manifest.csv"), MANIFEST_COLS, manifest)
    write_csv(os.path.join(work, "missing.csv"), MISSING_COLS, missing)
    summary = {"set": set_name, "version": VERSION, "host": socket.gethostname(), "run_id": RUN_ID,
               "mode": mode, "hap_version": hap["version"], "started": RESULT.get("started", ""),
               "finished": now_iso(), "files_copied": len(manifest), "bytes_copied": copied_bytes,
               "missing": len(missing), "not_in_storage": absent, "copy_failures": failures, "exit_code": rc}
    with open(os.path.join(work, "summary.txt"), "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in summary.items())
    os.rename(work, final_dir)
    save_json(state_path, new_state)
    RESULT["set"] = set_name
    RESULT["summary"] = (f"{len(manifest)} files ({human(copied_bytes)}), {len(missing)} missing"
                         f"{f' ({absent} not in storage)' if absent else ''}, {failures} copy failures")
    log.info("done: %s: %s (see missing.csv)", set_name, RESULT["summary"])
    if failures:
        log.warning("%d files could not be copied; they are listed in %s/missing.csv", failures, final_dir)
    prune(cfg)
    return rc


# ---------------------------------------------------------------- verify and restore

def read_manifest(set_dir):
    with open(os.path.join(set_dir, "manifest.csv"), encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def run_verify(cfg, name):
    set_dir = find_set(cfg, name)
    rows = read_manifest(set_dir)
    log.info("verify %s: %d files", os.path.basename(set_dir), len(rows))
    bad, progress = 0, Progress("verify", len(rows))
    for i, row in enumerate(rows, 1):
        p = os.path.join(set_dir, row["backup_file"])
        if not os.path.exists(p):
            bad += 1
            log.error("missing file: %s", row["backup_file"])
            continue
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != row["sha256"]:
            bad += 1
            log.error("checksum mismatch: %s", row["backup_file"])
        else:
            log.debug("ok %s", row["backup_file"])
        progress.tick(i)
    msg = f"verify {os.path.basename(set_dir)}: {len(rows) - bad}/{len(rows)} files OK"
    (log.error if bad else log.info)(msg)
    RESULT["summary"] = msg
    return EXIT_OK if not bad else EXIT_COPY_FAILED


def run_restore_storage(cfg, name, dry_run):
    """Put files back to their original storage path, only where the object is missing."""
    set_dir = find_set(cfg, name)
    resolve(cfg)
    rows = read_manifest(set_dir)
    log.info("restore-storage%s from %s: %d files in the set", " (dry run)" if dry_run else "",
             os.path.basename(set_dir), len(rows))
    put = skipped = failed = 0
    progress = Progress("restore-storage", len(rows))
    for i, row in enumerate(rows, 1):
        target = f"{row['bucket']}/{row['storage_key']}"
        if object_exists(cfg, row["bucket"], row["storage_key"]):
            skipped += 1
            log.debug("already in storage: %s", target)
        elif dry_run:
            put += 1
            log.info("would restore %s <- %s", target, row["backup_file"])
        else:
            try:
                put_to_storage(cfg, row["bucket"], row["storage_key"], os.path.join(set_dir, row["backup_file"]))
                put += 1
                log.info("restored %s", target)
            except (RuntimeError, OSError) as e:
                failed += 1
                log.error("restore failed %s: %s", target, e)
        progress.tick(i)
    msg = (f"restore-storage{' (dry run)' if dry_run else ''}: {put} {'to restore' if dry_run else 'restored'}, "
           f"{skipped} already in storage (left untouched), {failed} failed")
    (log.error if failed else log.info)(msg)
    RESULT["summary"] = msg
    return EXIT_OK if not failed else EXIT_COPY_FAILED


# ---------------------------------------------------------------- check

CHECK_JS = r"""
var out = { dbs: db.getMongo().getDBNames(), version: db.version(), fields: 0, attachments: 0, samples: [] };
if (out.dbs.indexOf("mdworksheet") >= 0 && out.dbs.indexOf("mdattachment") >= 0) {
  out.fields = db.getSiblingDB("mdworksheet").wscontrols.countDocuments({ type: 14 });
  var A = db.getSiblingDB("mdattachment").attachment;
  out.attachments = A.estimatedDocumentCount();
  A.find({ isDel: { $ne: true }, name: { $exists: true } }).sort({ _id: -1 }).limit(5).forEach(function (a) {
    out.samples.push({ server: a.server, path: a.path, name: a.name });
  });
}
print("HAPBK\t" + JSON.stringify(out));
"""


class Report:
    TAGS = {"ok": ("[ OK ]", logging.INFO), "info": ("[INFO]", logging.INFO),
            "warn": ("[WARN]", logging.WARNING), "fail": ("[FAIL]", logging.ERROR)}

    def __init__(self, out):
        self.out, self.fails, self.warns = out, 0, 0

    def add(self, status, msg, fix=None):
        tag, level = self.TAGS[status]
        self.out(f"{tag} {msg}")
        file_log(level, f"check {tag} {msg}")
        if fix and status in ("warn", "fail"):
            self.out(f"       fix: {fix}")
        self.fails += status == "fail"
        self.warns += status == "warn"


def cron_lines():
    try:
        with open(CRON_FILE, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")
                    and "hap-attachment-backup" in l]
    except OSError:
        return []


def server_timezone():
    try:
        p = run(["timedatectl", "show", "-p", "Timezone", "--value"], timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except RuntimeError:
        pass
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return time.strftime("%Z")


def hap_data_path(cfg):
    """A host folder that holds HAP data (a bind mount of the MongoDB container), if any."""
    if cfg["MONGO_CONTAINER"].lower() == "auto":
        return None
    p = run(["docker", "inspect", "--format", '{{range .Mounts}}{{.Source}}{{"\\n"}}{{end}}', cfg["MONGO_CONTAINER"]],
            timeout=60)
    if p.returncode != 0:
        return None
    for src in p.stdout.splitlines():
        if src.strip() and os.path.isdir(src.strip()) and not src.startswith("/var/lib/docker"):
            return src.strip()
    return None


def check_hap(cfg, rep):
    try:
        rows = docker_ps()
    except RuntimeError as e:
        rep.add("fail", str(e), "start Docker and run as root")
        return
    images = {n: img for n, img, _ in rows}
    for key, pats, svc, label in (("MONGO_CONTAINER", MONGO_IMAGES, "sc", "MongoDB container"),
                                  ("APP_CONTAINER", APP_IMAGES, "app", "HAP app container")):
        val = cfg[key]
        if val.lower() == "auto":
            found = find_containers(rows, pats, svc)
            if len(found) == 1:
                cfg[key] = found[0]
                rep.add("ok", f"{label}: {found[0]} ({images[found[0]]}), found automatically")
                continue
            what = "no HAP container found" if not found else "more than one candidate: " + ", ".join(found)
            rep.add("fail", f"{label}: {what} ({key}=auto)",
                    f"set {key} in backup.conf to one of: {', '.join(images) or 'no containers are running'}")
            return
        if val in images:
            rep.add("ok", f"{label}: {val} ({images[val]})")
        else:
            rep.add("fail", f"{label}: {val} is not running ({key})",
                    f"set {key} in backup.conf to auto or one of: {', '.join(images) or 'no containers are running'}")
            return
    try:
        resolve(cfg)
    except RuntimeError as e:
        rep.add("fail", str(e), "set MONGO_SHELL to mongo or mongosh")
        return
    rep.add("ok", f"MongoDB shell: {cfg['MONGO_SHELL']}")
    try:
        out = mongo_eval(cfg, CHECK_JS, "check query", 300)
        line = next((l for l in out.splitlines() if l.startswith(MARK)), None)
        if line is None:
            raise RuntimeError("no result: " + short(out, 300))
        info = json.loads(line[len(MARK):])
    except (RuntimeError, ValueError) as e:
        rep.add("fail", f"MongoDB query failed: {e}",
                "if MongoDB has a password or runs outside this container, set MONGO_URI")
        return
    lacking = [d for d in HAP_DATABASES if d not in info["dbs"]]
    if lacking:
        rep.add("fail", f"MongoDB {info['version']}: HAP databases missing: {', '.join(lacking)}",
                "MONGO_CONTAINER or MONGO_URI points to the wrong MongoDB")
        return
    rep.add("ok", f"MongoDB {info['version']}: HAP databases found")
    rep.add("info", f"{info['fields']} attachment fields; {info['attachments']} file records in HAP")
    hap = hap_info(cfg)
    if not hap["version"]:
        rep.add("warn", f"HAP version unknown (image {hap['image'] or '?'})", "check APP_CONTAINER")
    elif not hap["version"].startswith("7."):
        rep.add("warn", f"HAP {hap['version']} ({hap['image']}): tested with HAP 7.x",
                "run --dry-run and check the file counts before relying on the backup")
    else:
        rep.add("ok", f"HAP {hap['version']} ({hap['image']})")
    app, alias = cfg["APP_CONTAINER"], cfg["MINIO_ALIAS"]
    p = run(["docker", "exec", app, "mc", "alias", "list", alias], timeout=60)
    if p.returncode != 0:
        rep.add("fail", f"storage alias '{alias}' not found in {app}: {short(p.stderr or p.stdout, 200)}",
                f"list the aliases with: docker exec {app} mc alias list, then set MINIO_ALIAS")
        return
    url = re.search(r"URL\s*:\s*(\S+)", p.stdout)
    rep.add("ok", f"storage alias '{alias}' -> {url.group(1) if url else '?'}")
    p = run(["docker", "exec", app, "mc", "ls", alias], timeout=120)
    if p.returncode != 0:
        rep.add("fail", f"cannot list storage buckets: {short(p.stderr or p.stdout, 200)}",
                "check that HAP storage is running and MINIO_ALIAS is right")
        return
    buckets = [l.split()[-1].rstrip("/") for l in p.stdout.splitlines() if l.strip()]
    samples = info.get("samples") or []
    lacking = sorted({bucket_of(s.get("server")) for s in samples} - set(buckets))
    if lacking:
        rep.add("fail", f"buckets used by HAP files are missing: {', '.join(lacking)} (found: {', '.join(buckets)})",
                "MINIO_ALIAS points to the wrong storage")
        return
    rep.add("ok", f"storage buckets: {', '.join(buckets)}")
    if not samples:
        rep.add("info", "no files in HAP yet; storage read test skipped")
        return
    found = [(bucket_of(s.get("server")), (s.get("path") or "") + (s.get("name") or "")) for s in samples]
    found = [bk for bk in found if object_exists(cfg, *bk)]
    if not found:
        rep.add("fail", f"storage read test: none of the {len(samples)} newest HAP files are in storage",
                "check that MINIO_ALIAS points to HAP's file storage")
        return
    tmpdir = tempfile.mkdtemp(prefix="hapbk-check-")
    try:
        _, size, secs = copy_from_storage(cfg, found[0][0], found[0][1], os.path.join(tmpdir, "sample"))
        status = "ok" if len(found) == len(samples) else "warn"
        rep.add(status, f"storage read test: {len(found)} of {len(samples)} newest HAP files found; "
                        f"read {human(size)} in {secs:.2f}s",
                "files HAP lists but storage lacks are reported in missing.csv" if status == "warn" else None)
    except RuntimeError as e:
        rep.add("fail", f"storage read test failed: {e}", f"check: docker exec {app} mc cat ...")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def check_disk(cfg, rep):
    base = cfg["BACKUP_DIR"]
    probe = base
    while not os.path.isdir(probe):
        probe = os.path.dirname(probe.rstrip("/")) or "/"
    if os.path.isdir(base):
        try:
            with tempfile.NamedTemporaryFile(dir=base, prefix=".write-test-"):
                pass
            rep.add("ok", f"backup folder {base} is writable; {len(list_sets(base))} backup sets")
        except OSError as e:
            rep.add("fail", f"cannot write to backup folder {base}: {e}", "run as root, or set BACKUP_DIR")
    else:
        rep.add("info", f"backup folder {base} does not exist yet; it is created by the first run")
    du = shutil.disk_usage(probe)
    pct = du.free * 100 / du.total
    status = "fail" if pct < cfg["MIN_FREE_PCT"] else "warn" if pct < cfg["MIN_FREE_PCT"] + 10 else "ok"
    rep.add(status, f"backup disk ({probe}): {human(du.free)} free of {human(du.total)} ({pct:.0f}% free)",
            f"free space, lower KEEP_FULL, or set BACKUP_DIR to a bigger disk (runs stop below "
            f"MIN_FREE_PCT={cfg['MIN_FREE_PCT']}%)")
    try:
        hap_dir = hap_data_path(cfg)
    except RuntimeError:
        hap_dir = None
    if hap_dir and os.stat(hap_dir).st_dev == os.stat(probe).st_dev:
        rep.add("warn", f"backups are on the same disk as HAP data ({hap_dir})",
                "copy the backup folder to another server or disk regularly (rsync)")


def check_logging(cfg, rep):
    lf = cfg["LOG_FILE"]
    try:
        with open(lf, "a", encoding="utf-8"):
            pass
        rep.add("ok", f"log file {lf} ({human(os.path.getsize(lf))}), LOG_LEVEL={cfg['LOG_LEVEL']}")
    except OSError as e:
        rep.add("fail", f"cannot write log file {lf}: {e}", "run as root, or set LOG_FILE to a writable path")
    if cfg["LOG_LEVEL"] == "DEBUG":
        rep.add("warn", "LOG_LEVEL=DEBUG logs every file and makes the log large",
                "set LOG_LEVEL=INFO when troubleshooting is done; use --debug for a single run")
    if not logrotate_path():
        rep.add("fail", "logrotate is not installed (required), so the log is never rotated",
                "run install.sh again (it installs logrotate), or: apt install logrotate (Debian/Ubuntu) "
                "or dnf install logrotate (RHEL/CentOS)")
    elif not os.path.exists(ROTATE_FILE):
        rep.add("warn", "log rotation is not set up", "run install.sh again")
    elif not logrotate_schedule():
        rep.add("warn", "logrotate is installed but does not run daily",
                "systemctl enable --now logrotate.timer (or check /etc/cron.daily/logrotate)")
    else:
        rep.add("ok", f"log rotation: {ROTATE_FILE}")


def logrotate_path():
    """The logrotate program, or None. Cron's PATH may lack /usr/sbin, so look there too."""
    for p in (shutil.which("logrotate"), "/usr/sbin/logrotate", "/sbin/logrotate"):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def logrotate_schedule():
    """What runs logrotate daily: 'logrotate.timer', '/etc/cron.daily/logrotate', or None."""
    if shutil.which("systemctl"):
        try:
            p = run(["systemctl", "is-enabled", "logrotate.timer"], timeout=15)
        except RuntimeError:
            p = None
        if p is not None:
            if p.returncode == 0:
                return "logrotate.timer"
            if p.stdout.strip() in ("disabled", "masked"):
                return None  # the timer exists but is off; the cron.daily script then defers to it
    return "/etc/cron.daily/logrotate" if os.path.exists("/etc/cron.daily/logrotate") else None


def check_schedule(cfg, rep):
    lines = cron_lines()
    if not lines:
        rep.add("warn", f"no schedule ({CRON_FILE} not found)",
                "run install.sh without --no-cron, or add a cron entry yourself")
    for line in lines:
        parts = line.split()
        script = next((x for x in parts if x.endswith(("hap-attachment-backup.sh", "hap-attachment-backup"))), "")
        when = " ".join(parts[:5])
        args = " ".join(x for x in parts[parts.index(script) + 1:] if not x.startswith((">", "2>"))) if script else ""
        if script and os.access(script, os.X_OK):
            rep.add("ok", f"schedule: {when}  {args}")
        else:
            rep.add("fail", f"schedule points to a missing script: {line}", "run install.sh again")
    active = None
    for svc in ("cron", "crond"):
        try:
            p = run(["systemctl", "is-active", svc], timeout=20)
        except RuntimeError:
            break
        if p.stdout.strip() == "active":
            active = svc
            break
    if active:
        rep.add("ok", f"cron service '{active}' is running")
    elif lines:
        rep.add("warn", "cron service is not running (or systemd is not used)",
                "start it: systemctl enable --now cron (Debian/Ubuntu) or crond (RHEL/CentOS)")
    rep.add("info", f"server time zone: {server_timezone()}, now {dt.datetime.now():%Y-%m-%d %H:%M}; "
                    f"cron times use this zone")


def check_last_run(cfg, rep):
    holder = lock_holder()
    if holder:
        rep.add("info", f"a backup or restore is running now ({holder})")
    last = read_last_run(cfg["BACKUP_DIR"])
    if not last:
        rep.add("warn", "no backup has run yet", "run: sudo hap-attachment-backup --full")
        return
    try:
        age = time.time() - dt.datetime.fromisoformat(last["finished"]).timestamp()
    except (KeyError, ValueError):
        age = 0
    what = f"last run {last.get('finished', '?')} ({last.get('mode', '?')}): {last.get('result', '?')}"
    if last.get("exit_code") != EXIT_OK:
        rep.add("warn", f"{what} - {last.get('message', '')}", "see the run: sudo hap-attachment-backup --log")
    elif age > 2 * 86400:
        rep.add("warn", f"{what}, {human_time(age)} ago", "check the schedule and the log")
    else:
        rep.add("ok", f"{what}, {last.get('message', '')}")


def run_check(cfg, conf_path, conf_warns, out=print):
    rep = Report(out)
    out(f"HAP attachment backup {VERSION} - system check on {socket.gethostname()}, "
        f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    out(f"settings: {conf_path}")
    out("")
    for w in conf_warns:
        rep.add("warn", f"backup.conf {w}", "correct the setting name in backup.conf")
    if os.geteuid() == 0:
        rep.add("ok", "running as root")
    else:
        rep.add("warn", f"not running as root (user {user_name()})", "run with sudo")
    rep.add("ok", f"Python {platform.python_version()}")
    try:
        p = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
        if p.returncode == 0:
            rep.add("ok", f"Docker {p.stdout.strip()}")
            check_hap(cfg, rep)
        else:
            rep.add("fail", "Docker is not reachable: " + short(p.stderr, 200),
                    "start Docker (systemctl start docker) and run as root")
    except RuntimeError as e:
        rep.add("fail", str(e), "install Docker or add it to PATH")
    check_disk(cfg, rep)
    check_logging(cfg, rep)
    check_schedule(cfg, rep)
    check_last_run(cfg, rep)
    out("")
    out(f"result: {rep.fails} failed, {rep.warns} warnings" + ("" if rep.fails else " - ready to back up"))
    RESULT["summary"] = f"{rep.fails} failed, {rep.warns} warnings"
    return EXIT_FATAL if rep.fails else EXIT_OK


# ---------------------------------------------------------------- status, log, support bundle

def run_status(cfg, out=print):
    base = cfg["BACKUP_DIR"]
    out(f"HAP attachment backup {VERSION} on {socket.gethostname()}, {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    if os.path.isdir(base):
        du = shutil.disk_usage(base)
        out(f"backup folder : {base} ({human(du.free)} free of {human(du.total)}, {du.free * 100 // du.total}% free)")
    else:
        out(f"backup folder : {base} (does not exist yet)")
    try:
        tracked = len(load_state(os.path.join(base, "state.json")))
    except (OSError, ValueError):
        tracked = "unreadable"
    out(f"files tracked : {tracked} (state.json; nightly runs copy only files not in it)")
    holder = lock_holder()
    out("running now   : " + (f"yes, {holder}" if holder else "no"))
    last = read_last_run(base)
    if last:
        out(f"last run      : {last.get('finished')} {last.get('mode')} -> {last.get('result')} "
            f"(exit {last.get('exit_code')}){', set ' + last['set'] if last.get('set') else ''}")
        if last.get("message"):
            out(f"                {last['message']}")
    else:
        out("last run      : none yet")
    lines = cron_lines()
    out("schedule      : " + (CRON_FILE if lines else "not installed"))
    for line in lines:
        out("                " + line)
    sets = list_sets(base)
    out(f"backup sets   : {len(sets)}")
    if sets:
        out(f"  {'SET':<22} {'FILES':>7} {'SIZE':>10} {'MISSING':>8} {'FAILED':>7}  FINISHED")
        for s in sets:
            sm = read_summary(os.path.join(base, s, "summary.txt"))
            size = human(int(sm["bytes_copied"])) if sm.get("bytes_copied", "").isdigit() else "?"
            out(f"  {s:<22} {sm.get('files_copied', '?'):>7} {size:>10} {sm.get('missing', '?'):>8} "
                f"{sm.get('copy_failures', '?'):>7}  {sm.get('finished', '?')}")
    partial = sorted(d for d in os.listdir(base) if d.endswith(".partial")) if os.path.isdir(base) else []
    if partial:
        out("unfinished    : " + ", ".join(partial) + " (interrupted runs; removed after one day)")
    lf = cfg["LOG_FILE"]
    out(f"log file      : {lf}" + (f" ({human(os.path.getsize(lf))})" if os.path.exists(lf) else " (not created yet)"))
    return EXIT_OK


def tail_lines(path, max_bytes=8 << 20):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    lines = data.splitlines()
    return lines[1:] if size > max_bytes else lines


def last_backup_run(lines):
    """Run ID of the last real backup run (not a dry run) in the log."""
    run_id = None
    for line in lines:
        m = LOG_LINE_RE.match(line)
        if m and m.group(3).startswith("start: backup") and "dry run" not in m.group(3):
            run_id = m.group(2)
    return run_id


def run_show_log(cfg, count):
    lf = cfg["LOG_FILE"]
    if not os.path.exists(lf):
        print(f"no log file yet: {lf}")
        return EXIT_OK
    lines = tail_lines(lf)
    if count:
        for line in lines[-count:]:
            print(line)
        return EXIT_OK
    run_id = last_backup_run(lines)
    if not run_id:
        print(f"no backup run found in the last part of {lf}; showing the last 50 lines")
        lines, run_id = lines[-50:], None
    for line in lines:
        m = LOG_LINE_RE.match(line)
        if run_id is None or (m and m.group(2) == run_id):
            print(line)
    return EXIT_OK


def masked_conf_text(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            key = line.split("=", 1)[0].strip().upper()
            if "=" in line and not line.lstrip().startswith("#") and re.search(r"PASS|SECRET|TOKEN|KEY$", key):
                line = f"{line.split('=', 1)[0]}=***\n"
            out.append(mask(line))
    return "".join(out)


SYSTEM_CMDS = [
    ["uname", "-a"], ["cat", "/etc/os-release"], ["date"], ["uptime"], ["timedatectl"],
    ["df", "-h"], ["free", "-m"], ["docker", "version"],
    ["docker", "ps", "-a", "--format", "table {{.Names}}\t{{.Image}}\t{{.Status}}"],
    ["ls", "-la", "/etc/cron.d"],
]


def run_bundle(cfg, conf_path, conf_warns, out_dir):
    files = {}
    buf = []

    def both(line):
        print(line)
        buf.append(line)

    run_check(dict(cfg), conf_path, conf_warns, out=both)
    files["check.txt"] = "\n".join(buf) + "\n"
    st = []
    run_status(cfg, out=st.append)
    files["status.txt"] = "\n".join(st) + "\n"
    sysinfo = [f"# hap-attachment-backup {VERSION}, python {sys.version.split()[0]}, user {user_name()}"]
    for cmd in SYSTEM_CMDS:
        sysinfo.append("\n$ " + " ".join(cmd))
        try:
            p = run(cmd, timeout=30)
            sysinfo.append((p.stdout + p.stderr).rstrip())
        except RuntimeError as e:
            sysinfo.append(f"({e})")
    files["system.txt"] = "\n".join(sysinfo) + "\n"
    try:
        files["backup.conf"] = masked_conf_text(conf_path)
    except OSError as e:
        files["backup.conf"] = f"(cannot read {conf_path}: {e})\n"
    for path, name in ((CRON_FILE, "cron.txt"), (ROTATE_FILE, "logrotate.txt"),
                       (os.path.join(cfg["BACKUP_DIR"], "last_run.json"), "last_run.json")):
        try:
            with open(path, encoding="utf-8") as f:
                files[name] = f.read()
        except OSError as e:
            files[name] = f"({e})\n"
    try:
        files["log-tail.txt"] = "\n".join(mask(l) for l in tail_lines(cfg["LOG_FILE"])[-5000:]) + "\n"
    except OSError as e:
        files["log-tail.txt"] = f"({e})\n"
    for s in list_sets(cfg["BACKUP_DIR"])[-3:]:
        for fn in ("summary.txt", "run.log"):
            try:
                text = "\n".join(tail_lines(os.path.join(cfg["BACKUP_DIR"], s, fn))[-20000:]) + "\n"
                files[f"sets/{s}/{fn}"] = mask(text)
            except OSError:
                pass
    stamp = f"{dt.datetime.now():%Y%m%d-%H%M%S}"
    name = f"hap-attachment-backup-support-{socket.gethostname()}-{stamp}"
    path = os.path.join(out_dir or "/tmp", name + ".tar.gz")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for fn, text in files.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{name}/{fn}")
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(data))
    print("")
    print(f"support bundle: {path} ({human(os.path.getsize(path))})")
    print("It holds the check result, status, settings (passwords hidden), recent log lines and system details.")
    print("It holds no attachment files. Log lines can include app, worksheet and record names.")
    log.info("support bundle written: %s", path)
    RESULT["summary"] = f"support bundle {path}"
    return EXIT_OK


# ---------------------------------------------------------------- main

def parse_args():
    ap = argparse.ArgumentParser(
        prog="hap-attachment-backup",
        description="Back up the files in HAP worksheet attachment fields.",
        epilog="Exit codes: 0 OK, 1 error, 2 files not copied/verified, 3 another run in progress, 4 low disk space.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="copy every attachment into a new full set")
    g.add_argument("--incremental", action="store_true", help="copy only files not in earlier sets (default)")
    g.add_argument("--verify", nargs="?", const="latest", metavar="SET",
                   help="re-check a backup set against its manifest.csv (default: newest set)")
    g.add_argument("--restore-storage", metavar="SET",
                   help="put files from a backup set back into HAP storage where they are missing")
    g.add_argument("--check", action="store_true", help="test settings, Docker, MongoDB, storage, disk, schedule")
    g.add_argument("--status", action="store_true", help="show backup sets, last run and schedule")
    g.add_argument("--log", nargs="?", const=0, type=int, metavar="N",
                   help="show the log of the last backup run, or the last N log lines")
    g.add_argument("--support-bundle", nargs="?", const="/tmp", metavar="DIR",
                   help="pack check, status, settings and logs into a .tar.gz for support (default /tmp)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    ap.add_argument("--debug", action="store_true", help="log every command and file for this run")
    ap.add_argument("--quiet", action="store_true", help="print only warnings and errors (the log is complete)")
    ap.add_argument("--conf", default=os.path.join(HERE, "backup.conf"), help="settings file")
    ap.add_argument("--version", action="version", version=f"hap-attachment-backup {VERSION}")
    return ap.parse_args()


def main():
    a = parse_args()
    if a.check:
        cmd = "check"
    elif a.status:
        cmd = "status"
    elif a.log is not None:
        cmd = "log"
    elif a.support_bundle:
        cmd = "bundle"
    elif a.verify:
        cmd = "verify"
    elif a.restore_storage:
        cmd = "restore"
    else:
        cmd = "backup"

    cfg, warns, conf_err = None, [], None
    try:
        cfg, warns = load_conf(a.conf)
    except (ConfigError, OSError, UnicodeDecodeError) as e:
        conf_err = str(e)
    level = "DEBUG" if a.debug else (cfg["LOG_LEVEL"] if cfg else "INFO")
    log_file = None if cmd in ("status", "log") else (cfg or DEFAULTS)["LOG_FILE"]
    console = "WARNING" if a.quiet or (cmd in ("check", "bundle") and not a.debug) else "DEBUG"
    setup_logging(level, log_file, console)
    if conf_err:
        log.error("settings: %s", conf_err)
        return EXIT_FATAL
    if cmd == "status":
        return run_status(cfg)
    if cmd == "log":
        return run_show_log(cfg, a.log)

    mode = "full" if a.full else "incr"
    what = {"backup": f"backup mode={'full' if a.full else 'incremental'}", "verify": f"verify {a.verify}",
            "restore": f"restore-storage {a.restore_storage}", "check": "check",
            "bundle": "support-bundle"}[cmd] + (" (dry run)" if a.dry_run and cmd in ("backup", "restore") else "")
    started, t0 = now_iso(), time.monotonic()
    RESULT.update({"run_id": RUN_ID, "started": started, "mode": mode if cmd == "backup" else cmd})
    log.info("start: %s - version %s, host %s, user %s, pid %d, %s", what, VERSION, socket.gethostname(),
             user_name(), os.getpid(), "terminal" if sys.stdin.isatty() else "no terminal (cron or script)")
    log.debug("python %s on %s; settings %s: %s", platform.python_version(), platform.platform(), a.conf,
              json.dumps(cfg_for_log(cfg), ensure_ascii=False))
    for w in warns:
        log.warning("backup.conf %s", w)

    lock_fd = None
    if cmd in ("backup", "restore") and not a.dry_run:
        lock_fd, holder = acquire_lock()
        if lock_fd is None:
            log.error("another backup or restore is already running (%s); nothing done", holder)
            return EXIT_BUSY

    rc = EXIT_FATAL
    try:
        if cmd == "check":
            rc = run_check(cfg, a.conf, warns)
        elif cmd == "bundle":
            rc = run_bundle(cfg, a.conf, warns, a.support_bundle)
        elif cmd == "verify":
            rc = run_verify(cfg, a.verify)
        elif cmd == "restore":
            rc = run_restore_storage(cfg, a.restore_storage, a.dry_run)
        else:
            rc = run_backup(cfg, mode, a.dry_run)
    except RuntimeError as e:
        RESULT["error"] = str(e)
        log.error("%s", e)
        log.debug("traceback:\n%s", traceback.format_exc().rstrip())
        if not a.debug:
            log.error("for details run the same command with --debug, or see: sudo hap-attachment-backup --check")
    except KeyboardInterrupt:
        RESULT["error"] = "interrupted"
        log.error("interrupted")
    except Exception as e:  # noqa: BLE001 - every failure must reach the log
        RESULT["error"] = f"{type(e).__name__}: {e}"
        log.error("unexpected error, please send the log or a support bundle to support:\n%s",
                  traceback.format_exc().rstrip())
    finally:
        log.info("finished: %s - exit %d (%s) in %s", what, rc, EXIT_NAMES.get(rc, "?"),
                 human_time(time.monotonic() - t0))
        if cmd == "backup" and not a.dry_run and os.path.isdir(cfg["BACKUP_DIR"]):
            try:
                save_json(os.path.join(cfg["BACKUP_DIR"], "last_run.json"), {
                    "run_id": RUN_ID, "mode": RESULT.get("mode"), "started": started, "finished": now_iso(),
                    "duration_s": round(time.monotonic() - t0, 1), "exit_code": rc,
                    "result": EXIT_NAMES.get(rc, "?"), "set": RESULT.get("set", ""),
                    "message": RESULT.get("error") or RESULT.get("summary", ""), "version": VERSION,
                    "hap_version": RESULT.get("hap_version", ""), "host": socket.gethostname()}, indent=2)
            except OSError as e:
                log.warning("cannot write last_run.json: %s", e)
        detach_set_log()
        if lock_fd is not None:
            os.close(lock_fd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
