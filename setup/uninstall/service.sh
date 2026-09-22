#!/bin/sh
# Undo setup/install/service.sh. Leaves the venv and config.json untouched.
set -e
. "$(dirname "$0")/../lib.sh"

SERVICE_FILE="$HOME/.config/systemd/user/power-user-dictation.service"

state() {
    state_unit power-user-dictation.service --user
    state_path "$SERVICE_FILE"
}

echo "Before:"
state

echo ""
echo "Removes the service file listed above and stops the daemon."
confirm "Continue?"

systemctl --user disable --now power-user-dictation.service 2>/dev/null || true
remove "$SERVICE_FILE"
systemctl --user daemon-reload

echo ""
echo "After:"
state

echo ""
echo "Done. The venv and config.json are untouched."
