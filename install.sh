#!/usr/bin/env bash
# Install kitchenboss-bridge as a systemd service.
#
#   sudo ./install.sh              install or upgrade (keeps an existing config)
#   sudo ./install.sh --uninstall  remove service and program (keeps config + cache)
#
# Overridable: PREFIX=/opt/kitchenboss CONF_DIR=/etc/kitchenboss SERVICE_USER=kitchenboss PYTHON=python3

# started as "sh install.sh" (dash on Debian/Ubuntu): re-run under bash
if [ -z "${BASH_VERSION:-}" ]; then exec bash "$0" "$@"; fi
set -euo pipefail

NAME=kitchenboss-bridge
PREFIX=${PREFIX:-/opt/kitchenboss}
CONF_DIR=${CONF_DIR:-/etc/kitchenboss}
SERVICE_USER=${SERVICE_USER:-kitchenboss}
PYTHON=${PYTHON:-python3}
STATE_DIR=/var/lib/kitchenboss
UNIT=/etc/systemd/system/${NAME}.service
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 $*"
command -v systemctl >/dev/null || die "systemctl not found - this script needs systemd"

if [ "${1:-}" = "--uninstall" ]; then
    info "stopping and removing $NAME"
    systemctl disable --now "$NAME" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    echo "kept: $CONF_DIR, $STATE_DIR, /var/log/kitchenboss and user $SERVICE_USER (remove manually if wanted)"
    exit 0
fi
[ -z "${1:-}" ] || die "unknown option $1 (use --uninstall or nothing)"

[ -f "$SRC/pyproject.toml" ] && [ -f "$SRC/systemd/$NAME.service" ] || die "run from the kitchenboss source directory"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found (Debian/Ubuntu: apt install python3 python3-venv)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10 or newer required"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    info "creating system user $SERVICE_USER"
    NOLOGIN=$(command -v nologin || echo /usr/sbin/nologin)
    useradd --system --home-dir "$STATE_DIR" --no-create-home --shell "$NOLOGIN" "$SERVICE_USER"
fi

info "installing into $PREFIX/venv"
mkdir -p "$PREFIX"
if [ ! -x "$PREFIX/venv/bin/python" ]; then
    "$PYTHON" -m venv "$PREFIX/venv" || die "venv creation failed (Debian/Ubuntu: apt install python3-venv)"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
# build from a copy so the source tree stays clean and root-owned files do not end up in it
BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
cp -r "$SRC/pyproject.toml" "$SRC/README.md" "$SRC/kitchenboss" "$BUILD/"
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$BUILD"
install -m 644 "$SRC/README.md" "$SRC/PROTOCOL.md" "$SRC/config.example.yaml" "$PREFIX/"

install -d -m 750 -o root -g "$SERVICE_USER" "$CONF_DIR"
NEW_CONFIG=0
if [ ! -f "$CONF_DIR/config.yaml" ]; then
    info "creating $CONF_DIR/config.yaml from the example"
    install -m 640 -o root -g "$SERVICE_USER" "$SRC/config.example.yaml" "$CONF_DIR/config.yaml"
    # the service logs to the journal; DEBUG would log every payload
    sed -i 's/^  level: DEBUG/  level: INFO/' "$CONF_DIR/config.yaml"
    NEW_CONFIG=1
else
    chown root:"$SERVICE_USER" "$CONF_DIR/config.yaml"
    chmod 640 "$CONF_DIR/config.yaml"
fi
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_DIR"

info "installing $UNIT"
sed -e "s|@PREFIX@|$PREFIX|g" -e "s|@USER@|$SERVICE_USER|g" -e "s|@CONF_DIR@|$CONF_DIR|g" \
    "$SRC/systemd/$NAME.service" > "$UNIT"
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable "$NAME" >/dev/null

if [ "$NEW_CONFIG" -eq 1 ]; then
    cat <<MSG

Installed. Next steps:
  1. edit $CONF_DIR/config.yaml (account email/password, mqtt host/credentials)
  2. check:  sudo runuser -u $SERVICE_USER -- $PREFIX/venv/bin/$NAME --config $CONF_DIR/config.yaml --check
  3. start:  sudo systemctl start $NAME
  4. logs:   journalctl -u $NAME -f
MSG
else
    if runuser -u "$SERVICE_USER" -- "$PREFIX/venv/bin/$NAME" --config "$CONF_DIR/config.yaml" --check; then
        info "restarting $NAME"
        systemctl restart "$NAME"
        systemctl --no-pager --lines=5 status "$NAME" || true
    else
        echo "config check failed - fix $CONF_DIR/config.yaml, then: systemctl restart $NAME" >&2
        exit 1
    fi
    echo "logs: journalctl -u $NAME -f"
fi
