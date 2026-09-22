#!/usr/bin/env python3
"""Dictation daemon - keeps the transcription model loaded in memory."""

import os

import asyncio
import socket
import struct
import signal
import logging
from collections.abc import Callable, Iterable
from typing import NoReturn, Protocol

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from config import load_config
from logging_setup import set_level, setup_daemon_logging

from notifications import set_silent_mode, NotificationQueue, notify_error

class TextSegment(Protocol):
    @property
    def text(self) -> str: ...


class TranscriptionModel(Protocol):
    def transcribe(
        self, audio: np.ndarray, *, language: str, beam_size: int,
        vad_filter: bool, vad_parameters: dict[str, int | float],
        initial_prompt: str | None, hotwords: str | None,
        condition_on_previous_text: bool, patience: float,
    ) -> tuple[Iterable[TextSegment], object]: ...


class AudioStream(Protocol):
    def start(self) -> object: ...
    def stop(self) -> object: ...
    def close(self) -> object: ...


SAMPLE_RATE = 16000
BLOCKSIZE = 512  # 32ms chunks @ 16kHz

VAD_PARAMETERS = {
    "min_silence_duration_ms": 1000,
    "speech_pad_ms": 400,
    "threshold": 0.3,
}
# Fraction of the measured amplitude range used for trailing-silence trimming.
TRIM_SPEECH_THRESHOLD_RATIO = 0.08
TRIM_KEEP_SILENCE_MS = 700
TRIM_MIN_FRAMES = 60
LEAD_IN_SILENCE_MS = 300

log = logging.getLogger("dictation.server")


def trim_trailing_silence(
    audio: np.ndarray,
    min_silence_ms: int,
    keep_silence_ms: int,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    if audio.size == 0:
        return audio

    audio = audio.reshape(-1)
    frame_samples = BLOCKSIZE
    frame_count = len(audio) // frame_samples
    if frame_count < TRIM_MIN_FRAMES:
        return audio
    framed_audio = audio[: frame_count * frame_samples].reshape(
        frame_count, frame_samples
    )
    frame_rms = np.sqrt(np.mean(np.square(framed_audio), axis=1))

    signal_floor = float(np.percentile(frame_rms, 90))
    if signal_floor < 0.003:
        return audio

    noise_floor = float(np.percentile(frame_rms, 20))
    speech_threshold = max(
        0.003,
        noise_floor + (signal_floor - noise_floor) * TRIM_SPEECH_THRESHOLD_RATIO,
    )
    speech_frames = np.flatnonzero(frame_rms >= speech_threshold)
    if speech_frames.size == 0:
        return audio

    speech_end = min(len(audio), (int(speech_frames[-1]) + 1) * frame_samples)
    tail_samples = len(audio) - speech_end
    min_silence_samples = int(sample_rate * min_silence_ms / 1000)
    if tail_samples < min_silence_samples:
        return audio

    keep_samples = int(sample_rate * keep_silence_ms / 1000)
    return audio[: min(len(audio), speech_end + keep_samples)]


async def async_main(
    config_factory=load_config,
    model_factory: Callable[..., TranscriptionModel] = WhisperModel,
    stream_factory: Callable[..., AudioStream] = sd.InputStream,
):
    config = config_factory()
    set_silent_mode(config.get("silent_mode", False))
    set_level(getattr(logging, config["log_level"]))
    log.info("Dictation daemon starting")
    socket_path = config["paths"]["socket_path"]
    max_recording_sec = config.get("max_recording_sec", 120)
    max_chunks = int(max_recording_sec * SAMPLE_RATE / BLOCKSIZE)
    log.info("Max recording: %ss (%s chunks)", max_recording_sec, max_chunks)
    preopen = config.get("preopen_microphone", False)

    if os.path.exists(socket_path):
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.close()
            await writer.wait_closed()
            log.info("Daemon already running, exiting.")
            return
        except (ConnectionRefusedError, FileNotFoundError):
            try:
                os.unlink(socket_path)
            except Exception:
                log.exception("Failed to remove stale socket")
                raise

    def abort_on_failure() -> NoReturn:
        try:
            # The exception is already in the journal; the banner only has to
            # say where to look, since it cannot tell a transient failure from a
            # broken config.
            notify_error(
                "Dictation failed, restarting. "
                "Logs: journalctl --user -u power-user-dictation"
            )
        finally:
            # Transcription runs in a worker thread; terminate the whole daemon.
            os._exit(1)

    def load_model() -> TranscriptionModel:
        device = config["device"]
        cfg = config[device]
        log.info("Loading model '%s' on %s...", cfg["model"], device)
        try:
            model = model_factory(
                cfg["model"], device=device, compute_type=cfg["compute_type"]
            )
            log.info("Model loaded on %s!", device)
            return model
        except Exception:
            log.exception("Failed to load model on %s", device)
            abort_on_failure()

    model = load_model()
    device = config["device"]
    default_language = config["language"]

    def transcribe(audio: np.ndarray, lang: str | None = None) -> str:
        language = lang or default_language
        try:
            audio = trim_trailing_silence(
                audio,
                min_silence_ms=VAD_PARAMETERS["min_silence_duration_ms"],
                keep_silence_ms=TRIM_KEEP_SILENCE_MS,
            )
            lead_in = np.zeros(
                int(SAMPLE_RATE * LEAD_IN_SILENCE_MS / 1000), dtype=np.float32
            )
            audio = np.concatenate([lead_in, audio])
            segments, _ = model.transcribe(
                audio,
                language=language,
                beam_size=config[device]["beam_size"],
                vad_filter=True,
                vad_parameters=VAD_PARAMETERS.copy(),
                initial_prompt=config.get("initial_prompts", {}).get(language),
                hotwords=config.get("hotwords"),
                condition_on_previous_text=False,
                patience=1.5,
            )
            return "".join(segment.text for segment in segments).strip()
        except Exception:
            log.exception("Transcription failed on %s", device)
            abort_on_failure()

    chunks: list[np.ndarray] = []
    stream: AudioStream | None = None
    is_recording = False
    # Identifies the current recording so a callback queued by a finished one
    # cannot stop its successor. The stream cannot serve as that identity: when
    # the device is pre-opened it is the same object across every recording.
    session = 0
    is_transcribing = False
    last_audio: np.ndarray | None = None
    pending_audio: np.ndarray | None = None
    loop = asyncio.get_running_loop()
    notifications = NotificationQueue()
    subscribers: set[asyncio.StreamWriter] = set()

    def status_message() -> str:
        state = "transcribing" if is_transcribing else "recording" if is_recording else "idle"
        return f"status:{state}"

    def publish_status():
        message = (status_message() + "\n").encode()
        for writer in tuple(subscribers):
            # A stalled observer must not accumulate data or delay recording.
            if writer.is_closing() or writer.transport.get_write_buffer_size() > 4096:
                subscribers.discard(writer)
                writer.close()
                continue
            try:
                writer.write(message)
            except (ConnectionResetError, BrokenPipeError):
                subscribers.discard(writer)
                writer.close()

    def recording_callback(indata, frames, time_info, status):
        if is_recording and len(chunks) < max_chunks:
            chunks.append(indata.copy())
            if len(chunks) == max_chunks:
                log.warning("Max recording duration reached")
                loop.call_soon_threadsafe(finish_at_limit, session)

    def finish_at_limit(expected_session):
        nonlocal pending_audio
        if is_recording and session == expected_session:
            pending_audio = stop_recording()
            publish_status()
            notifications.error("Recording limit reached. Press the hotkey to transcribe.")

    def open_capture():
        """Create the input stream, running; a stream that failed is dropped."""
        nonlocal stream
        # Overwriting a live handle would leak the device: nothing else holds a
        # reference, so it could never be closed and its node would outlive the
        # daemon's own idea of what it has open.
        close_capture()
        try:
            stream = stream_factory(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=BLOCKSIZE,
                callback=recording_callback,
                latency="low",
            )
            stream.start()
        except Exception:
            log.exception("Failed to open the microphone")
            if stream:
                try:
                    stream.close()
                except Exception:
                    log.exception("Error closing failed stream")
            stream = None
            raise

    def release_capture():
        """Pre-opened, only stop the stream so the device stays open; otherwise hand it back."""
        if not preopen:
            close_capture()
            return
        if stream:
            try:
                stream.stop()
            except Exception:
                log.exception("Error stopping stream")

    def close_capture():
        nonlocal stream
        if stream:
            try:
                stream.stop()
            except Exception:
                log.exception("Error stopping stream")
            try:
                stream.close()
            except Exception:
                log.exception("Error closing stream")
            stream = None

    def start_recording():
        nonlocal chunks, is_recording, session
        chunks = []
        session += 1
        is_recording = True
        try:
            if stream is None:
                # Not pre-opened, startup could not open it, or it went away.
                open_capture()
            else:
                stream.start()
        except Exception:
            is_recording = False
            chunks = []
            # Take the device again on the next toggle rather than staying
            # wedged on a handle that no longer works.
            close_capture()
            raise

    def stop_recording() -> np.ndarray:
        nonlocal is_recording
        is_recording = False
        notifications.recording_stop()
        release_capture()
        if chunks:
            return np.concatenate(chunks, axis=0).flatten()
        return np.array([], dtype=np.float32)

    async def transcribe_audio(audio, lang):
        nonlocal is_transcribing
        is_transcribing = True
        publish_status()
        try:
            text = await asyncio.to_thread(transcribe, audio, lang)
            return f"transcription:{text}"
        finally:
            is_transcribing = False
            publish_status()

    async def handle_toggle(lang: str | None = None) -> str:
        nonlocal last_audio, pending_audio
        if is_transcribing:
            return "error:transcription in progress"
        audio = stop_recording() if is_recording else pending_audio
        if audio is not None:
            pending_audio = None
            if audio.size == 0:
                publish_status()
                return "error:no audio recorded"
            last_audio = audio
            return await transcribe_audio(audio, lang)
        start_recording()
        publish_status()
        language = lang or default_language
        notifications.recording_start(language)
        return f"recording:{language}"

    async def handle_retranscribe(lang: str | None = None) -> str:
        if is_transcribing:
            return "error:transcription in progress"
        if is_recording:
            return "error:cannot retranscribe while recording"
        if pending_audio is not None:
            return "error:recording awaits transcription; use toggle"
        if last_audio is None or last_audio.size == 0:
            return "error:no audio to retranscribe"
        return await transcribe_audio(last_audio, lang)

    async def handle_preopen(state: str | None = None) -> str:
        nonlocal preopen
        if state is None:
            wanted = not preopen
        elif state in ("on", "off"):
            wanted = state == "on"
        else:
            return f"error:expected 'on' or 'off', got '{state}'"
        if is_recording:
            return "error:cannot change the microphone while recording"
        if wanted == preopen:
            return f"preopen:{'on' if preopen else 'off'}"

        preopen = wanted
        if preopen:
            try:
                open_capture()
                # Opening starts the device; hold it stopped until a toggle.
                release_capture()
            except Exception:
                preopen = False
                return "error:could not open the microphone"
        else:
            close_capture()
        log.info("Pre-open microphone: %s", "on" if preopen else "off")
        return f"preopen:{'on' if preopen else 'off'}"

    async def shutdown():
        log.info("Shutting down daemon...")
        if is_recording:
            stop_recording()
        close_capture()
        for writer in tuple(subscribers):
            writer.close()
        subscribers.clear()
        server.close()
        await server.wait_closed()
        try:
            if os.path.exists(socket_path):
                os.unlink(socket_path)
        except OSError:
            log.warning("Failed to remove %s", socket_path)

    # Every command is "<name>" or "<name>:<argument>", and every handler takes
    # that argument the same way, so the name alone chooses the handler. What
    # the argument means belongs to the handler: a language, or on/off.
    handlers = {
        "toggle": handle_toggle,
        "retranscribe": handle_retranscribe,
        "preopen": handle_preopen,
    }

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sock = writer.get_extra_info("socket")
        if sock:
            try:
                creds = sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                _, uid, _ = struct.unpack("3i", creds)
                if uid != os.getuid():
                    log.warning("Rejected connection from UID %s", uid)
                    writer.close()
                    await writer.wait_closed()
                    return
            except Exception as e:
                log.error("Failed to verify peer credentials: %s", e)
                writer.close()
                await writer.wait_closed()
                return

        try:
            data = await reader.read(1024)
            command = data.decode().strip()
            name, separator, value = command.partition(":")
            lang = value or None if separator else None

            if name == "subscribe":
                subscribers.add(writer)
                writer.write((status_message() + "\n").encode())
                await writer.drain()
                # This connection only streams state; EOF ends the subscription.
                await reader.read(1)
                return

            handler = handlers.get(name)
            if handler is None:
                response = f"error:unknown command: {command}"
            else:
                response = await handler(lang)

            writer.write(response.encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.exception("Error handling client")
            try:
                writer.write(f"error:{e}".encode())
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
        finally:
            subscribers.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    if preopen:
        # Starting an existing stream is faster and steadier than creating one
        # per toggle, and that delay lands on the first word.
        log.info("Microphone opened at startup, stopped between recordings")
        try:
            open_capture()
            # PortAudio's ALSA backend starts the device as it opens it, so
            # the stream runs for the moment between these two calls. Stopping
            # it settles the device into the idle state it holds until a
            # toggle, delivering no audio.
            release_capture()
        except Exception:
            # A toggle opens the device instead; losing the fast start does not
            # justify refusing to run.
            log.warning("Microphone did not open; the next toggle retries")

    try:
        server = await asyncio.start_unix_server(handle_client, socket_path)
        os.chmod(socket_path, 0o600)
    except OSError as e:
        log.error("Failed to start server: %s", e)
        raise

    log.info("Daemon listening on %s", socket_path)

    stopped = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)

    try:
        await stopped.wait()
    except asyncio.CancelledError:
        pass
    finally:
        await shutdown()
        await notifications.close()
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)


if __name__ == "__main__":
    setup_daemon_logging()
    asyncio.run(async_main())
