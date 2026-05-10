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

from .audio_io import (AudioUnavailable, MicCapture, SpeakerPlayback,
                       check_devices, list_devices)
from .realtime_client import RealtimeClient, decode_audio_delta

log = logging.getLogger("voice_actor")
HTML_PATH = pathlib.Path(__file__).resolve().parent / "index.html"

DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = os.environ.get("VOICE_ACTOR_MODEL", "gpt-realtime-2")
DEFAULT_INPUT_DEVICE = os.environ.get("VOICE_ACTOR_INPUT_DEVICE")  # name substring or int
DEFAULT_OUTPUT_DEVICE = os.environ.get("VOICE_ACTOR_OUTPUT_DEVICE")
TRANSCRIPT_MAX = 100
METER_PUSH_HZ = 20  # state push rate while a session is live, for UI visualizer


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class Session:
    """One live Realtime API session + audio I/O loop."""

    def __init__(self, *, voice: str, system_prompt: Optional[str],
                 user_target: Optional[str], user_target_node: Optional[str],
                 assistant_target: Optional[str], api_key: str, model: str,
                 owner: "VoiceActor",
                 input_device: object = None,
                 output_device: object = None) -> None:
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
        # Per-session override > env-var default > auto-pick.
        in_dev = input_device if input_device not in (None, "") else DEFAULT_INPUT_DEVICE
        if isinstance(in_dev, str) and in_dev.lstrip("-").isdigit():
            in_dev = int(in_dev)
        out_dev = output_device if output_device not in (None, "") else DEFAULT_OUTPUT_DEVICE
        if isinstance(out_dev, str) and out_dev.lstrip("-").isdigit():
            out_dev = int(out_dev)
        self.mic = MicCapture(device=in_dev)
        self.spk = SpeakerPlayback(device=out_dev)

        self._mic_task: Optional[asyncio.Task] = None
        self._evt_task: Optional[asyncio.Task] = None
        self._meter_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()
        # name → {target_surface, mode, kind} — used to dispatch tool calls
        self.mesh_targets: dict = {}
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

        # Introspect the mesh once at session start so the model knows what it
        # can reach, and register one Realtime function-call tool per edge.
        self.mesh_targets, tools = await self._build_mesh_tools()
        instructions = self._build_instructions(self.mesh_targets)

        # GA Realtime API session schema (nested under audio.input / audio.output).
        cfg: dict = {
            "type": "realtime",
            "model": self.client.model,
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "gpt-4o-transcribe"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": self.voice,
                },
            },
        }
        if tools:
            cfg["tools"] = tools
            cfg["tool_choice"] = "auto"
        self.client.session_config = cfg
        await self.client.connect()

        self._evt_task = asyncio.create_task(self._event_loop())
        if self.input_device_ok:
            self._mic_task = asyncio.create_task(self._mic_loop())
        self._meter_task = asyncio.create_task(self._meter_loop())

    async def stop(self) -> None:
        self._stopped.set()
        try:
            await self.client.close()
        except Exception:
            pass
        for t in (self._mic_task, self._evt_task, self._meter_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._mic_task = None
        self._evt_task = None
        self._meter_task = None
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

    # ------------------- mesh tool integration -------------------

    async def _build_mesh_tools(self) -> tuple[dict, list[dict]]:
        """Query the mesh and build Realtime function tools for each edge.

        Returns ({tool_name: target_info}, [tool_defs_for_realtime_session]).
        """
        node = self._owner.node
        try:
            assert node._http is not None
            async with node._http.get(f"{node.core_url}/v0/introspect") as r:
                data = await r.json()
        except Exception as e:
            log.warning("introspect failed; voice agent will run tool-less: %s", e)
            return {}, []

        node_index = {n["id"]: n for n in data.get("nodes", [])}
        targets: dict = {}
        tools: list[dict] = []
        for edge in data.get("relationships", []):
            if edge.get("from") != node.node_id:
                continue
            target = edge.get("to", "")
            target_node, _, surface_name = target.partition(".")
            # skip self-edges
            if target_node == node.node_id:
                continue
            ndecl = node_index.get(target_node, {})
            sdecl = next(
                (s for s in ndecl.get("surfaces", []) if s["name"] == surface_name),
                {},
            )
            stype = sdecl.get("type")
            mode = sdecl.get("invocation_mode")
            # Only inbox-style hand-offs and request_response tools are useful
            # to expose to a voice model. ui_visibility etc are noise.
            if stype == "inbox":
                tool_name = f"send_to_{target_node}"
                desc = (
                    f"Send a free-form text message to {target_node}'s inbox "
                    f"({ndecl.get('kind','node')}). Fire-and-forget — no reply. "
                    f"Use this to hand off a task that needs reasoning, code, "
                    f"or external action beyond what you can do as the voice."
                )
                params = {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string",
                                 "description": "Message body — phrase it as a task or question."},
                    },
                    "required": ["text"],
                }
            elif stype == "tool" and mode == "request_response":
                # Skip ui_visibility — internal harness control.
                if surface_name == "ui_visibility":
                    continue
                tool_name = f"{target_node}__{surface_name}".replace(".", "_")
                desc = (
                    f"Invoke the {target_node}.{surface_name} tool surface and "
                    f"return its response. Use only when the user explicitly "
                    f"asks for that capability."
                )
                params = {
                    "type": "object",
                    "properties": {
                        "payload": {"type": "object",
                                    "description": "Surface input. Match the surface's expected schema."},
                    },
                    "required": ["payload"],
                }
            else:
                continue

            targets[tool_name] = {"target": target, "mode": mode, "type": stype,
                                  "node": target_node, "surface": surface_name}
            tools.append({"type": "function", "name": tool_name,
                          "description": desc, "parameters": params})
        return targets, tools

    def _build_instructions(self, targets: dict) -> str:
        node_id = self._owner.node.node_id
        lines = [
            f"You are voice_actor, the voice surface of a node in the RAVEN Mesh "
            f"(node id: {node_id}). The user speaks into a microphone and hears "
            f"your replies through speakers. Talk naturally, like a fluent assistant — "
            f"concise, friendly, and direct. Do not narrate what you are doing.",
            "",
            "You are part of a larger system of cooperating nodes. Other nodes can "
            "do things you cannot — run code, edit files, call APIs, drive UIs, ask "
            "humans for approval. When the user asks for something that needs more "
            "than conversation, hand it off to the right node by calling the matching "
            "tool below. After dispatching, tell the user briefly what you sent and "
            "to whom. Don't fabricate results — wait for the node to respond (it may "
            "come back as a follow-up message in this conversation).",
        ]
        if targets:
            lines.append("")
            lines.append("Available mesh tools:")
            for name, info in targets.items():
                if info["type"] == "inbox":
                    lines.append(
                        f"  - {name}(text): hand off a task to {info['node']} "
                        f"(actor; runs autonomously; no immediate reply)."
                    )
                else:
                    lines.append(
                        f"  - {name}(payload): call {info['node']}.{info['surface']} "
                        f"and use the response."
                    )
        else:
            lines.append("")
            lines.append("(No mesh tools are reachable in this session — you can only "
                         "respond conversationally.)")
        if self.system_prompt:
            lines.append("")
            lines.append("Operator-supplied instructions:")
            lines.append(self.system_prompt)
        return "\n".join(lines)

    async def _handle_function_call(self, call_id: str, name: str,
                                    arguments_json: str) -> None:
        """Dispatch a Realtime function call to the mesh and post the result back."""
        info = self.mesh_targets.get(name)
        if not info:
            output = {"error": f"unknown tool: {name}"}
        else:
            try:
                args = json.loads(arguments_json) if arguments_json else {}
            except json.JSONDecodeError:
                args = {}
            try:
                if info["type"] == "inbox":
                    payload = {
                        "from": "voice_actor",
                        "kind": "voice_handoff",
                        "text": args.get("text", ""),
                        "session_id": self.id,
                        "timestamp": now_iso(),
                    }
                    await self._owner.node.invoke(info["target"], payload, wait=False)
                    output = {"ok": True, "delivered_to": info["target"]}
                else:
                    payload = args.get("payload") or {}
                    result = await self._owner.node.invoke(info["target"], payload, wait=True)
                    output = {"ok": True, "result": result}
            except MeshError as e:
                output = {"error": True, "status": e.status, "data": e.data}
            except Exception as e:
                output = {"error": True, "message": str(e)}

        # Log it in the transcript so the operator can see what happened.
        self._owner._record("tool", f"{name} → {json.dumps(output, default=str)[:200]}")

        # Post the function_call_output item back, then ask for a follow-up
        # response so the model speaks an acknowledgement.
        try:
            await self.client._send({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, default=str),
                },
            })
            await self.client.create_response()
        except Exception as e:
            log.warning("posting function_call_output failed: %s", e)

    # ------------------- internal loops -------------------

    async def _meter_loop(self) -> None:
        # Periodically push state so the UI visualizer animates from mic_rms /
        # spk_rms. We only push if the audio levels actually changed; otherwise
        # the UI gets a flat-line at the previous value.
        period = 1.0 / METER_PUSH_HZ
        last_mic = -1.0
        last_spk = -1.0
        try:
            while not self._stopped.is_set():
                await asyncio.sleep(period)
                mic = float(getattr(self.mic, "last_rms", 0.0))
                spk = float(getattr(self.spk, "last_rms", 0.0))
                if abs(mic - last_mic) > 0.005 or abs(spk - last_spk) > 0.005:
                    last_mic, last_spk = mic, spk
                    await self._owner.push()
        except asyncio.CancelledError:
            return

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
        elif et == "response.function_call_arguments.done":
            call_id = evt.get("call_id") or ""
            name = evt.get("name") or ""
            args = evt.get("arguments") or "{}"
            log.info("function call: %s args=%s", name, args[:160])
            asyncio.create_task(self._handle_function_call(call_id, name, args))
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
                "input_device_name": getattr(s.mic, "device_name", None),
                "output_device_name": getattr(s.spk, "device_name", None),
                "error": s.error,
                "mic_rms": getattr(s.mic, "last_rms", 0.0),
                "spk_rms": getattr(s.spk, "last_rms", 0.0),
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
        input_device = body.get("input_device")
        output_device = body.get("output_device")

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
                input_device=input_device, output_device=output_device,
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

    async def http_devices(request: web.Request) -> web.Response:
        return web.json_response(list_devices())

    app.router.add_get("/", index)
    app.router.add_get("/state", state)
    app.router.add_get("/events", events)
    app.router.add_get("/api/devices", http_devices)
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
