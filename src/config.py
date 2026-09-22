## Check config.example.json for example default values and config.schema.json for schema.

import os
import json
import copy

SRC_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(SRC_DIR)
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")


def _get_runtime_dir() -> str:
    """Get secure runtime dir: XDG_RUNTIME_DIR or fallback to /tmp/power-user-dictation-<uid>/"""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return xdg
    fallback = f"/tmp/power-user-dictation-{os.getuid()}"
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


RUNTIME_DIR = _get_runtime_dir()

PASTE_TOOLS = ("wtype", "ydotool")

DEFAULTS = {
    "log_level": "INFO",
    "silent_mode": False,
    "device": "cpu",
    "cuda": {
        "model": "turbo",
        "compute_type": "float16",
        "beam_size": 5,
    },
    "cpu": {
        "model": "base",
        "compute_type": "int8",
        "beam_size": 1,
    },
    "language": "en",
    "max_recording_sec": 120,
    "preopen_microphone": False,
    "paste": {
        "tools": ["wtype", "ydotool"],
        "key_delay_ms": 30,
        "pre_delay_ms": 100,
    },
    "paths": {
        "socket_path": os.path.join(RUNTIME_DIR, "power-user-dictation.sock"),
    },
}


def validate_config(config: dict) -> None:
    if type(config.get("silent_mode")) is not bool:
        raise ValueError("'silent_mode' must be a boolean")
    if type(config.get("preopen_microphone")) is not bool:
        raise ValueError("'preopen_microphone' must be a boolean")
    if config.get("log_level") not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("invalid log_level")

    device = config.get("device")
    if device not in {"cpu", "cuda"}:
        raise ValueError("device must be 'cpu' or 'cuda'")

    device_cfg = config.get(device, {})
    if not device_cfg.get("model"):
        raise ValueError(f"'{device}.model' required")
    if device_cfg.get("compute_type") not in {
        "float16",
        "float32",
        "int8",
        "int8_float16",
    }:
        raise ValueError(f"invalid '{device}.compute_type'")
    beam = device_cfg.get("beam_size", 1)
    if type(beam) is not int or beam < 1 or beam > 10:
        raise ValueError(f"'{device}.beam_size' must be int 1-10")

    for name, value, minimum, maximum in (
        ("max_recording_sec", config.get("max_recording_sec"), 1, None),
        ("paste.key_delay_ms", config.get("paste", {}).get("key_delay_ms"), 1, 200),
        ("paste.pre_delay_ms", config.get("paste", {}).get("pre_delay_ms"), 0, 500),
    ):
        if (
            type(value) is not int
            or value < minimum
            or (maximum is not None and value > maximum)
        ):
            limit = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
            raise ValueError(f"'{name}' must be int {limit}")

    tools = config.get("paste", {}).get("tools", [])
    if not tools:
        raise ValueError("'paste.tools' must list at least one tool")
    for tool in tools:
        if tool not in PASTE_TOOLS:
            raise ValueError(f"unknown paste tool '{tool}', expected {PASTE_TOOLS}")


def load_config() -> dict:
    """Load config from config.json, merging with defaults."""
    config = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            for key, value in json.load(f).items():
                if isinstance(value, dict) and key in config:
                    config[key].update(value)
                else:
                    config[key] = value
    validate_config(config)
    return config
