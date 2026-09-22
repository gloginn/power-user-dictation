# Troubleshooting

## CUDA Issues

### Model loading or transcription fails

The daemon logs the exception and exits, and systemd restarts it (3 starts per 30 seconds). See the error with:

```sh
journalctl --user -u power-user-dictation -n 100 --no-pager
```

### CUDA not detected

Check `nvidia-smi`, then re-run `setup/install/service.sh` to refresh the CUDA library path in the service.

## Paste Issues

With `"log_level": "DEBUG"`, the client logs which paste tool worked.

### wtype not working

```sh
wtype -M ctrl -k v -m ctrl
```

### ydotool not working

```sh
ls -la /run/power-user-dictation/ydotool.sock
groups | grep power-user-dictation-input   # if missing: sudo usermod -aG power-user-dictation-input $USER, then re-login
YDOTOOL_SOCKET=/run/power-user-dictation/ydotool.sock ydotool key 29:1 47:1 47:0 29:0
```

See [setup.md](setup.md#ydotool).

## Audio Issues

The daemon uses the system default input device. List devices with:

```sh
.venv/bin/python -c "import sounddevice; print(sounddevice.query_devices())"
```

## Logs

```sh
journalctl --user -u power-user-dictation -f          # daemon
journalctl --user -t power-user-dictation-client -f   # client
```

Set `"log_level"` in config.json to change verbosity (default `"INFO"`).
