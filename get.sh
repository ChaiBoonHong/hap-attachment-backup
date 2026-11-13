#!/usr/bin/env bash
# Download and install HAP attachment backup from GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/ChaiBoonHong/hap-attachment-backup/main/get.sh | sudo bash
#
# Options go after "bash -s --":
#   ... | sudo bash -s -- --version v1.1.0     install a given release (default: latest release)
#   ... | sudo bash -s -- --no-cron            install without the nightly schedule
#   ... | sudo bash -s -- /srv/hap-backup      install to another folder
set -euo pipefail

REPO="${HAPBK_REPO:-ChaiBoonHong/hap-attachment-backup}"
REF=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --version) REF="${2:?--version needs a tag, e.g. v1.1.0}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

say() { echo "[get] $*"; }
die() { echo "[get] ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: curl -fsSL .../get.sh | sudo bash"
command -v tar >/dev/null || die "tar not found"
if command -v curl >/dev/null; then
  fetch() { curl -fsSL "$1"; }
elif command -v wget >/dev/null; then
  fetch() { wget -qO- "$1"; }
else
  die "curl or wget is needed"
fi

if [ -z "$REF" ]; then
  REF="$(fetch "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
         | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1 || true)"
fi
if [ -n "$REF" ]; then
  URL="https://github.com/$REPO/archive/refs/tags/$REF.tar.gz"
else
  REF="main"
  URL="https://github.com/$REPO/archive/refs/heads/main.tar.gz"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
say "downloading $REPO $REF"
fetch "$URL" > "$TMP/package.tar.gz" || die "download failed: $URL"
tar xzf "$TMP/package.tar.gz" -C "$TMP" || die "the download is not a valid package: $URL"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -f "$SRC/install.sh" ] || die "install.sh not found in the package"

bash "$SRC/install.sh" ${ARGS[@]+"${ARGS[@]}"}
