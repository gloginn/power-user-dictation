"""Configuration rejects invalid recording and paste timing before use."""

import copy
import json
import os
import sys
import unittest
from unittest.mock import mock_open, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import DEFAULTS, load_config, validate_config


class TestConfig(unittest.TestCase):
    def test_silent_mode_requires_boolean(self):
        for value in (True, False, "true", 1, None):
            with self.subTest(value=value):
                config = copy.deepcopy(DEFAULTS)
                config["silent_mode"] = value
                if type(value) is bool:
                    validate_config(config)
                else:
                    with self.assertRaisesRegex(ValueError, "silent_mode"):
                        validate_config(config)

    def test_defaults_and_partial_config_load(self):
        validate_config(copy.deepcopy(DEFAULTS))
        with (
            patch("config.os.path.exists", return_value=True),
            patch("builtins.open", mock_open(read_data='{"paste": {"pre_delay_ms": 0}}')),
        ):
            config = load_config()
        self.assertEqual(config["paste"]["pre_delay_ms"], 0)
        self.assertEqual(config["paste"]["key_delay_ms"], 30)

    def test_rejects_invalid_timing_in_config_file(self):
        cases = {
            "max_recording_sec": [0, -1, 0.5, "120", True, None],
            "key_delay_ms": [0, -1, 201, 1.5, "30", True, None],
            "pre_delay_ms": [-1, 501, 1.5, "100", True, None],
        }
        for field, values in cases.items():
            for value in values:
                data = {field: value} if field == "max_recording_sec" else {"paste": {field: value}}
                with (
                    self.subTest(field=field, value=value),
                    patch("config.os.path.exists", return_value=True),
                    patch("builtins.open", mock_open(read_data=json.dumps(data))),
                    self.assertRaisesRegex(ValueError, field),
                ):
                    load_config()

    def test_accepts_timing_boundaries(self):
        config = copy.deepcopy(DEFAULTS)
        config["max_recording_sec"] = 1
        for key_delay, pre_delay in ((1, 0), (200, 500)):
            config["paste"].update(key_delay_ms=key_delay, pre_delay_ms=pre_delay)
            validate_config(config)
