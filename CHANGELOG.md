# Changelog

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
