#!/bin/sh
# Auto-paste fallback: run an existing ydotool installation as a system service
# under a dedicated power-user-dictation-input user.
# Without this you still get the transcription in the clipboard.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/../lib.sh"
YDOTOOL_USER="power-user-dictation-input"
YDOTOOL_GROUP="power-user-dictation-input"
YDOTOOLD_SERVICE_SRC="$SCRIPT_DIR/../power-user-dictation-ydotoold.service"
YDOTOOLD_SERVICE_DST="/etc/systemd/system/power-user-dictation-ydotoold.service"
UINPUT_RULES_DST="/etc/udev/rules.d/99-power-user-dictation-uinput.rules"
SOCKET="/run/power-user-dictation/ydotool.sock"

YDOTOOL_BIN=$(command -v ydotool || true)
YDOTOOLD_BIN=$(command -v ydotoold || true)

state() {
    state_unit power-user-dictation-ydotoold.service
    state_path "$YDOTOOLD_SERVICE_DST" "$UINPUT_RULES_DST" "$SOCKET"
    state_line "ydotool" "${YDOTOOL_BIN:-not found}"
    state_line "ydotoold" "${YDOTOOLD_BIN:-not found}"
    state_account "$YDOTOOL_USER"
}

echo "Before:"
state
echo ""

if [ -z "$YDOTOOL_BIN" ] || [ -z "$YDOTOOLD_BIN" ]; then
    echo "ERROR: ydotool and ydotoold must be installed first with your distro package manager." >&2
    exit 1
fi

if getent group "$YDOTOOL_GROUP" >/dev/null && ! id -u "$YDOTOOL_USER" >/dev/null 2>&1; then
    echo "ERROR: group $YDOTOOL_GROUP already exists without the matching service user." >&2
    echo "Resolve the account-name conflict before running this installer." >&2
    exit 1
fi

if id -u "$YDOTOOL_USER" >/dev/null 2>&1 && \
    [ "$(id -gn "$YDOTOOL_USER")" != "$YDOTOOL_GROUP" ]; then
    echo "ERROR: user $YDOTOOL_USER exists with a different primary group." >&2
    echo "Resolve the account-name conflict before running this installer." >&2
    exit 1
fi

if ! getent group "$YDOTOOL_GROUP" >/dev/null; then
    echo "Creating group $YDOTOOL_GROUP (requires sudo)..."
    sudo groupadd --system "$YDOTOOL_GROUP"
fi

if ! id -u "$YDOTOOL_USER" >/dev/null 2>&1; then
    echo "Creating user $YDOTOOL_USER (requires sudo)..."
    sudo useradd --system --gid "$YDOTOOL_GROUP" --shell /usr/sbin/nologin "$YDOTOOL_USER"
fi

echo "Installing uinput udev rule (requires sudo)..."
# tee, not `sudo install /dev/stdin`: sudo rewires the command's stdin, so that
# path no longer refers to the pipe.
printf 'KERNEL=="uinput", OWNER="%s", MODE="0600"\n' "$YDOTOOL_USER" |
    sudo tee "$UINPUT_RULES_DST" >/dev/null
sudo chmod 0644 "$UINPUT_RULES_DST"
sudo udevadm control --reload-rules
sudo udevadm trigger

NEEDS_RELOGIN=0
if ! id -nG "$(whoami)" | tr ' ' '\n' | grep -qx "$YDOTOOL_GROUP"; then
    echo "Adding $(whoami) to $YDOTOOL_GROUP (requires sudo)..."
    sudo usermod -aG "$YDOTOOL_GROUP" "$(whoami)"
    NEEDS_RELOGIN=1
fi

if [ ! -f "$YDOTOOLD_SERVICE_SRC" ]; then
    echo "ERROR: missing $YDOTOOLD_SERVICE_SRC" >&2
    exit 1
fi

echo "Installing power-user-dictation-ydotoold.service (requires sudo)..."
sudo install -m 0644 "$YDOTOOLD_SERVICE_SRC" "$YDOTOOLD_SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable power-user-dictation-ydotoold
sudo systemctl reset-failed power-user-dictation-ydotoold 2>/dev/null || true
# restart rather than "enable --now", which would leave an already-running
# ydotoold on the unit file that was just replaced.
sudo systemctl restart power-user-dictation-ydotoold

echo ""
echo "After:"
state

echo ""
echo "ydotoold configured. Binaries were left untouched."
if [ "$NEEDS_RELOGIN" = "1" ]; then
    echo "Log out and back in before auto-paste works - your new group membership needs it."
fi
