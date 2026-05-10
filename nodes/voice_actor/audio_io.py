"""Mic capture + speaker playback for the voice_actor node.

Both run as background asyncio tasks talking to sounddevice via thread-safe
queues. The Realtime API expects 24 kHz mono PCM16 in both directions.

Mic flow:
    sounddevice InputStream callback (audio thread)
        -> bytes pushed onto a thread-safe queue.Queue
        -> asyncio reader pulls from queue and feeds an asyncio.Queue

Speaker flow:
    asyncio writer pushes PCM16 bytes onto a thread-safe queue.Queue
    sounddevice OutputStream callback (audio thread) drains it
"""
from __future__ import annotations

import asyncio
import logging
import math
import queue as _queue
import threading
from typing import Optional

log = logging.getLogger("voice_actor.audio")

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # PCM16
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH


class AudioUnavailable(Exception):
    """Raised when sounddevice can't open input/output devices."""


def _import_sounddevice():
    try:
        import sounddevice as sd  # type: ignore
        import numpy as np  # type: ignore
        return sd, np
    except Exception as e:
        raise AudioUnavailable(f"sounddevice/numpy import failed: {e}") from e


class MicCapture:
    """Captures mic audio in PCM16 24kHz mono. push() output via async get()."""

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._sd = None
        self._np = None
        self._thread_q: _queue.Queue[bytes] = _queue.Queue(maxsize=200)
        self._async_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._stream = None
        self._reader_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closed = False
        self.last_rms: float = 0.0  # 0..1, updated by callback for UI meter

    def start(self) -> None:
        if self._stream is not None:
            return
        self._sd, self._np = _import_sounddevice()
        try:
            self._stream = self._sd.RawInputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=self._cb,
            )
            self._stream.start()
        except Exception as e:
            raise AudioUnavailable(f"could not open input stream: {e}") from e
        self._loop = asyncio.get_running_loop()
        self._reader_task = asyncio.create_task(self._reader())
        log.info("mic capture started @ %d Hz", self.sample_rate)

    def _cb(self, indata, frames, time_info, status) -> None:  # audio thread
        if status:
            log.debug("mic status: %s", status)
        try:
            buf = bytes(indata)
            # crude RMS for UI meter
            try:
                arr = self._np.frombuffer(buf, dtype=self._np.int16).astype(self._np.float32)
                if arr.size:
                    rms = float((arr * arr).mean()) ** 0.5
                    self.last_rms = min(1.0, rms / 32768.0)
            except Exception:
                pass
            try:
                self._thread_q.put_nowait(buf)
            except _queue.Full:
                # drop a frame rather than block the audio thread
                pass
        except Exception:
            log.exception("mic callback raised")

    async def _reader(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._closed:
                buf = await loop.run_in_executor(None, self._blocking_get)
                if buf is None:
                    return
                try:
                    self._async_q.put_nowait(buf)
                except asyncio.QueueFull:
                    # backpressure — drop oldest
                    try:
                        self._async_q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        self._async_q.put_nowait(buf)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            return

    def _blocking_get(self) -> Optional[bytes]:
        try:
            return self._thread_q.get(timeout=0.5)
        except _queue.Empty:
            return b""

    async def get(self) -> bytes:
        return await self._async_q.get()

    async def stop(self) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        log.info("mic capture stopped")


class SpeakerPlayback:
    """Drains PCM16 chunks pushed via play() through the default output device."""

    def __init__(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._sd = None
        self._np = None
        self._thread_q: _queue.Queue[bytes] = _queue.Queue(maxsize=400)
        self._stream = None
        self._buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._closed = False
        self._is_speaking = False
        self._last_audio_ts = 0.0

    def start(self) -> None:
        if self._stream is not None:
            return
        self._sd, self._np = _import_sounddevice()
        try:
            self._stream = self._sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=CHANNELS,
                dtype="int16",
                blocksize=FRAME_SAMPLES,
                callback=self._cb,
            )
            self._stream.start()
        except Exception as e:
            raise AudioUnavailable(f"could not open output stream: {e}") from e
        log.info("speaker playback started @ %d Hz", self.sample_rate)

    def _cb(self, outdata, frames, time_info, status) -> None:  # audio thread
        if status:
            log.debug("spk status: %s", status)
        need = frames * SAMPLE_WIDTH
        with self._buffer_lock:
            # top up buffer from queue
            while len(self._buffer) < need:
                try:
                    chunk = self._thread_q.get_nowait()
                except _queue.Empty:
                    break
                self._buffer.extend(chunk)
            if len(self._buffer) >= need:
                outdata[:need] = bytes(self._buffer[:need])
                del self._buffer[:need]
                self._is_speaking = True
            else:
                give = len(self._buffer)
                if give:
                    outdata[:give] = bytes(self._buffer[:give])
                    self._buffer.clear()
                # zero the rest (silence)
                outdata[give:need] = b"\x00" * (need - give)
                self._is_speaking = give > 0

    def play(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        try:
            self._thread_q.put_nowait(pcm16)
        except _queue.Full:
            # drop oldest if backed up
            try:
                self._thread_q.get_nowait()
            except _queue.Empty:
                pass
            try:
                self._thread_q.put_nowait(pcm16)
            except _queue.Full:
                pass

    def clear(self) -> None:
        """Drop any pending audio (used for barge-in / interruption)."""
        with self._buffer_lock:
            self._buffer.clear()
        try:
            while True:
                self._thread_q.get_nowait()
        except _queue.Empty:
            pass

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    async def stop(self) -> None:
        self._closed = True
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        log.info("speaker playback stopped")


def check_devices() -> dict:
    """Quick probe — returns {input_ok, output_ok, error?}."""
    out = {"input_ok": False, "output_ok": False}
    try:
        sd, _ = _import_sounddevice()
    except AudioUnavailable as e:
        out["error"] = str(e)
        return out
    try:
        sd.check_input_settings(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
        out["input_ok"] = True
    except Exception as e:
        out["input_error"] = str(e)
    try:
        sd.check_output_settings(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16")
        out["output_ok"] = True
    except Exception as e:
        out["output_error"] = str(e)
    return out
