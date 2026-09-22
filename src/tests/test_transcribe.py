#!/usr/bin/env python3
"""Daemon command flow with mocked audio and model; not recognition quality."""

import asyncio
import os
import sys
import tempfile
import threading
from types import SimpleNamespace
from collections.abc import Callable
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.modules["sounddevice"] = MagicMock()
sys.modules["faster_whisper"] = MagicMock()

from dictation_client import main as client_main, send_command, subscribe_status
from dictation_server import (
    BLOCKSIZE,
    AudioStream,
    TranscriptionModel,
    LEAD_IN_SILENCE_MS,
    SAMPLE_RATE,
    async_main,
)


class MockModel:
    def __init__(self, *args, **kwargs):
        self.transcribe = MagicMock(return_value=([SimpleNamespace(text=" test text")], None))


class MockInputStream:
    def __init__(self, callback, amplitude=0.1, **kwargs):
        self.callback = callback
        self.blocksize = kwargs.get("blocksize", 1024)
        self.amplitude = amplitude
        self.stop = MagicMock()
        self.close = MagicMock()

    def start(self):
        self.feed(self.amplitude)

    def feed(self, amplitude):
        block = np.full((self.blocksize, 1), amplitude, dtype=np.float32)
        self.callback(block, len(block), None, None)


class MockOpenMicStream(MockInputStream):
    """An always-open microphone: it emits only what the test feeds it."""

    def __init__(self, callback, **kwargs):
        super().__init__(callback, **kwargs)
        self.start = MagicMock()


class MockInputStreamContinuous(MockInputStream):
    """Feed more than one second of audio to exercise the recording limit."""

    def start(self):
        for _ in range(100):
            super().start()


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for patcher in (
            patch("notifications._silent_mode", False),
            patch("notifications.subprocess.run", return_value=MagicMock(
                returncode=0, stdout='{"data": [42]}'
            )),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmpdir.name, "test.sock")
        self.server_task = None

    async def asyncTearDown(self):
        await self.stop_server()
        self.tmpdir.cleanup()

    async def stop_server(self):
        if self.server_task:
            self.server_task.cancel()
            try:
                await asyncio.wait_for(self.server_task, timeout=2)
            except asyncio.CancelledError:
                pass

    async def start_server(
        self,
        model_factory: Callable[..., TranscriptionModel] | None = None,
        stream_factory: Callable[..., AudioStream] = MockInputStream,
        **config_overrides,
    ):
        self.model = MockModel()
        config = {
            "log_level": "DEBUG",
            "device": "cpu",
            "cpu": {"model": "base", "compute_type": "int8", "beam_size": 1},
            "language": "en",
            "initial_prompts": {},
            "paths": {"socket_path": self.socket_path},
            **config_overrides,
        }
        self.server_task = asyncio.create_task(
            async_main(
                config_factory=lambda: config,
                model_factory=model_factory or (lambda *args, **kwargs: self.model),
                stream_factory=stream_factory,
            )
        )
        for _ in range(100):
            if os.path.exists(self.socket_path):
                return
            await asyncio.sleep(0.01)
        self.fail("server socket was not created")

    async def send(self, command: str) -> str:
        """Run the blocking client off the loop the daemon answers on."""
        return await asyncio.to_thread(send_command, command, self.socket_path)

    async def subscribe(self):
        reader, writer = await asyncio.open_unix_connection(self.socket_path)

        async def close():
            writer.close()
            await writer.wait_closed()

        self.addAsyncCleanup(close)
        writer.write(b"subscribe")
        await writer.drain()
        return reader, writer

    async def assert_state(self, reader, state):
        self.assertEqual(await asyncio.wait_for(reader.readline(), 2), f"status:{state}\n".encode())


class TestStatusSubscription(ServerTestCase):
    async def test_snapshot_transitions_and_disconnected_observer(self):
        await self.start_server(silent_mode=True)
        first, first_writer = await self.subscribe()
        await self.assert_state(first, "idle")
        await self.send("toggle")
        await self.assert_state(first, "recording")
        second, _ = await self.subscribe()
        await self.assert_state(second, "recording")
        first_writer.close()
        await first_writer.wait_closed()
        await self.send("toggle")
        await self.assert_state(second, "transcribing")
        await self.assert_state(second, "idle")

    async def test_shutdown_signal_closes_live_subscription(self):
        import signal

        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler") as register:
            await self.start_server()
        reader, _ = await self.subscribe()
        await self.assert_state(reader, "idle")
        callbacks = {args[0]: args[1] for args, _ in register.call_args_list}
        callbacks[signal.SIGTERM]()
        assert self.server_task is not None
        await asyncio.wait_for(asyncio.shield(self.server_task), 1)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertFalse(os.path.exists(self.socket_path))

    async def test_client_reconnects_after_unavailable_daemon_and_restart(self):
        events = subscribe_status(self.socket_path)
        self.addCleanup(events.close)

        async def next_event():
            return await asyncio.to_thread(next, events)

        self.assertEqual(await next_event(), "status:unavailable")
        await self.start_server()
        self.assertEqual(await asyncio.wait_for(next_event(), 2), "status:idle")
        await self.stop_server()
        self.assertEqual(await asyncio.wait_for(next_event(), 2), "status:unavailable")
        await self.start_server()
        self.assertEqual(await asyncio.wait_for(next_event(), 2), "status:idle")


class TestServerTranscription(ServerTestCase):
    async def test_slow_notifications_preserve_order_without_delaying_commands(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def transport(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test notification was not released")
            return MagicMock(returncode=0, stdout='{"data": [42]}')

        with patch("notifications.subprocess.run", side_effect=transport):
            await self.start_server()
            reader, _ = await self.subscribe()
            await self.assert_state(reader, "idle")
            try:
                for _ in range(2):
                    self.assertEqual(
                        await asyncio.wait_for(self.send("toggle"), 1),
                        "recording:en",
                    )
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    await self.assert_state(reader, "recording")
                    response = await asyncio.wait_for(self.send("toggle"), 1)
                    self.assertTrue(response.startswith("transcription:"))
                    await self.assert_state(reader, "transcribing")
                    await self.assert_state(reader, "idle")
                self.assertEqual(len(calls), 1)
            finally:
                release.set()
                await self.stop_server()
            self.assertEqual([call[7] for call in calls],
                             ["Notify", "CloseNotification", "Notify", "CloseNotification"])
            self.assertEqual([call[-1] for call in calls if call[7] == "CloseNotification"], ["42", "42"])

    async def test_silent_mode_suppresses_recording_limit_and_failure_notifications(self):
        await self.start_server(
            silent_mode=True,
            stream_factory=MockInputStreamContinuous,
            max_recording_sec=1,
        )
        with patch("notifications.subprocess.run") as notification_transport:
            await self.send("toggle")
            self.assertTrue((await self.send("toggle")).startswith("transcription:"))
            with (
                patch.object(self.model, "transcribe", side_effect=ValueError("test failure")),
                patch("dictation_server.os._exit", side_effect=RuntimeError("daemon exited")) as exit_process,
            ):
                await self.send("retranscribe")
            exit_process.assert_called_once_with(1)
            await self.stop_server()
            notification_transport.assert_not_called()

    async def test_status_during_recording_transcription_and_retranscription(self):
        entered = threading.Event()
        release = threading.Event()

        def transcribe(audio, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test transcription timed out")
            return [SimpleNamespace(text=" test text")], None

        await self.start_server()
        self.model.transcribe.side_effect = transcribe
        reader, _ = await self.subscribe()
        await self.assert_state(reader, "idle")
        await self.send("toggle")
        await self.assert_state(reader, "recording")
        for command in ("toggle", "retranscribe"):
            entered.clear()
            release.clear()
            task = asyncio.create_task(self.send(command))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                await self.assert_state(reader, "transcribing")
                for busy_command in ("toggle", "retranscribe"):
                    self.assertEqual(await self.send(busy_command), "error:transcription in progress")
            finally:
                release.set()
                await task
            await self.assert_state(reader, "idle")

    async def test_microphone_failure_allows_next_toggle_to_retry(self):
        for failure_stage in ("create", "start"):
            with self.subTest(failure_stage=failure_stage):
                failed_stream = MagicMock()
                failed_stream.start.side_effect = RuntimeError("microphone unavailable")
                def create_stream(**kwargs):
                    if factory.call_count == 1:
                        if failure_stage == "create":
                            raise RuntimeError("microphone unavailable")
                        return failed_stream
                    return MockInputStream(**kwargs)

                factory = MagicMock(side_effect=create_stream)
                await self.start_server(stream_factory=factory)
                self.assertEqual(
                    await self.send("toggle"),
                    "error:microphone unavailable",
                )
                self.assertEqual(
                    await self.send("retranscribe"),
                    "error:no audio to retranscribe",
                )
                self.assertEqual(await self.send("toggle"), "recording:en")
                self.assertEqual(factory.call_count, 2)
                self.assertEqual(
                    await self.send("toggle"),
                    "transcription:test text",
                )
                if failure_stage == "start":
                    failed_stream.close.assert_called_once()
                await self.stop_server()

    async def test_device_is_held_from_startup_but_stopped_between_recordings(self):
        streams = []

        def stream_factory(**kwargs):
            streams.append(MockOpenMicStream(**kwargs))
            return streams[-1]

        await self.start_server(stream_factory=stream_factory, preopen_microphone=True)
        # Taken once, with the daemon, and immediately stopped: the device is
        # held open for a fast start but is not capturing.
        self.assertEqual(len(streams), 1)
        mic = streams[0]
        mic.start.assert_called_once()
        mic.stop.assert_called_once()

        # Audio arriving while stopped is retained by nothing.
        mic.feed(0.9)
        self.assertEqual(await self.send("toggle"), "recording:en")
        # Started again rather than re-created.
        self.assertEqual(mic.start.call_count, 2)
        self.assertEqual(len(streams), 1)
        mic.feed(0.2)
        self.assertEqual(await self.send("toggle"), "transcription:test text")

        audio = self.model.transcribe.call_args.args[0]
        lead_in = int(SAMPLE_RATE * LEAD_IN_SILENCE_MS / 1000)
        self.assertEqual(len(audio), lead_in + BLOCKSIZE)
        np.testing.assert_array_equal(audio[lead_in:], np.float32(0.2))

        # Stopped again once the recording ended, and only released at shutdown.
        self.assertEqual(mic.stop.call_count, 2)
        mic.close.assert_not_called()
        await self.stop_server()
        mic.close.assert_called_once()

    async def test_device_is_taken_per_recording_by_default(self):
        streams = []

        def stream_factory(**kwargs):
            streams.append(MockInputStream(**kwargs))
            return streams[-1]

        await self.start_server(stream_factory=stream_factory)
        # Nothing is opened until a toggle asks for it.
        self.assertEqual(streams, [])
        self.assertEqual(await self.send("toggle"), "recording:en")
        self.assertEqual(len(streams), 1)
        self.assertEqual(await self.send("toggle"), "transcription:test text")
        # Handed straight back, rather than held between recordings.
        streams[0].close.assert_called_once()
        self.assertEqual(await self.send("toggle"), "recording:en")
        self.assertEqual(len(streams), 2)

    async def test_preopen_command_takes_and_releases_the_device(self):
        streams = []

        def stream_factory(**kwargs):
            streams.append(MockOpenMicStream(**kwargs))
            return streams[-1]

        await self.start_server(stream_factory=stream_factory)
        self.assertEqual(streams, [])

        # On: the device is taken now and held stopped, without a restart.
        self.assertEqual(await self.send("preopen:on"), "preopen:on")
        self.assertEqual(len(streams), 1)
        mic = streams[0]
        mic.stop.assert_called_once()
        mic.close.assert_not_called()

        # A recording reuses it rather than opening another.
        self.assertEqual(await self.send("toggle"), "recording:en")
        mic.feed(0.2)
        self.assertEqual(await self.send("toggle"), "transcription:test text")
        self.assertEqual(len(streams), 1)

        # Off: handed back immediately, and the next recording opens its own.
        self.assertEqual(await self.send("preopen:off"), "preopen:off")
        mic.close.assert_called_once()
        self.assertEqual(await self.send("toggle"), "recording:en")
        self.assertEqual(len(streams), 2)

    async def test_preopen_flips_without_an_argument_and_rejects_junk(self):
        await self.start_server(
            stream_factory=MockOpenMicStream, preopen_microphone=True
        )
        # No argument flips whatever the daemon started with.
        self.assertEqual(await self.send("preopen"), "preopen:off")
        self.assertEqual(await self.send("preopen"), "preopen:on")
        self.assertEqual(
            await self.send("preopen:maybe"),
            "error:expected 'on' or 'off', got 'maybe'",
        )
        # Still on, since the rejected command changed nothing.
        self.assertEqual(await self.send("preopen:off"), "preopen:off")

    async def test_repeating_preopen_on_does_not_leak_a_device(self):
        streams = []

        def stream_factory(**kwargs):
            streams.append(MockOpenMicStream(**kwargs))
            return streams[-1]

        await self.start_server(
            stream_factory=stream_factory, preopen_microphone=True
        )
        self.assertEqual(len(streams), 1)
        # Asking for the state it is already in must not take a second device:
        # the daemon would hold a handle no later call could reach to close.
        for _ in range(3):
            self.assertEqual(await self.send("preopen:on"), "preopen:on")
        self.assertEqual(len(streams), 1)
        streams[0].close.assert_not_called()

        # And the one device it has is the one that gets released.
        self.assertEqual(await self.send("preopen:off"), "preopen:off")
        streams[0].close.assert_called_once()

    async def test_preopen_refuses_to_pull_the_device_mid_recording(self):
        await self.start_server(preopen_microphone=True)
        self.assertEqual(await self.send("toggle"), "recording:en")
        self.assertEqual(
            await self.send("preopen:off"),
            "error:cannot change the microphone while recording",
        )
        # The recording survived the refusal.
        self.assertEqual(await self.send("toggle"), "transcription:test text")

    async def test_transcription_failure_exits(self):
        await self.start_server()
        self.model.transcribe.side_effect = ValueError("invalid transcription input")
        await self.send("toggle")
        with patch("dictation_server.os._exit", side_effect=RuntimeError("daemon exited")) as exit_process:
            await self.send("toggle")
        exit_process.assert_called_once_with(1)

    async def test_toggle_returns_complete_long_transcription(self):
        await self.start_server()
        text = "test text " * 1000
        self.model.transcribe.return_value = ([SimpleNamespace(text=text)], None)
        self.assertEqual(await self.send("toggle"), "recording:en")
        self.assertEqual(await self.send("toggle"), "transcription:" + text.strip())

    async def test_language_override_and_retranscription(self):
        amplitudes = iter((0.1, 0.2))
        await self.start_server(stream_factory=lambda **kwargs: MockInputStream(
            amplitude=next(amplitudes), **kwargs
        ))
        for command, response in (
            ("toggle", "recording:en"),
            ("toggle", "transcription:test text"),
            ("toggle:es", "recording:es"),
            ("toggle:es", "transcription:test text"),
            ("retranscribe", "transcription:test text"),
            ("retranscribe:pt", "transcription:test text"),
        ):
            self.assertEqual(await self.send(command), response)
        calls = self.model.transcribe.call_args_list
        self.assertEqual([call.kwargs["language"] for call in calls], ["en", "es", "en", "pt"])
        self.assertFalse(np.array_equal(calls[0].args[0], calls[1].args[0]))
        for call in calls[2:]:
            np.testing.assert_array_equal(call.args[0], calls[1].args[0])

    async def test_retranscribe_without_last_audio_returns_error(self):
        await self.start_server()

        self.assertEqual(
            await self.send("retranscribe:pt"),
            "error:no audio to retranscribe",
        )

    async def test_recording_limit_stops_capture_and_waits_for_toggle(self):
        streams = []

        def stream_factory(**kwargs):
            stream = MockInputStreamContinuous(**kwargs)
            streams.append(stream)
            return stream

        max_sec = 1
        await self.start_server(
            stream_factory=stream_factory,
            max_recording_sec=max_sec,
        )

        reader, _ = await self.subscribe()
        await self.assert_state(reader, "idle")
        notification_done = threading.Event()
        with (
            patch("notifications.notify_recording_start", return_value=42),
            patch("notifications.notify_recording_stop") as close_banner,
            patch("notifications.notify_error", side_effect=lambda message: notification_done.set()) as notify,
        ):
            await self.send("toggle")
            await self.assert_state(reader, "recording")
            await self.assert_state(reader, "idle")
            streams[0].stop.assert_called_once()
            streams[0].close.assert_called_once()
            self.assertTrue(await asyncio.to_thread(notification_done.wait, 2))
            close_banner.assert_called_once_with(42)
            notify.assert_called_once_with("Recording limit reached. Press the hotkey to transcribe.")
        self.model.transcribe.assert_not_called()
        self.assertIn("awaits transcription", await self.send("retranscribe"))
        response = await self.send("toggle")

        self.assertTrue(response.startswith("transcription:"))
        await self.assert_state(reader, "transcribing")
        await self.assert_state(reader, "idle")
        lead_in = int(SAMPLE_RATE * LEAD_IN_SILENCE_MS / 1000)
        actual_samples = len(self.model.transcribe.call_args.args[0]) - lead_in
        self.assertLessEqual(actual_samples, int(max_sec * SAMPLE_RATE) + 2048)
        self.assertGreater(actual_samples, 0)
        self.assertEqual(await self.send("toggle"), "recording:en")
        self.assertEqual(len(streams), 2)


class TestClientArguments(unittest.TestCase):
    def sent_command(self, argv: list[str]) -> str:
        config = {"log_level": "INFO", "silent_mode": True,
                  "paths": {"socket_path": "/unused.sock"}}
        with (
            patch("dictation_client.load_config", return_value=config),
            patch("sys.argv", ["dictation_client.py", *argv]),
            patch("dictation_client.send_command", return_value="") as send,
        ):
            client_main()
        return send.call_args.args[0]

    def test_language_reaches_only_the_commands_that_read_one(self):
        self.assertEqual(self.sent_command(["--lang", "es"]), "toggle:es")
        self.assertEqual(
            self.sent_command(["retranscribe", "--lang", "es"]), "retranscribe:es"
        )
        # A wrapper that always passes --lang must not turn a flip into a state.
        self.assertEqual(self.sent_command(["preopen", "--lang", "es"]), "preopen")
        self.assertEqual(self.sent_command(["preopen", "off"]), "preopen:off")


class TestClientNotifications(unittest.TestCase):
    def test_preopen_banner_respects_silent_mode(self):
        for silent_mode in (True, False):
            config = {"log_level": "INFO", "silent_mode": silent_mode,
                      "paths": {"socket_path": "/unused.sock"}}
            with (
                self.subTest(silent_mode=silent_mode),
                patch("dictation_client.load_config", return_value=config),
                patch("sys.argv", ["dictation_client.py", "preopen", "off"]),
                patch("dictation_client.send_command", return_value="preopen:off"),
                patch("notifications._silent_mode", False),
                patch("notifications.subprocess.run", return_value=MagicMock(
                    returncode=0, stdout='{"data": [42]}'
                )) as notice,
            ):
                client_main()
            self.assertEqual(notice.call_count, 0 if silent_mode else 1)

    def test_unavailable_daemon_respects_silent_mode(self):
        for silent_mode in (True, False):
            config = {"log_level": "INFO", "silent_mode": silent_mode,
                      "paths": {"socket_path": "/unused.sock"}}
            with (
                self.subTest(silent_mode=silent_mode),
                patch("dictation_client.load_config", return_value=config),
                patch("sys.argv", ["dictation_client.py"]),
                patch("dictation_client.send_command", side_effect=FileNotFoundError),
                patch("notifications._silent_mode", False),
                patch("notifications.subprocess.run", return_value=MagicMock(
                    returncode=0, stdout='{"data": [42]}'
                )) as notice,
                self.assertRaises(SystemExit),
            ):
                client_main()
            self.assertEqual(notice.call_count, 0 if silent_mode else 1)


class TestModelFailure(unittest.IsolatedAsyncioTestCase):
    async def test_model_load_failure_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "log_level": "DEBUG",
                "device": "cpu",
                "cpu": {"model": "base", "compute_type": "float32"},
                "paths": {"socket_path": os.path.join(directory, "test.sock")},
            }
            with (
                patch("dictation_server.notify_error"),
                patch("dictation_server.os._exit", side_effect=SystemExit(1)) as exit_process,
                self.assertRaises(SystemExit),
            ):
                await async_main(
                    config_factory=lambda: config,
                    model_factory=MagicMock(side_effect=ValueError("invalid model")),
                )
            exit_process.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
