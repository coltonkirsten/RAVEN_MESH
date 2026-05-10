"""voice_actor — bidirectional voice mesh node.

Opens an OpenAI Realtime API session, plays the model's audio out of the
Mac's speakers, captures the mic, and forwards transcribed user speech to
a configurable mesh target so an agent can react to what the human said.

Tool surfaces:
    voice_actor.start_session    -> { session_id }
    voice_actor.stop_session     -> { stopped }
    voice_actor.say              -> { spoken }
    voice_actor.session_status   -> { active, ... }
    voice_actor.ui_visibility    -> ui_visibility helper

Inspector UI:
    http://127.0.0.1:8807
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import pathlib
import signal
import sys
import uuid
from typing import Optional

from aiohttp import web

from node_sdk import MeshError, MeshNode
from nodes.ui_visibility import (
    VisibilityState,
    make_handler as make_visibility_handler,
    make_visibility_middleware,
    report_status,
)

from .audio_io import AudioUnavailable, MicCapture, SpeakerPlayback, check_devices
from .realtime_client import RealtimeClient, decode_audio_delta

log = logging.getLogger("voice_actor")
HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"

DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = os.environ.get("VOICE_ACTOR_MODEL", "gpt-realtime-2")
TRANSCRIPT_MAX = 100


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class Session:
    """One live Realtime API session + audio I/O loop."""

    def __init__(self, *, voice: str, system_prompt: Optional[str],
                 user_target: Optional[str], user_target_node: Optional[str],
                 assistant_target: Optional[str], api_key: str, model: str,
                 owner: "VoiceActor") -> None:
        self.id = f"vs_{uuid.uuid4().hex[:12]}"
        self.voice = voice
        self.system_prompt = system_prompt
        self.user_target = user_target
        self.user_target_node = user_target_node
        self.assistant_target = assistant_target
        self.started_at = now_iso()
        self.last_user_transcript: Optional[str] = None
        self.last_assistant_transcript: Optional[str] = None
        self.error: Optional[str] = None
        self._owner = owner

        self.client = RealtimeClient(api_key=api_key, model=model)
        self.mic = MicCapture()
        self.spk = SpeakerPlayback()

        self._mic_task: Optional[asyncio.Task] = None
        self._evt_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        self.input_device_ok = False
        self.output_device_ok = False

    async def start(self) -> None:
        # Open speakers first — text-only "say" mode still works without mic.
        try:
            self.spk.start()
            self.output_device_ok = True
        except AudioUnavailable as e:
            log.warning("speaker unavailable: %s", e)
            self.output_device_ok = False
        try:
            self.mic.start()
            self.input_device_ok = True
        except AudioUnavailable as e:
            log.warning("mic unavailable: %s", e)
            self.input_device_ok = False

        cfg: dict = {
            "modalities": ["audio", "text"],
            "voice": self.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {"type": "server_vad"},
        }
        if self.system_prompt:
            cfg["instructions"] = self.system_prompt
        self.client.session_config = cfg
        await self.client.connect()

        self._evt_task = asyncio.create_task(self._event_loop())
        if self.input_device_ok:
            self._mic_task = asyncio.create_task(self._mic_loop())

    async def stop(self) -> None:
        self._stopped.set()
        try:
            await self.client.close()
        except Exception:
            pass
        for t in (self._mic_task, self._evt_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._mic_task = None
        self._evt_task = None
        try:
            await self.mic.stop()
        except Exception:
            pass
        try:
            await self.spk.stop()
        except Exception:
            pass

    async def say(self, text: str) -> None:
        await self.client.say_now(text, voice=self.voice)

    # ------------------- internal loops -------------------

    async def _mic_loop(self) -> None:
        try:
            while not self._stopped.is_set() and not self.client.closed:
                buf = await self.mic.get()
                if not buf:
                    continue
                try:
                    await self.client.append_audio(buf)
                except Exception as e:
                    log.warning("realtime append_audio failed: %s", e)
                    return
        except asyncio.CancelledError:
            return

    async def _event_loop(self) -> None:
        try:
            async for evt in self.client.events():
                if self._stopped.is_set():
                    return
                await self._handle_event(evt)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("realtime event loop crashed")
            self.error = "event_loop_crashed"
        finally:
            await self._owner._on_session_ended(self)

    async def _handle_event(self, evt: dict) -> None:
        et = evt.get("type", "")
        if et in ("response.audio.delta", "response.output_audio.delta"):
            delta = evt.get("delta")
            if delta:
                try:
                    self.spk.play(decode_audio_delta(delta))
                except Exception as e:
                    log.debug("audio decode failed: %s", e)
        elif et == "conversation.item.input_audio_transcription.completed":
            transcript = (evt.get("transcript") or "").strip()
            if transcript:
                self.last_user_transcript = transcript
                self._owner._record("user", transcript)
                await self._forward_user_transcript(transcript)
        elif et in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
            transcript = (evt.get("transcript") or "").strip()
            if transcript:
                self.last_assistant_transcript = transcript
                self._owner._record("assistant", transcript)
                await self._forward_assistant_transcript(transcript)
        elif et == "input_audio_buffer.speech_started":
            self.spk.clear()  # barge-in: drop queued speech
            self._owner._set_status("listening")
        elif et == "input_audio_buffer.speech_stopped":
            self._owner._set_status("processing")
        elif et in ("response.created", "response.output_item.added"):
            self._owner._set_status("speaking")
        elif et == "response.done":
            self._owner._set_status("listening" if self.input_device_ok else "idle")
        elif et == "error":
            err = evt.get("error", {})
            log.warning("realtime error: %s", err)
            self.error = err.get("message") or json.dumps(err)[:200]
            self._owner._set_status("error")
        else:
            log.debug("realtime event: %s", et)

    async def _forward_user_transcript(self, text: str) -> None:
        await self._forward(text, role="user",
                            target=self.user_target,
                            inbox_node=self.user_target_node)

    async def _forward_assistant_transcript(self, text: str) -> None:
        await self._forward(text, role="assistant",
                            target=self.assistant_target,
                            inbox_node=None)

    async def _forward(self, text: str, *, role: str,
                       target: Optional[str], inbox_node: Optional[str]) -> None:
        if not target and not inbox_node:
            return
        target_surface = target or f"{inbox_node}.inbox"
        payload = {
            "from": "voice_actor",
            "kind": "voice_transcript",
            "role": role,
            "text": text,
            "session_id": self.id,
            "timestamp": now_iso(),
        }
        try:
            await self._owner.node.invoke(target_surface, payload, wait=False)
        except MeshError as e:
            log.warning("forward to %s failed: %s %s", target_surface, e.status, e.data)
        except Exception:
            log.exception("forward to %s raised", target_surface)


class VoiceActor:
    def __init__(self, node: MeshNode, *, api_key: Optional[str], model: str,
                 replace_active: bool = True) -> None:
        self.node = node
        self.api_key = api_key
        self.model = model
        self.replace_active = replace_active
        self.session: Optional[Session] = None
        self.status = "idle"  # idle | listening | speaking | processing | error
        self.transcript_log: list[dict] = []
        self.subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()

    # ---------- public state for inspector ----------

    def state(self) -> dict:
        s = self.session
        return {
            "node_id": self.node.node_id,
            "model": self.model,
            "key_present": bool(self.api_key),
            "devices": check_devices(),
            "status": self.status,
            "active": s is not None,
            "session": {
                "id": s.id,
                "voice": s.voice,
                "system_prompt": s.system_prompt,
                "user_target": s.user_target,
                "user_target_node": s.user_target_node,
                "assistant_target": s.assistant_target,
                "started_at": s.started_at,
                "last_user_transcript": s.last_user_transcript,
                "last_assistant_transcript": s.last_assistant_transcript,
                "input_device_ok": s.input_device_ok,
                "output_device_ok": s.output_device_ok,
                "error": s.error,
                "mic_rms": getattr(s.mic, "last_rms", 0.0),
            } if s else None,
            "transcript": list(self.transcript_log),
        }

    async def push(self) -> None:
        snap = self.state()
        for q in list(self.subscribers):
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                pass

    def _set_status(self, status: str) -> None:
        if self.status != status:
            self.status = status
            asyncio.create_task(self.push())

    def _record(self, role: str, text: str) -> None:
        entry = {"role": role, "text": text, "timestamp": now_iso()}
        self.transcript_log.append(entry)
        del self.transcript_log[: max(0, len(self.transcript_log) - TRANSCRIPT_MAX)]
        asyncio.create_task(self.push())

    async def _on_session_ended(self, sess: Session) -> None:
        if self.session is sess:
            log.info("session %s ended", sess.id)
            self.session = None
            self._set_status("idle")
            await self.push()

    # ---------- mesh tool handlers ----------

    async def start_session(self, env: dict) -> dict:
        if not self.api_key:
            return {"error": "openai_key_missing", "detail": "set OPENAI_API_KEY"}
        body = env.get("payload", {}) or {}
        voice = body.get("voice") or DEFAULT_VOICE
        system_prompt = body.get("system_prompt")
        user_target = body.get("on_user_transcript_target")
        user_target_node = body.get("on_user_transcript_node")
        assistant_target = body.get("on_assistant_transcript_target")

        async with self._lock:
            if self.session is not None:
                if not self.replace_active:
                    return {"error": "session_already_active",
                            "session_id": self.session.id}
                log.info("replacing active session %s", self.session.id)
                old = self.session
                self.session = None
                try:
                    await old.stop()
                except Exception:
                    log.exception("old session stop raised")

            sess = Session(
                voice=voice, system_prompt=system_prompt,
                user_target=user_target, user_target_node=user_target_node,
                assistant_target=assistant_target,
                api_key=self.api_key, model=self.model, owner=self,
            )
            try:
                await sess.start()
            except Exception as e:
                log.exception("session start failed")
                try:
                    await sess.stop()
                except Exception:
                    pass
                return {"error": "session_start_failed", "detail": str(e)[:300]}

            self.session = sess
            self._set_status("listening" if sess.input_device_ok else "idle")
            await self.push()
            return {
                "session_id": sess.id,
                "voice": sess.voice,
                "input_device_ok": sess.input_device_ok,
                "output_device_ok": sess.output_device_ok,
            }

    async def stop_session(self, env: dict) -> dict:
        async with self._lock:
            sess = self.session
            if sess is None:
                return {"stopped": False, "reason": "no_active_session"}
            self.session = None
            try:
                await sess.stop()
            except Exception:
                log.exception("stop_session: stop raised")
            self._set_status("idle")
            await self.push()
            return {"stopped": True, "session_id": sess.id}

    async def say(self, env: dict) -> dict:
        if not self.api_key:
            return {"error": "openai_key_missing", "detail": "set OPENAI_API_KEY"}
        text = (env.get("payload", {}) or {}).get("text")
        if not isinstance(text, str) or not text.strip():
            return {"error": "missing_text"}
        sess = self.session
        if sess is None:
            return {"error": "no_active_session", "detail": "call start_session first"}
        try:
            await sess.say(text)
            self._record("assistant", text)
            self._set_status("speaking")
            return {"spoken": True, "session_id": sess.id}
        except Exception as e:
            log.exception("say failed")
            return {"error": "say_failed", "detail": str(e)[:300]}

    async def session_status(self, env: dict) -> dict:
        s = self.session
        return {
            "active": s is not None,
            "key_present": bool(self.api_key),
            "status": self.status,
            "session_id": s.id if s else None,
            "voice": s.voice if s else None,
            "started_at": s.started_at if s else None,
            "last_user_transcript": s.last_user_transcript if s else None,
            "last_assistant_transcript": s.last_assistant_transcript if s else None,
        }


# ---------------- web app ----------------

def make_web_app(va: VoiceActor, visibility: VisibilityState) -> web.Application:
    app = web.Application(middlewares=[make_visibility_middleware(visibility)])

    async def index(request: web.Request) -> web.Response:
        return web.Response(text=HTML_PATH.read_text(), content_type="text/html")

    async def state(request: web.Request) -> web.Response:
        return web.json_response(va.state())

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(status=200, headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
        })
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        va.subscribers.add(queue)
        try:
            await response.write(f"event: state\ndata: {json.dumps(va.state())}\n\n".encode())
            while True:
                try:
                    snap = await asyncio.wait_for(queue.get(), timeout=2)
                except asyncio.TimeoutError:
                    # heartbeat AND a fresh snapshot so the mic meter ticks
                    snap = va.state()
                try:
                    await response.write(f"event: state\ndata: {json.dumps(snap)}\n\n".encode())
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            va.subscribers.discard(queue)
        return response

    async def http_start(request: web.Request) -> web.Response:
        body = await request.json()
        env = {"payload": body}
        result = await va.start_session(env)
        return web.json_response(result)

    async def http_stop(request: web.Request) -> web.Response:
        result = await va.stop_session({"payload": {}})
        return web.json_response(result)

    async def http_say(request: web.Request) -> web.Response:
        body = await request.json()
        result = await va.say({"payload": body})
        return web.json_response(result)

    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    app.router.add_post("/api/start", http_start)
    app.router.add_post("/api/stop", http_stop)
    app.router.add_post("/api/say", http_say)
    return app


# ---------------- runtime entry ----------------

async def run(node_id: str, secret: str, core_url: str,
              web_host: str, web_port: int) -> int:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        log.warning("OPENAI_API_KEY not set — node will register but tools will fail")

    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    visibility = VisibilityState(visible=True)
    va = VoiceActor(node, api_key=api_key, model=DEFAULT_MODEL)

    node.on("start_session", va.start_session)
    node.on("stop_session", va.stop_session)
    node.on("say", va.say)
    node.on("session_status", va.session_status)
    node.on("ui_visibility", make_visibility_handler(visibility, node_id=node_id, core_url=core_url))

    await node.start()
    await report_status(node_id, visibility.visible, core_url=core_url)

    web_app = make_web_app(va, visibility)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, web_host, web_port)
    await site.start()

    print(f"[{node_id}] voice_actor ready. inspector: http://{web_host}:{web_port}", flush=True)
    if not api_key:
        print(f"[{node_id}] OPENAI_API_KEY missing — set it then call start_session.", flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

    if va.session is not None:
        try:
            await va.session.stop()
        except Exception:
            pass
    await runner.cleanup()
    await node.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default="voice_actor")
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--web-host", default=os.environ.get("VOICE_ACTOR_HOST", "127.0.0.1"))
    p.add_argument("--web-port", type=int, default=int(os.environ.get("VOICE_ACTOR_PORT", "8807")))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    return asyncio.run(run(args.node_id, secret, args.core_url, args.web_host, args.web_port))


if __name__ == "__main__":
    sys.exit(main())
