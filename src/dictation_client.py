#!/usr/bin/env python3
"""Lightweight client for the dictation daemon - use this as your keybind."""

import os
import sys

import socket
import subprocess
import argparse
import time
import logging
from contextlib import contextmanager

from config import load_config
from logging_setup import set_level, setup_client_logging

from notifications import set_silent_mode, notify_error, notify_status

LOG_IDENT = "power-user-dictation-client"

# Blocking socket rather than asyncio: this runs on every keypress, before the
# microphone opens, and importing asyncio was most of the client's startup.
RESPONSE_TIMEOUT_SEC = 300

log = logging.getLogger("dictation.client")


@contextmanager
def socket_connect(socket_path: str):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(RESPONSE_TIMEOUT_SEC)
    try:
        connection.connect(socket_path)
        yield connection
    finally:
        connection.close()


def send_command(command: str, socket_path: str) -> str:
    with socket_connect(socket_path) as connection:
        connection.sendall(command.encode())
        # The daemon answers once and closes, so the response ends at EOF.
        chunks = []
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode()


def subscribe_status(socket_path: str):
    while True:
        try:
            with socket_connect(socket_path) as connection:
                connection.sendall(b"subscribe")
                # A subscription is idle for as long as the user is, so the
                # response timeout must not apply to it.
                connection.settimeout(None)
                with connection.makefile("r") as lines:
                    for line in lines:
                        response = line.strip()
                        if response not in {"status:idle", "status:recording", "status:transcribing"}:
                            raise ValueError(f"Unexpected status response: {response}")
                        yield response
        except OSError:
            pass
        yield "status:unavailable"
        time.sleep(1)


def paste_key(config: dict):
    paste_cfg = config["paste"]

    # Wait for triggering keys to fully release
    time.sleep(paste_cfg["pre_delay_ms"] / 1000)

    for tool in paste_cfg["tools"]:
        try:
            if tool == "wtype":
                command = ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]
                env = None
            else:
                command = [
                    "ydotool",
                    "key",
                    "--key-delay",
                    str(paste_cfg["key_delay_ms"]),
                    # 29:1 = Ctrl down, 47:1 = V down, 47:0 = V up, 29:0 = Ctrl up
                    "29:1",
                    "47:1",
                    "47:0",
                    "29:0",
                ]
                env = os.environ.copy()
                env["YDOTOOL_SOCKET"] = "/run/power-user-dictation/ydotool.sock"
            subprocess.run(command, check=True, env=env)
            log.debug("pasted via %s", tool)
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    raise RuntimeError("No paste tool available")


def copy_to_clipboard(text: str):
    cmds = [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for cmd in cmds:
        try:
            # Both tools fork and stay resident to serve the selection, and the
            # survivor keeps whatever stdout/stderr it inherited. Handed a pipe it
            # holds the write end open for good, so anything reading our output
            # blocks on an EOF that never comes. DEVNULL rather than
            # capture_output, which would make run() wait on that same EOF.
            subprocess.run(
                cmd,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    raise RuntimeError("No clipboard tool available (tried wl-copy, xclip)")


def handle_transcription(text: str, config: dict, clipboard_fn=copy_to_clipboard):
    log.info("Transcription received (%d characters)", len(text))
    clipboard_fn(text)

    try:
        paste_key(config)
    except RuntimeError as e:
        log.warning("paste failed (%s), falling back to clipboard only", e)


def main():
    config = load_config()
    set_silent_mode(config.get("silent_mode", False))
    set_level(getattr(logging, config["log_level"]))
    socket_path = config["paths"]["socket_path"]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        nargs="?",
        default="toggle",
        choices=["toggle", "retranscribe", "subscribe", "preopen"],
        help="Command to send to daemon",
    )
    parser.add_argument(
        "state",
        nargs="?",
        default=None,
        choices=["on", "off"],
        help="For preopen: 'on' holds the microphone open for a faster "
             "start, 'off' releases it between recordings. Omit to flip it.",
    )
    parser.add_argument(
        "--lang",
        default=None,
        help="Language code for transcription (e.g., pt, en)",
    )
    args = parser.parse_args()

    if args.command == "subscribe":
        for response in subscribe_status(socket_path):
            print(response, flush=True)
        return

    cmd = args.command
    # The protocol carries one argument per command, and what it means belongs
    # to the command: preopen reads a state, the rest read a language. Keeping
    # them apart here means a wrapper that always passes --lang cannot have it
    # land in preopen's argument.
    if args.command == "preopen":
        if args.state:
            cmd = f"{cmd}:{args.state}"
    elif args.lang:
        cmd = f"{cmd}:{args.lang}"

    try:
        response = send_command(cmd, socket_path)
    except (ConnectionRefusedError, FileNotFoundError) as e:
        # systemd owns the daemon's lifecycle; report instead of starting it.
        log.error("daemon unreachable: %s", e)
        notify_error("Dictation is not running - systemctl --user start power-user-dictation")
        sys.exit(1)

    log.debug("Response type: %s", response.partition(":")[0])

    # "recording:<lang>" needs nothing here: the daemon owns that banner.
    if response.startswith("transcription:"):
        text = response[len("transcription:") :]
        if text:
            handle_transcription(text, config)
    elif response.startswith("preopen:"):
        # The only command whose whole point is the answer it gives back, and
        # on a hotkey there is no terminal to print it to.
        print(response, flush=True)
        notify_status(
            "Microphone held open"
            if response == "preopen:on"
            else "Microphone released"
        )
    elif response.startswith("error:"):
        log.error(response)
        sys.exit(1)


if __name__ == "__main__":
    setup_client_logging(LOG_IDENT)
    try:
        main()
    except Exception as e:
        log.exception("unhandled error: %s", e)
        sys.exit(1)
