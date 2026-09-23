# Power User Dictation

A minimal, auditable, no-UI speech-to-text daemon for Linux power users. Intended to be used through keybinds or custom-made UIs.

- **~1410 lines of code** (~1040 Python, ~370 shell, tests aside) - small enough to read in one sitting
- **Model loads once** - daemon keeps faster-whisper in memory between transcriptions
- **Bring your own UI** - no window of its own; a bar or widget reads its state and sends commands over the same Unix socket

**Linux only** - uses Unix sockets, wtype or ydotool for paste simulation, wl-copy/xclip for clipboard.
  
  
https://github.com/user-attachments/assets/8a38ba99-c999-45db-844a-d77fc4baefbd  

*Running under niri with a DankMaterialShell bar widget. The widget is my own setup and isn't included.*


## Installation

Needs Python 3.12+ and a systemd Linux desktop. CUDA is optional; CPU is the default.

### 1. Get the code

```bash
git clone https://github.com/gloginn/power-user-dictation
cd power-user-dictation
```

The project runs from this folder, so put it where it can stay: the service bakes these paths in.

### 2. System packages

PortAudio and a clipboard tool. Debian and Ubuntu:

```bash
sudo apt install libportaudio2 wl-clipboard
```

Elsewhere PortAudio is usually `portaudio` (Arch, Fedora) and `wl-clipboard` keeps its name. On X11, `xclip` instead of `wl-clipboard`.

### 3. Python dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install .          # CPU
.venv/bin/pip install '.[gpu]'   # CUDA
```

pip only fills the venv; nothing is installed system-wide.

### 4. Install the service (required)

```bash
sh setup/install/service.sh
```

Writes and starts the `power-user-dictation` user service. Absolute paths get baked into it, so re-run it if you move the project or rebuild the venv.

### 5. Auto-paste (optional)

**Option A - wtype.** Install it with your distro, then check it works in your session with `wtype ""`:

```bash
sudo apt install wtype   # Debian, Ubuntu
```

**Option B - ydotool.** Install `ydotool` and `ydotoold` with your distro first. Use version 1.0.4 or newer. The script then sets up the `power-user-dictation-input` user, the `/dev/uinput` udev rule and its own `ydotoold` service. Log out and back in afterwards for the new group membership:

```bash
sh setup/install/ydotool.sh
```

Skip both to paste manually from the clipboard.

### 6. Bind a hotkey

Point a single hotkey at the client command from [Usage](#usage). Config is optional.

What the scripts do, or how to do it by hand: [setup.md](docs/setup.md).

### Uninstalling

Each script asks before touching anything, and none touches your venv or `config.json`:

```bash
sh setup/uninstall/service.sh        # service
sudo apt remove wtype                # the wtype package, however your distro removes it
sh setup/uninstall/ydotool.sh        # service, rule, optional account cleanup
```

## Usage

```bash
.venv/bin/python3 src/dictation_client.py
```

First call records, the next one transcribes and pastes, and so on.

Bind it to a hotkey in your desktop settings, e.g. [Ubuntu](https://help.ubuntu.com/stable/ubuntu-help/keyboard-shortcuts-set.html) or [niri](https://github.com/YaLTeR/niri/wiki/Configuration:-Key-Bindings), using absolute paths.

Recording stops automatically at `max_recording_sec` (120 seconds by default); the next press transcribes it.

### Watching the state

```bash
.venv/bin/python src/dictation_client.py subscribe
```

Prints the current state and each change, one per line: `status:idle`, `status:recording`, `status:transcribing`, or `status:unavailable` while the daemon is down. The client reconnects automatically.

## Configuration

Start from the example, and restart the service after changing it:

```bash
cp config.example.json config.json
```

Every key is optional; these are the common ones, with their defaults. Full schema in `config.schema.json`.

The comments below are only for reading. `config.json` is plain JSON, and a comment in it stops the daemon from starting.
```jsonc
{
  "language": "en",               // language Whisper transcribes in
  "device": "cpu",                // "cuda" for an NVIDIA GPU
  "cpu":  { "model": "base" },    // model used on CPU
  "cuda": { "model": "turbo" },   // model used on CUDA
  "max_recording_sec": 120,       // recording stops on its own after this
  "silent_mode": false,           // true disables all desktop notifications
  "paste": {
    "tools": ["wtype", "ydotool"] // tried in order; drop or reorder
  },
  "initial_prompts": {            // unset by default; per-language context that biases Whisper
    "en": "Working on a Linux terminal"
  },
  "hotwords": "systemd sudo ssh"  // unset by default; words Whisper should recognize better
}
```


## Pasting Behavior

Transcriptions are copied to the clipboard, then Ctrl+V is simulated with the first installed tool in `paste.tools` that works. Auto-paste is optional: with neither wtype nor ydotool, the text just stays in the clipboard.

## Extra features

### Language as an argument

`--lang` overrides the configured language for that one command:

```bash
.venv/bin/python3 src/dictation_client.py --lang es              # this recording, in es
.venv/bin/python3 src/dictation_client.py retranscribe --lang en # redo this one in en
```

It only matters on the press that transcribes, so one hotkey per language works.

**Use a valid [Whisper language code](https://github.com/openai/whisper/blob/main/whisper/tokenizer.py)** (`en`, `pt`, `es`...), in `--lang` and in `config.json`. Unknown codes are not checked and may fail the transcription.

### Retranscribe

Runs the last recording through the model again and pastes the new result, so you don't have to say it all again. The usual case: you spoke Portuguese but the hotkey was set to English, and the result came out garbled. Retranscribe with `--lang pt` and the same audio comes out right. It does not translate.

```bash
.venv/bin/python3 src/dictation_client.py retranscribe
```

### Faster recording start

Opening the microphone on every press can make recording start a little later than keeping it open. `preopen_microphone` has the daemon open it once at startup and keep it stopped between recordings. The default is `false`; restart the service after changing it.

```json
{
  "preopen_microphone": true
}
```

To switch it on the running daemon without a restart, use `preopen`. The change lasts until the daemon restarts.

```bash
.venv/bin/python3 src/dictation_client.py preopen off   # hand the microphone back
.venv/bin/python3 src/dictation_client.py preopen on    # hold it for a faster start
.venv/bin/python3 src/dictation_client.py preopen       # flip whichever it is
```

## Troubleshooting

Logs go to the journal: `journalctl --user -u power-user-dictation -f` for the daemon and `journalctl --user -t power-user-dictation-client -f` for the client.

CUDA issues, paste problems, audio device config - see [troubleshooting.md](docs/troubleshooting.md).

## License

MIT. See [LICENSE](LICENSE).
