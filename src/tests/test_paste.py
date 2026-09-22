#!/usr/bin/env python3
"""Paste and clipboard behavior with subprocesses mocked."""

import os
import subprocess
import sys
import unittest
from unittest.mock import call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dictation_client import copy_to_clipboard, paste_key


CONFIG = {
    "paste": {"tools": ["wtype", "ydotool"], "key_delay_ms": 30, "pre_delay_ms": 100},
}


class TestPaste(unittest.TestCase):
    @patch("dictation_client.time.sleep")
    @patch("dictation_client.subprocess.run")
    def test_falls_back_from_wtype_to_ydotool(self, run, sleep):
        run.side_effect = [subprocess.CalledProcessError(1, "wtype"), None]

        paste_key(CONFIG)

        sleep.assert_called_once_with(0.1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0],
            call(["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"], check=True, env=None),
        )
        command = run.call_args_list[1].args[0]
        env = run.call_args_list[1].kwargs["env"]
        self.assertEqual(
            command,
            [
                "ydotool", "key", "--key-delay", "30",
                "29:1", "47:1", "47:0", "29:0",
            ],
        )
        self.assertEqual(env["YDOTOOL_SOCKET"], "/run/power-user-dictation/ydotool.sock")

    @patch("dictation_client.time.sleep")
    @patch("dictation_client.subprocess.run", side_effect=FileNotFoundError)
    def test_raises_when_no_paste_tool_is_available(self, run, sleep):
        with self.assertRaisesRegex(RuntimeError, "No paste tool available"):
            paste_key(CONFIG)

        self.assertEqual(run.call_count, 2)

    @patch("dictation_client.subprocess.run")
    def test_clipboard_falls_back_from_wl_copy_to_xclip(self, run):
        run.side_effect = [subprocess.CalledProcessError(1, "wl-copy"), None]

        copy_to_clipboard("test clipboard")

        common = {
            "input": "test clipboard",
            "text": True,
            "check": True,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        self.assertEqual(
            run.call_args_list,
            [call(["wl-copy"], **common), call(["xclip", "-selection", "clipboard"], **common)],
        )


if __name__ == "__main__":
    unittest.main()
