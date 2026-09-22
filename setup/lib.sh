#!/bin/sh
# Helpers shared by the install and uninstall scripts. Sourced, never run.

# ask <question>: yes/no, defaults to no. EOF (no terminal) counts as no.
ask() {
    printf '%s [y/N] ' "$1"
    read -r reply || reply=""
    case "$reply" in
        [yY]*) return 0 ;;
        *) return 1 ;;
    esac
}

# confirm <question>: like ask, but a no ends the script.
confirm() {
    ask "$1" || { echo "Aborted."; exit 0; }
}

# remove <path>...: deletes the paths that exist, sudo only where needed.
remove() {
    for path in "$@"; do
        [ -e "$path" ] || continue
        echo "Removing $path"
        if [ -w "$(dirname "$path")" ]; then
            rm -f "$path"
        else
            sudo rm -f "$path"
        fi
    done
}

# State reporting. Each helper prints one aligned line, so a script can call a
# handful of them before and after its work and the user can diff the two blocks
# by eye.

# state_line <label> <value>: for facts that are not a file, unit or account.
state_line() {
    printf '  %-44s %s\n' "$1" "$2"
}

# state_path <path>...: whether each file is there. Paths under $HOME print
# shortened, so they still fit the column.
state_path() {
    for path in "$@"; do
        case "$path" in
            "$HOME"/*) label="~${path#"$HOME"}" ;;
            *) label="$path" ;;
        esac
        if [ -e "$path" ]; then
            state_line "$label" "found"
        else
            state_line "$label" "not found"
        fi
    done
}

# state_unit <unit> [systemctl options]: how systemd sees the unit, which is not
# the same question as whether its file exists.
state_unit() {
    unit=$1
    shift
    load=$(systemctl "$@" show -p LoadState --value "$unit" 2>/dev/null || true)
    active=$(systemctl "$@" show -p ActiveState --value "$unit" 2>/dev/null || true)
    # is-enabled exits non-zero for disabled and missing units, and the pipe
    # keeps that from tripping set -e in the caller.
    enabled=$(systemctl "$@" is-enabled "$unit" 2>/dev/null | tail -n1)
    printf '  %-44s %s, %s, %s\n' "$unit" "${load:-unknown}" "${active:-unknown}" "${enabled:-unknown}"
}

# state_account <name>: the system user and the group of the same name.
state_account() {
    if id -u "$1" >/dev/null 2>&1; then
        printf '  %-44s uid %s\n' "user $1" "$(id -u "$1")"
    else
        printf '  %-44s not found\n' "user $1"
    fi
    entry=$(getent group "$1" || true)
    if [ -n "$entry" ]; then
        printf '  %-44s gid %s, members: %s\n' "group $1" \
            "$(echo "$entry" | cut -d: -f3)" "$(echo "$entry" | cut -d: -f4 | sed 's/^$/none/')"
    else
        printf '  %-44s not found\n' "group $1"
    fi
}
