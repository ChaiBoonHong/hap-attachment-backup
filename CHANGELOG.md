# Changelog

## 1.4.0 - 2026-11-25

- `--app-backups [DAYS]`: HAP's own app backups (`.mdy`) of every app on one screen, instead of opening Backup & Restore app by app. One line per app: Regular Backup schedule, newest backup, backup files kept, a mark per day for the last 7 days (or DAYS, up to 60), and a status: `OK`, `OVERDUE`, `FAILED`, `STUCK`, `NO BACKUP` or `no schedule`. `--app NAME` for one app; `--quiet` prints only apps that need attention. Read from MongoDB only.
- Exit code `5` when an app backup failed, is overdue or is stuck; problems are also written to the log as warnings.
- `--support-bundle` includes the app backup report for the last 14 days.

## 1.3.0 - 2026-11-20

- `--find-removed [SET]`: finds attachments that were removed from records since they were backed up, and copies just those files into `BACKUP_DIR/restore-<date>/<App>/<Worksheet>/<Field>/` with a `removed.csv` list and `HOW-TO-IMPORT.txt`, ready for HAP Import Attachments (write mode Append). Filters: `--app`, `--worksheet`, `--field`, `--record`. Records in the recycle bin or deleted are listed, not staged.
- `RECORD_FOLDERS=yes` saves each record's files in a folder named after the record title: `<App>/<Worksheet>/<title>/<title>_<file name>`. Default `no` (unchanged layout).
- `--restore-storage` now explains when nothing was restored because every file is still in storage (removing an attachment from a record leaves the file in storage).
- `--status` lists restore folders that are still on disk.
- Files put back by Import Attachments (HAP names them `<title>_<original name>`) are recognised: `--find-removed` does not report them again, and backups no longer name them `<title>_<title>_...`.

## 1.2.0 - 2026-11-13

- `logrotate` is now required. `install.sh` installs it with the system package manager (apt-get, dnf, yum, zypper or apk) when it is missing and turns on `logrotate.timer`; if it cannot be installed, the install stops before changing anything.
- `--check` reports a missing `logrotate` as FAIL, and warns when logrotate does not run daily.

## 1.1.1 - 2026-11-13

- `--check` warns when `logrotate` is not installed (for example on minimal Debian 13 images), instead of reporting log rotation as OK while the log is never rotated. The installer prints the same warning.

## 1.1.0 - 2026-11-13

- One-line install from GitHub (`get.sh`); installs to `/opt/hap-attachment-backup` with a `hap-attachment-backup` command.
- `--check`: tests settings, Docker, MongoDB, storage (with a sample file read), disk, log, schedule and the last run, with a fix for each problem.
- `--status`, `--log [N]` and `--support-bundle [DIR]`.
- Logging: run IDs on every line, `LOG_LEVEL` and `--debug` (every command and file), `--quiet`, `run.log` in each set, `last_run.json`, start-up failures recorded by the wrapper.
- Containers and the MongoDB shell are found automatically (`auto`), for international and China images.
- A run stops when the first 10 files cannot be read; files missing from storage are reported, not counted as failures.
- Failed full sets no longer count toward `KEEP_FULL`; nightly runs with nothing new write no set.
- Locking moved into the Python script (`flock` no longer needed); `install.sh --uninstall`.

## 1.0.0 - 2026-11-13

- First version: full and incremental backups named for Import Attachments, verify, restore to storage, retention, cron schedule.
