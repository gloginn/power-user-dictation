# Setup

What the service and optional desktop integrations configure.

## Notifications

Banners go through `org.freedesktop.Notifications` via `busctl`. A failed notification is logged and dropped; it never stops a recording.

If none show up, check `silent_mode` is `false`, then that a notification service answers:

```sh
busctl --user call org.freedesktop.Notifications /org/freedesktop/Notifications org.freedesktop.Notifications GetServerInformation
```

To change how they look, edit `src/notifications.py`.

## Systemd Service

```sh
sh setup/install/service.sh
```

Detects the CUDA libs in the venv and bakes them into the user service. Re-run after changing the venv or CUDA setup. No root needed.

Every script under `setup/install/` has a twin in `setup/uninstall/`.

## Failure Recovery

A model-loading or transcription exception makes the daemon exit with code 1, and systemd restarts it within the service's start limit. See [troubleshooting.md](troubleshooting.md).

## Ydotool

Install `ydotool` and `ydotoold` with your distro first. Use version 1.0.4 or newer. The installer never downloads or replaces package-managed binaries.

```sh
sh setup/install/ydotool.sh
```

The script creates the `power-user-dictation-input` account, installs a udev rule and starts `ydotoold`. Log out and back in if it added you to the socket group.

The uninstaller removes the service and the udev rule, then asks before removing your group membership, the account or the group. Distro binaries are never removed.

## Permissions

The udev rule gives `/dev/uinput` to `power-user-dictation-input` with mode `0600`. `ydotoold` runs as that account and creates its socket with mode `0660`, and your user joins that group to reach it.
