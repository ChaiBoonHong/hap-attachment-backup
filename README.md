# HAP Attachment Backup

Backs up the files attached to HAP worksheet records on a Linux server that runs HAP in Docker.

HAP's own app backup saves apps, worksheets, workflows, views and records, but not the files in attachment fields. This tool copies those files to a folder on the server every night and names each one so that HAP's **Import Attachments** can put it back on its record:

```
<record title>_<original file name>.<ext>
```

- Every file type in every attachment field, from records that are not in the recycle bin.
- Full copy every Sunday at 02:00, new files only Monday to Saturday at 02:00, or any time by hand.
- Reads HAP only. The optional storage restore writes only files that are missing.
- Every run is logged. `--check` tests the server, `--debug` logs every command and file, and `--support-bundle` packs everything support needs.
- Python 3.8+ standard library only. Nothing to install in the HAP containers.

**Step-by-step runbook:** https://chaiboonhong.github.io/hap-attachment-backup/ (install, check, logs and debugging, troubleshooting, restore, hand-over checklist).

## Install

On the HAP server, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/ChaiBoonHong/hap-attachment-backup/main/get.sh | sudo bash
```

This downloads the latest release, installs it to `/opt/hap-attachment-backup`, adds the `hap-attachment-backup` command, sets up the nightly schedule and log rotation, and runs `--check`.

Options go after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/ChaiBoonHong/hap-attachment-backup/main/get.sh | sudo bash -s -- --version v1.1.0   # a given release
curl -fsSL https://raw.githubusercontent.com/ChaiBoonHong/hap-attachment-backup/main/get.sh | sudo bash -s -- --no-cron          # no schedule
curl -fsSL https://raw.githubusercontent.com/ChaiBoonHong/hap-attachment-backup/main/get.sh | sudo bash -s -- /srv/hap-backup    # another folder
```

Without the one-liner (for example on a server without internet access, copy the release file over first):

```bash
curl -fsSLO https://github.com/ChaiBoonHong/hap-attachment-backup/archive/refs/tags/v1.1.0.tar.gz
tar xzf v1.1.0.tar.gz
sudo bash hap-attachment-backup-1.1.0/install.sh
```

Or with git: `git clone https://github.com/ChaiBoonHong/hap-attachment-backup.git && sudo bash hap-attachment-backup/install.sh`

### Requirements

- Linux server running **HAP standalone in Docker** (tested with HAP 7.4.5 on Debian 13), with root access.
- `docker`, `python3` 3.8 or later, `bash`, `curl` or `wget`.
- The HAP MongoDB container (`nocoly/sc` or `mingdaoyun-sc`) with the `mongo` or `mongosh` shell, and the HAP app container (`nocoly/hap` or `mingdaoyun-community`) with the `mc` storage client. Both are standard in HAP standalone; `--check` confirms them.
- Disk space for your attachments several times over (one full copy per week is kept for 4 weeks by default).

## First backup

```bash
sudo hap-attachment-backup --check      # test the server; fix anything marked FAIL
sudo hap-attachment-backup --dry-run    # what would be copied, and what has no file
sudo hap-attachment-backup --full       # first full copy
sudo hap-attachment-backup --verify     # re-check the newest set against its checksums
sudo hap-attachment-backup --status     # backup sets, last run, schedule
```

A healthy `--check`:

```
HAP attachment backup 1.1.0 - system check on hap-prod-01, 2026-11-13 10:33:24
settings: /opt/hap-attachment-backup/backup.conf

[ OK ] running as root
[ OK ] Python 3.13.5
[ OK ] Docker 29.8.2
[ OK ] MongoDB container: script-sc-1 (nocoly/sc:3.2.0), found automatically
[ OK ] HAP app container: script-app-1 (nocoly/hap:7.4.5), found automatically
[ OK ] MongoDB shell: mongo
[ OK ] MongoDB 4.4.30: HAP databases found
[INFO] 15 attachment fields; 2480 file records in HAP
[ OK ] HAP 7.4.5 (nocoly/hap:7.4.5)
[ OK ] storage alias 'file' -> http://sc:9010
[ OK ] storage buckets: mdmedia, mdoc, mdpic, mdpub
[ OK ] storage read test: 5 of 5 newest HAP files found; read 104.5 KB in 0.07s
[ OK ] backup folder /data/hap-attachment-backup is writable; 3 backup sets
[ OK ] backup disk (/data/hap-attachment-backup): 128.3 GB free of 195.8 GB (65% free)
[WARN] backups are on the same disk as HAP data (/data/hap/script/volume/data)
       fix: copy the backup folder to another server or disk regularly (rsync)
[ OK ] log file /var/log/hap-attachment-backup.log (19.5 KB), LOG_LEVEL=INFO
[ OK ] log rotation: /etc/logrotate.d/hap-attachment-backup
[ OK ] schedule: 0 2 * * 0  --full --quiet
[ OK ] schedule: 0 2 * * 1-6  --incremental --quiet
[ OK ] cron service 'cron' is running
[INFO] server time zone: Asia/Singapore, now 2026-11-13 10:33; cron times use this zone
[ OK ] last run 2026-11-13T02:00:41+08:00 (incr): OK, 12 files (8.4 MB), 3 missing, 0 copy failures

result: 0 failed, 1 warnings - ready to back up
```

## Commands

| Command | What it does |
|---|---|
| `hap-attachment-backup --check` | Tests settings, Docker, MongoDB, storage, a sample file read, disk, log, schedule and the last run. Each problem comes with a fix. |
| `hap-attachment-backup --dry-run` | Shows what would be copied and which attachments have no file. Writes nothing. |
| `hap-attachment-backup --full` | Copies every attachment into a new `full-YYYY-MM-DD_HHMM` set. |
| `hap-attachment-backup` or `--incremental` | Copies only files not in earlier sets into `incr-YYYY-MM-DD_HHMM`. Runs a full copy if there is none yet; writes no set when nothing is new. |
| `hap-attachment-backup --verify [SET]` | Re-checks every file in a set against `manifest.csv` (default: newest set). |
| `hap-attachment-backup --restore-storage SET [--dry-run]` | Puts files back into HAP storage at their original path, only where they are missing. Never overwrites. |
| `hap-attachment-backup --status` | Backup sets with file counts and sizes, last run, whether a run is active, schedule. |
| `hap-attachment-backup --log [N]` | The log of the last backup run, or the last N log lines. |
| `hap-attachment-backup --support-bundle [DIR]` | Packs check, status, settings (passwords hidden), recent log and system details into a `.tar.gz` (default `/tmp`). |

Add `--debug` to any command to log every command and every file for that run, or `--quiet` to print only warnings and errors (the log stays complete). Run as root (`sudo`).

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Finished. Attachments with no file in HAP are listed in `missing.csv` but are not a failure. |
| `1` | Error: settings, Docker, MongoDB, or storage unreadable. The log says which. |
| `2` | Some files could not be copied or failed verification. |
| `3` | Another backup or restore is still running. |
| `4` | Not enough disk space. |

## Logs and debugging

| Where | What |
|---|---|
| `/var/log/hap-attachment-backup.log` | Every run, appended; rotated weekly, 8 weeks kept. Set with `LOG_FILE`. |
| `<set>/run.log` | The full log of the run that wrote that set, kept with the files. |
| `<backup folder>/last_run.json` | Result of the last backup run: time, mode, exit code, set, message. Easy to read from monitoring. |
| `hap-attachment-backup --log` | Prints the last backup run from the log. |

Each log line has a time, a level and a run ID, so the lines of one run can be picked out with `grep`:

```
2026-11-13 02:00:01 [INFO] [20261113-020001-24817] start: backup mode=incremental - version 1.1.0, host hap-prod-01, user root, pid 24817, no terminal (cron or script)
2026-11-13 02:00:01 [INFO] [20261113-020001-24817] HAP 7.4.5 (nocoly/hap:7.4.5); MongoDB container script-sc-1 (mongo), app container script-app-1, storage alias file
2026-11-13 02:00:02 [INFO] [20261113-020001-24817] MongoDB export: 2480 file references in 1.4s
2026-11-13 02:00:02 [INFO] [20261113-020001-24817] mode=incr: 2480 file references in active records, 2477 with files, 3 without, 12 to copy (8.4 MB)
2026-11-13 02:00:02 [INFO] [20261113-020001-24817] writing /data/hap-attachment-backup/.incr-2026-11-13_0200.partial
2026-11-13 02:00:04 [INFO] [20261113-020001-24817] done: incr-2026-11-13_0200: 12 files (8.4 MB), 3 missing, 0 copy failures (see missing.csv)
2026-11-13 02:00:04 [INFO] [20261113-020001-24817] finished: backup mode=incremental - exit 0 (OK) in 3s
```

**Levels.** `LOG_LEVEL=INFO` (default) logs each step, progress every 500 files or 60 seconds, warnings and errors. `DEBUG` also logs every Docker command with its exit code, time and error output, the settings in use (passwords hidden), counts per app, worksheet and field, and every file copied with its size, time and checksum. Use `--debug` for one run rather than leaving `LOG_LEVEL=DEBUG` on: the log grows with every file.

```bash
sudo hap-attachment-backup --dry-run --debug     # see how attachments are found, without copying
sudo hap-attachment-backup --check --debug       # see every command the check runs
sudo hap-attachment-backup --log                 # what happened in the last nightly run
grep '\[ERROR\]\|\[WARNING\]' /var/log/hap-attachment-backup.log | tail -20
```

**Getting help.** Run `sudo hap-attachment-backup --support-bundle` and send the `.tar.gz` it prints. It holds the check result, status, settings with passwords hidden, the last 5000 log lines, the logs of the last three sets and system details (OS, Docker, disk, containers). It holds no attachment files, but log lines can include app, worksheet and record names.

**Safety checks built in.**
- A second backup cannot start while one runs (exit code 3); the log names the running process.
- A run stops with an error when the first 10 files cannot be read, instead of writing an empty set.
- A set is written to a hidden `.<set>.partial` folder and renamed only when complete; unfinished folders are removed after a day.
- Full sets where most files failed do not count toward `KEEP_FULL`, so failed nights never cause good full copies to be deleted.
- Files HAP lists but storage does not have are reported as `file not in HAP storage` in `missing.csv`, not as failures.

## Troubleshooting

Start with `sudo hap-attachment-backup --check`. Common messages:

| Message | Cause | Fix |
|---|---|---|
| `MONGO_CONTAINER=auto: no HAP container found` | Containers have unusual names or images | Set `MONGO_CONTAINER` and `APP_CONTAINER` to names from `docker ps` |
| `more than one candidate` | Several HAP stacks on one server | Set the container names for the stack to back up |
| `no mongo or mongosh shell in container` | Wrong container | Point `MONGO_CONTAINER` at the HAP `sc` container |
| `MongoDB query ... failed: ... requires authentication` | MongoDB has a password or is external | Set `MONGO_URI` |
| `HAP databases missing` | Connected to the wrong MongoDB | Check `MONGO_CONTAINER` / `MONGO_URI` |
| `storage alias 'file' not found` | Different `mc` alias name | `docker exec <app> mc alias list`, then set `MINIO_ALIAS` |
| `the first 10 files could not be read from storage, stopping` | Storage down or wrong alias | Run `--check`; fix `MINIO_ALIAS` or start HAP storage |
| `not in storage [...]` | HAP lists the file but storage does not have it | Nothing to back up; listed in `missing.csv` |
| `another backup or restore is already running` | The previous run is still going | Wait; `--status` shows the running process |
| `not enough disk space` | Free space would drop below `MIN_FREE_PCT` | Free space, lower `KEEP_FULL`, or move `BACKUP_DIR` |
| `backup.conf line N: unknown setting` | Typo in a setting name | Correct it; the line is ignored until then |
| `python3 not found` / `Python 3.8 or later is required` | Old or missing Python | `apt install python3` or `dnf install python3` |
| No run at 02:00 | Cron stopped or schedule missing | `--check` shows both; `systemctl enable --now cron` (or `crond`) |

## Settings

`/opt/hap-attachment-backup/backup.conf`, read by every run (no restart). Run `--check` after changing it. New defaults from an update are in `backup.conf.example`.

| Setting | Default | Meaning |
|---|---|---|
| `BACKUP_DIR` | `/data/hap-attachment-backup` | Where backup sets are written. A separate disk is best. |
| `KEEP_FULL` | `4` | Good full copies to keep (4 = about a month) |
| `KEEP_INCR_DAYS` | `14` | Days of nightly sets to keep |
| `MIN_FREE_PCT` | `10` | Skip the run if free space would fall below this percentage |
| `MONGO_CONTAINER` | `auto` | HAP MongoDB container; `auto` finds `nocoly/sc` or `mingdaoyun-sc` |
| `APP_CONTAINER` | `auto` | HAP app container with `mc`; `auto` finds `nocoly/hap` or `mingdaoyun-community` |
| `MONGO_SHELL` | `auto` | `mongo` or `mongosh` |
| `MONGO_URI` | empty | Only for MongoDB with a password or outside the container |
| `MINIO_ALIAS` | `file` | Storage alias of `mc` in the app container |
| `DELIMITER` | `_` | Between title and file name; use the same in Import Attachments |
| `TITLE_MAX_CHARS`, `NAME_MAX_CHARS` | `100`, `150` | Longer titles and names are shortened |
| `INCLUDE_APPS`, `EXCLUDE_APPS` | empty | Comma-separated app IDs or names; empty means all apps |
| `LOG_FILE` | `/var/log/hap-attachment-backup.log` | Log file |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `EXPORT_TIMEOUT`, `COPY_TIMEOUT` | `3600` | Seconds for the MongoDB export, and for copying one file |

## Schedule

`/etc/cron.d/hap-attachment-backup`, in the server's time zone (`timedatectl`):

```
0 2 * * 0   root /opt/hap-attachment-backup/hap-attachment-backup.sh --full --quiet >/dev/null 2>&1
0 2 * * 1-6 root /opt/hap-attachment-backup/hap-attachment-backup.sh --incremental --quiet >/dev/null 2>&1
```

The first two fields are minute and hour: `30 3 * * 0` is Sunday 03:30. Output goes to the log file, not to cron mail.

## Backup layout

```
/data/hap-attachment-backup/
  state.json                    # files already backed up (used by nightly runs)
  last_run.json                 # result of the last backup run
  full-2026-11-15_0200/         # Sunday full copy
    <App>/<Worksheet>/<Title>_<Original name>.<ext>
    manifest.csv                # every file: record ID, title, original name, storage path, SHA-256
    missing.csv                 # attachments that could not be backed up, with the reason
    summary.txt                 # counts, version, HAP version, exit code
    run.log                     # log of this run
  incr-2026-11-16_0200/         # nightly: only files not backed up before
```

To restore everything as of a given night, use the latest full set before that night plus every nightly set after it. Both CSV files open in Excel.

**File names.** `<title><DELIMITER><original name>.<ext>`, the extension taken from the stored file. `\ / : * ? " < > |` and control characters become `-`. An empty title is replaced by the record ID. A name already used in the same worksheet folder gets ` (2)`, ` (3)`. The `note` column in `manifest.csv` flags empty titles and titles that contain the delimiter.

## Restore

### A. Import Attachments in HAP (records exist, files are missing)

1. Copy the worksheet folder `<set>/<App>/<Worksheet>/` to your PC (`scp`, or `tar` it first).
2. In HAP, open the worksheet, then **Import** > **Import Attachments**.
3. Upload the files: up to 100 files and 2 GB per upload.
4. **File Name Matching Field**: the worksheet's title field. **Delimiter**: `_`.
5. **Target field**: the attachment field (the `field` column in `manifest.csv`).
6. **Write mode**: Append adds files; Overwrite replaces the field's files and cannot be undone.

Titles must be unique for files to land on the right record. If two records share a title, or a title contains `_`, use `record_id` in `manifest.csv`.

### B. Put files back into HAP storage (storage lost, records intact)

```bash
sudo hap-attachment-backup --restore-storage full-2026-11-15_0200 --dry-run
sudo hap-attachment-backup --restore-storage full-2026-11-15_0200
```

Run it for the full set, then for each later nightly set. Records show their files again with the original file IDs. Files that already exist are left untouched.

## Copy backups off the server

The default backup folder is on the server's own disk. To survive losing that disk, copy it elsewhere on a schedule, for example:

```bash
sudo rsync -a /data/hap-attachment-backup/ backup-user@other-host:/backups/hap-attachments/
```

## Update and uninstall

Update: run the install command again. Your `backup.conf`, backups and log are kept.

```bash
curl -fsSL https://raw.githubusercontent.com/ChaiBoonHong/hap-attachment-backup/main/get.sh | sudo bash
```

Uninstall (removes the script, command, schedule and log rotation; keeps backups and the log):

```bash
sudo bash /opt/hap-attachment-backup/install.sh --uninstall
```

## What it changes on the server

| Path | Purpose |
|---|---|
| `/opt/hap-attachment-backup/` | Script, `backup.conf` (root only), README |
| `/usr/local/bin/hap-attachment-backup` | Command (link to the script) |
| `/etc/cron.d/hap-attachment-backup` | Schedule |
| `/etc/logrotate.d/hap-attachment-backup` | Log rotation |
| `/var/log/hap-attachment-backup.log` | Log |
| `/data/hap-attachment-backup/` | Backups |

MongoDB and storage are only read. `--restore-storage` writes to storage, and only files that are missing. Run `--dry-run` after each HAP upgrade: an upgrade can change how attachments are stored.

## License

MIT. See [LICENSE](LICENSE).
