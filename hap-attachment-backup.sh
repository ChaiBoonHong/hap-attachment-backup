#!/usr/bin/env bash
# HAP attachment backup - entry point for cron and manual runs.
#
#   hap-attachment-backup [--full | --incremental | --verify [SET] | --restore-storage SET
#                          | --check | --status | --log [N] | --support-bundle [DIR]]
#                         [--dry-run] [--debug] [--quiet]
#
# The Python script writes its own log (LOG_FILE in backup.conf) and stops a second
# backup from starting while one runs. This wrapper finds Python and records start-up
# failures that happen before Python can log, so they also reach the log file.
set -u

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
CONF="${HAP_BACKUP_CONF:-$DIR/backup.conf}"
LOG_FILE="$(sed -n 's/^[[:space:]]*LOG_FILE[[:space:]]*=[[:space:]]*//p' "$CONF" 2>/dev/null | tail -1 | tr -d "\"'")"
LOG_FILE="${LOG_FILE:-/var/log/hap-attachment-backup.log}"

fail() {
  local line
  line="$(date '+%F %T') [ERROR] [wrapper] $1"
  echo "$line" >&2
  { echo "$line" >> "$LOG_FILE"; } 2>/dev/null
  exit 1
}

PY="$(command -v python3 || true)"
[ -n "$PY" ] || fail "python3 not found - install it (apt install python3 / dnf install python3)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || fail "Python 3.8 or later is required, found: $("$PY" -V 2>&1)"
[ -f "$DIR/hap_attachment_backup.py" ] || fail "missing $DIR/hap_attachment_backup.py - run install.sh again"

# Anything Python prints to stderr (for example a crash) is also appended to the log.
exec "$PY" "$DIR/hap_attachment_backup.py" --conf "$CONF" "$@" 2> >(tee -a "$LOG_FILE" >&2)
