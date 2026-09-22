"""Routine logs must not include the dictated text."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import dictation_client


class TestLoggingContent(unittest.TestCase):
    def test_transcription_reaches_clipboard_without_appearing_in_logs(self):
        text = "private dictated text"
        config = {"log_level": "DEBUG", "paths": {"socket_path": "unused"}}
        with (
            patch("dictation_client.load_config", return_value=config),
            patch("sys.argv", ["dictation_client", "toggle"]),
            patch("dictation_client.send_command", new=MagicMock(return_value="transcription:" + text)),
            patch("dictation_client.paste_key"),
            patch("dictation_client.subprocess.run") as run,
            self.assertLogs("dictation.client", level="DEBUG") as captured,
        ):
            dictation_client.main()
        self.assertEqual(run.call_args.kwargs["input"], text)
        self.assertNotIn(text, "\n".join(captured.output))
        self.assertTrue(any("Transcription received" in line for line in captured.output))
