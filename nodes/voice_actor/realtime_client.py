"""Thin asyncio WebSocket client for the OpenAI Realtime API.

We deliberately use raw aiohttp WebSocket instead of the openai SDK so we
don't pin a specific SDK version. The wire protocol is a stream of
JSON-encoded events documented at:
    https://platform.openai.com/docs/api-reference/realtime

Audio format used here: 24 kHz mono PCM16, base64-encoded.
"""
from __future__ import annotations

import base64
import json
import logging
from typing import AsyncIterator, Optional

import aiohttp

log = logging.getLogger("voice_actor.realtime")

REALTIME_URL = "wss://api.openai.com/v1/realtime"


class RealtimeClient:
    """Wraps a single Realtime API WebSocket session.

    Open with `connect()`, drive the model via `send_*()` helpers, drain
    server events via `events()` async iterator (or pass a callback to
    `run()` which dispatches by event type).
    """

    def __init__(self, api_key: str, model: str = "gpt-realtime",
                 session_config: Optional[dict] = None) -> None:
        self.api_key = api_key
        self.model = model
        self.session_config = session_config or {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or self._ws is None or self._ws.closed

    async def connect(self) -> None:
        url = f"{REALTIME_URL}?model={self.model}"
        # GA Realtime API: no OpenAI-Beta header.
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(url, headers=headers, heartbeat=20)
        except Exception:
            await self._session.close()
            self._session = None
            raise
        log.info("realtime: connected to %s", url)
        if self.session_config:
            await self.update_session(self.session_config)

    async def update_session(self, session: dict) -> None:
        await self._send({"type": "session.update", "session": session})

    async def append_audio(self, pcm16: bytes) -> None:
        if not pcm16:
            return
        b64 = base64.b64encode(pcm16).decode("ascii")
        await self._send({"type": "input_audio_buffer.append", "audio": b64})

    async def commit_audio(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def clear_audio_buffer(self) -> None:
        await self._send({"type": "input_audio_buffer.clear"})

    async def create_assistant_text(self, text: str) -> None:
        """Insert an assistant-spoken text line and ask the model to speak it.

        Note: per the Realtime API docs you cannot create an *assistant audio*
        message directly — but you CAN add an assistant text item and then
        kick a response.create which will produce audio for it.
        Some model versions reject pre-filled assistant content; in that case
        we fall back to a system instruction asking it to read the line.
        """
        # Add the line as an assistant message item (the model will speak it on response.create).
        item = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        }
        await self._send({"type": "conversation.item.create", "item": item})
        await self.create_response({"output_modalities": ["audio"]})

    async def say_now(self, text: str, voice: Optional[str] = None) -> None:
        """Force the model to read `text` aloud verbatim.

        Uses an out-of-band response with explicit instructions, which works
        across model versions that don't accept pre-filled assistant audio.
        """
        instructions = (
            "Speak the following text aloud verbatim, naturally, with no "
            f"additions or commentary:\n\n{text}"
        )
        opts: dict = {
            "output_modalities": ["audio"],
            "instructions": instructions,
        }
        if voice:
            opts["audio"] = {"output": {"voice": voice}}
        await self.create_response(opts)

    async def create_response(self, options: Optional[dict] = None) -> None:
        evt: dict = {"type": "response.create"}
        if options:
            evt["response"] = options
        await self._send(evt)

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})

    async def _send(self, evt: dict) -> None:
        if self.closed:
            raise RuntimeError("realtime websocket is closed")
        assert self._ws is not None
        await self._ws.send_str(json.dumps(evt))

    async def events(self) -> AsyncIterator[dict]:
        if self._ws is None:
            return
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        yield json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning("realtime: non-JSON text frame")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    log.debug("realtime: unexpected binary frame, %d bytes", len(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                   aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    log.info("realtime: ws closed (%s)", msg.type)
                    break
        finally:
            self._closed = True

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None


def decode_audio_delta(delta_b64: str) -> bytes:
    return base64.b64decode(delta_b64)
