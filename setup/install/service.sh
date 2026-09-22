#!/bin/sh
# Required: install the dictation daemon as a systemd user service.
# Needs the venv to exist already. Reads the CUDA libs from it.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/../lib.sh"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python3"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_FILE="$SERVICE_DIR/power-user-dictation.service"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "ERROR: venv python not found at $VENV_PYTHON" >&2
    echo "Create it first: python3 -m venv .venv && .venv/bin/pip install ." >&2
    exit 1
fi

state() {
    state_unit power-user-dictation.service --user
    state_path "$SERVICE_FILE"
}

echo "Before:"
state
echo ""

# Auto-detect CUDA LD_LIBRARY_PATH from venv packages
LD_LIB_PATH="$($VENV_PYTHON -c "
import importlib, os, pathlib
paths = []
for pkg in ['nvidia.cudnn', 'nvidia.cublas']:
    try:
        mod = importlib.import_module(pkg)
        lib = pathlib.Path(mod.__path__[0]) / 'lib'
        if lib.is_dir():
            paths.append(str(lib))
    except ImportError:
        pass
print(':'.join(paths))
" 2>/dev/null || true)"

# OMP_NUM_THREADS = nproc - 1 (min 1)
NPROC=$(nproc)
OMP_THREADS=$((NPROC > 1 ? NPROC - 1 : 1))

ENV_LINES=""
if [ -n "$LD_LIB_PATH" ]; then
    ENV_LINES="Environment=LD_LIBRARY_PATH=$LD_LIB_PATH
"
    echo "Detected CUDA libs: $LD_LIB_PATH"
else
    echo "WARN: no CUDA libs detected, LD_LIBRARY_PATH not set"
fi
ENV_LINES="${ENV_LINES}Environment=OMP_NUM_THREADS=$OMP_THREADS"

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Power user dictation daemon
StartLimitBurst=3
StartLimitIntervalSec=30

[Service]
${ENV_LINES}
ExecStart=${VENV_PYTHON} ${PROJECT_ROOT}/src/dictation_server.py
Restart=on-failure
RestartSec=1

[Install]
WantedBy=default.target
EOF

echo "Wrote $SERVICE_FILE"

systemctl --user daemon-reload
systemctl --user enable power-user-dictation.service
# Clear any start-limit lockout, so reinstalling is also the way out of a
# crash loop. Harmless on a healthy unit.
systemctl --user reset-failed power-user-dictation.service 2>/dev/null || true
# restart rather than "enable --now": the unit file was just rewritten, and
# --now does nothing to a unit that is already running, so a reinstall would
# leave the old daemon in place with the old CUDA paths.
systemctl --user restart power-user-dictation.service

echo ""
echo "After:"
state

echo ""
echo "Installed and started."
systemctl --user status power-user-dictation.service --no-pager 2>/dev/null || true
