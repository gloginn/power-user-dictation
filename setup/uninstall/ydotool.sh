#!/bin/sh
# Undo setup/install/ydotool.sh: remove the ydotoold service and its udev rule,
# then offer optional cleanup of the power-user-dictation-input account and group.
set -e
. "$(dirname "$0")/../lib.sh"

YDOTOOL_USER="power-user-dictation-input"
SERVICE="/etc/systemd/system/power-user-dictation-ydotoold.service"
UINPUT_RULES="/etc/udev/rules.d/99-power-user-dictation-uinput.rules"
SOCKET="/run/power-user-dictation/ydotool.sock"

state() {
    state_unit power-user-dictation-ydotoold.service
    state_path "$SERVICE" "$UINPUT_RULES" "$SOCKET"
    state_account "$YDOTOOL_USER"
}

echo "Before:"
state

echo ""
echo "Removes the service and project-specific udev rule listed above."
echo "Account cleanup is offered separately and defaults to no."
confirm "Continue?"

sudo systemctl disable --now power-user-dictation-ydotoold.service 2>/dev/null || true
remove "$SERVICE" "$UINPUT_RULES"
sudo systemctl daemon-reload
sudo udevadm control --reload-rules

CURRENT_USER=$(whoami)
if id -nG "$CURRENT_USER" | tr ' ' '\n' | grep -qx "$YDOTOOL_USER"; then
    if ask "Remove $CURRENT_USER from group $YDOTOOL_USER?"; then
        sudo gpasswd -d "$CURRENT_USER" "$YDOTOOL_USER" >/dev/null
        echo "Removed $CURRENT_USER from group $YDOTOOL_USER."
    fi
fi

if id -u "$YDOTOOL_USER" >/dev/null 2>&1; then
    if ask "Delete system user $YDOTOOL_USER?"; then
        sudo userdel "$YDOTOOL_USER"
        echo "Deleted user $YDOTOOL_USER."
    fi
fi

if getent group "$YDOTOOL_USER" >/dev/null; then
    if id -u "$YDOTOOL_USER" >/dev/null 2>&1; then
        echo "Keeping group $YDOTOOL_USER because the service user still exists."
    else
        MEMBERS=$(getent group "$YDOTOOL_USER" | cut -d: -f4)
        if [ -n "$MEMBERS" ]; then
            echo "Keeping group $YDOTOOL_USER because it still has members: $MEMBERS"
        elif ask "Delete empty system group $YDOTOOL_USER?"; then
            if sudo groupdel "$YDOTOOL_USER"; then
                echo "Deleted group $YDOTOOL_USER."
            else
                echo "WARN: group $YDOTOOL_USER could not be deleted."
            fi
        fi
    fi
fi

echo ""
echo "After:"
state

echo ""
echo "Done. Drop \"ydotool\" from paste.tools in config.json to skip the failed attempt."
echo "The ydotool package was left untouched."
