#!/usr/bin/env bash
# Install, update or remove HAP attachment backup.
#
#   sudo bash install.sh [INSTALL_DIR] [--no-cron]   install or update (default /opt/hap-attachment-backup)
#   sudo bash install.sh --uninstall [INSTALL_DIR]   remove the script, schedule, log rotation and command;
#                                                    backups and the log file are kept
#
# logrotate is required and is installed with the system package manager if missing.
# An existing backup.conf is kept on update. Every step is printed and the result is
# written to the backup log, so an install can be checked later.
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEST="/opt/hap-attachment-backup"
BIN="/usr/local/bin/hap-attachment-backup"
CRON_FILE="/etc/cron.d/hap-attachment-backup"
ROTATE_FILE="/etc/logrotate.d/hap-attachment-backup"
CRON=1
UNINSTALL=0

for arg in "$@"; do
  case "$arg" in
    --no-cron) CRON=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg (see --help)" >&2; exit 1 ;;
    *) DEST="$arg" ;;
  esac
done

say() { echo "[install] $*"; }
die() { echo "[install] ERROR: $*" >&2; exit 1; }
conf_value() { sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$2" 2>/dev/null | tail -1 | tr -d "\"'"; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash install.sh"

if [ "$UNINSTALL" -eq 1 ]; then
  LOG_FILE="$(conf_value LOG_FILE "$DEST/backup.conf")"
  BACKUP_DIR="$(conf_value BACKUP_DIR "$DEST/backup.conf")"
  rm -f "$CRON_FILE" "$ROTATE_FILE" && say "removed $CRON_FILE and $ROTATE_FILE"
  if [ -L "$BIN" ]; then rm -f "$BIN" && say "removed $BIN"; fi
  if [ -f "$DEST/hap_attachment_backup.py" ]; then
    rm -rf "$DEST" && say "removed $DEST"
  else
    say "no installation found in $DEST"
  fi
  say "kept the backups in ${BACKUP_DIR:-/data/hap-attachment-backup} and the log ${LOG_FILE:-/var/log/hap-attachment-backup.log}"
  say "delete them yourself when they are no longer needed"
  exit 0
fi

[ "$SRC" != "$(readlink -f "$DEST" 2>/dev/null || echo "$DEST")" ] \
  || die "run install.sh from the downloaded package, not from $DEST (only --uninstall works here)"
for f in hap-attachment-backup.sh hap_attachment_backup.py export.js backup.conf; do
  [ -f "$SRC/$f" ] || die "missing $SRC/$f - download the complete package"
done
command -v docker >/dev/null || die "docker not found - this tool backs up HAP running in Docker"
command -v python3 >/dev/null || die "python3 not found - install it: apt install python3 (Debian/Ubuntu) or dnf install python3 (RHEL)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8 or later is required, found $(python3 -V 2>&1)"

# logrotate is required: it keeps the backup log small. Install it before changing anything else.
have_logrotate() { command -v logrotate >/dev/null 2>&1 || [ -x /usr/sbin/logrotate ] || [ -x /sbin/logrotate ]; }
PM_LOG="$(mktemp)"
pm() { echo "+ $*" >>"$PM_LOG"; "$@" >>"$PM_LOG" 2>&1; }
install_logrotate() {
  if command -v apt-get >/dev/null 2>&1; then
    pm env DEBIAN_FRONTEND=noninteractive apt-get install -y logrotate \
      || { pm apt-get update && pm env DEBIAN_FRONTEND=noninteractive apt-get install -y logrotate; }
  elif command -v dnf >/dev/null 2>&1; then pm dnf install -y logrotate
  elif command -v yum >/dev/null 2>&1; then pm yum install -y logrotate
  elif command -v zypper >/dev/null 2>&1; then pm zypper --non-interactive install logrotate
  elif command -v apk >/dev/null 2>&1; then pm apk add logrotate
  else echo "no supported package manager (apt-get, dnf, yum, zypper, apk)" >>"$PM_LOG"; return 1
  fi
}
if have_logrotate; then
  say "logrotate is installed"
else
  say "logrotate is not installed - installing it (required to keep the log small)"
  if install_logrotate && have_logrotate; then
    say "installed logrotate"
  else
    tail -n 6 "$PM_LOG" | sed 's/^/[install]   /' >&2
    rm -f "$PM_LOG"
    die "could not install logrotate, which is required. Install it yourself (apt install logrotate, dnf install logrotate, or the logrotate package from your OS media on a server without internet), then run install.sh again. Nothing was changed."
  fi
fi
rm -f "$PM_LOG"
if command -v systemctl >/dev/null 2>&1 && systemctl cat logrotate.timer >/dev/null 2>&1; then
  systemctl is-enabled --quiet logrotate.timer 2>/dev/null || systemctl enable --now logrotate.timer >/dev/null 2>&1 || true
  if systemctl is-enabled --quiet logrotate.timer 2>/dev/null; then say "logrotate runs daily (logrotate.timer)"
  else say "WARNING: could not enable logrotate.timer - run: systemctl enable --now logrotate.timer"; fi
elif [ -f /etc/cron.daily/logrotate ]; then
  say "logrotate runs daily (/etc/cron.daily/logrotate)"
else
  say "WARNING: logrotate is installed but nothing runs it daily (no logrotate.timer or /etc/cron.daily/logrotate)"
fi

VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/hap_attachment_backup.py")"
OLD=""
[ -f "$DEST/hap_attachment_backup.py" ] && OLD="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$DEST/hap_attachment_backup.py")"
if [ -n "$OLD" ]; then say "updating $DEST from $OLD to $VERSION"; else say "installing version $VERSION to $DEST"; fi

mkdir -p "$DEST"
install -m 0755 "$SRC/hap-attachment-backup.sh" "$SRC/install.sh" "$DEST/"
install -m 0644 "$SRC/hap_attachment_backup.py" "$SRC/export.js" "$DEST/"
for f in README.md CHANGELOG.md; do [ -f "$SRC/$f" ] && install -m 0644 "$SRC/$f" "$DEST/"; done
install -m 0644 "$SRC/backup.conf" "$DEST/backup.conf.example"
if [ -f "$DEST/backup.conf" ]; then
  say "kept your settings in $DEST/backup.conf (new defaults: $DEST/backup.conf.example)"
else
  install -m 0600 "$SRC/backup.conf" "$DEST/backup.conf"
  say "created $DEST/backup.conf"
fi

BACKUP_DIR="$(conf_value BACKUP_DIR "$DEST/backup.conf")"; BACKUP_DIR="${BACKUP_DIR:-/data/hap-attachment-backup}"
LOG_FILE="$(conf_value LOG_FILE "$DEST/backup.conf")"; LOG_FILE="${LOG_FILE:-/var/log/hap-attachment-backup.log}"
mkdir -p "$BACKUP_DIR" && chmod 0750 "$BACKUP_DIR" && say "backup folder $BACKUP_DIR"
mkdir -p "$(dirname "$LOG_FILE")"
[ -f "$LOG_FILE" ] || { touch "$LOG_FILE"; chmod 0640 "$LOG_FILE"; }
say "log file $LOG_FILE"

ln -sfn "$DEST/hap-attachment-backup.sh" "$BIN"
say "command $BIN"

if [ "$CRON" -eq 1 ]; then
  cat > "$CRON_FILE" <<EOF
# HAP attachment backup - times are the server's local time zone
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Sunday 02:00 - full copy
0 2 * * 0   root $DEST/hap-attachment-backup.sh --full --quiet >/dev/null 2>&1
# Monday-Saturday 02:00 - new files only
0 2 * * 1-6 root $DEST/hap-attachment-backup.sh --incremental --quiet >/dev/null 2>&1
EOF
  chmod 0644 "$CRON_FILE"
  say "schedule $CRON_FILE (Sunday 02:00 full, Monday-Saturday 02:00 new files)"
else
  say "no schedule installed (--no-cron)"
fi

cat > "$ROTATE_FILE" <<EOF
$LOG_FILE {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    create 0640 root root
}
EOF
say "log rotation $ROTATE_FILE (weekly, 8 weeks kept)"

echo "$(date '+%F %T') [INFO] [install] version $VERSION installed to $DEST${OLD:+ (was $OLD)}" >> "$LOG_FILE"

echo
say "checking the server:"
echo
"$BIN" --check || true
echo
say "done. Next steps:"
echo "  sudo hap-attachment-backup --dry-run      # what would be backed up"
echo "  sudo hap-attachment-backup --full         # first full backup"
echo "  sudo hap-attachment-backup --status       # backups, last run, schedule"
echo "  settings: $DEST/backup.conf, then run --check again"
