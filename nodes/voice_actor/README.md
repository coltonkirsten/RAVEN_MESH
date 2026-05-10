# voice_actor

A bidirectional voice mesh node backed by OpenAI's `gpt-realtime-2`.
It opens a Realtime API WebSocket session, plays the model's audio out
of the Mac's speakers, captures the mic, and forwards the user's
transcribed speech to whichever mesh target you configure.

## Install deps

There's no `requirements.txt` in the repo yet. Install the new deps directly:

```bash
pip install openai sounddevice numpy aiohttp
```

(`aiohttp` and `pyyaml` are already required by the rest of the mesh.)

`sounddevice` wraps PortAudio. On macOS the wheel is self-contained.
The first time the process opens the mic, macOS will prompt for
microphone permission — grant it once and you're done.

## Set the API key

```bash
export OPENAI_API_KEY=sk-...
```

If the key is missing the node still registers with the mesh and the
inspector loads — every tool call returns a clear `openai_key_missing`
error envelope. This makes the demo bootable even on machines that
haven't been keyed yet.

## Run

```bash
# Standalone:
scripts/run_voice_actor.sh

# Or full demo with nexus_agent + webui + human:
scripts/run_mesh.sh manifests/voice_actor_demo.yaml
```

Inspector: <http://localhost:8807>

## Tool surfaces

| surface                       | input                                                       | returns                              |
| ----------------------------- | ----------------------------------------------------------- | ------------------------------------ |
| `voice_actor.start_session`   | `{voice?, system_prompt?, on_user_transcript_target?, on_user_transcript_node?, on_assistant_transcript_target?}` | `{session_id, voice, ...}` |
| `voice_actor.stop_session`    | `{}`                                                        | `{stopped, session_id?}`             |
| `voice_actor.say`             | `{text}`                                                    | `{spoken}`                           |
| `voice_actor.session_status`  | `{}`                                                        | `{active, status, session_id?, ...}` |
| `voice_actor.ui_visibility`   | `{action: "show"|"hide"}`                                   | `{ok, visible}`                      |

Only one Realtime session is active at a time. Calling `start_session`
again will tear down the existing one and replace it.

## Conversation flow

1. An agent (e.g. `nexus_agent`) calls `voice_actor.start_session` with
   `on_user_transcript_node: "nexus_agent"`.
2. The node opens `wss://api.openai.com/v1/realtime?model=gpt-realtime-2`,
   sends a `session.update` configuring 24 kHz PCM16 audio in/out, the
   chosen voice, optional system prompt, and `whisper-1` for input
   transcription with `server_vad` for turn detection.
3. The mic streams `input_audio_buffer.append` events continuously.
   Server VAD detects speech endpoints and auto-commits.
4. When `conversation.item.input_audio_transcription.completed` arrives
   with the user's words, the node fire-and-forgets a payload to
   `nexus_agent.inbox`:

   ```json
   {
     "from": "voice_actor",
     "kind": "voice_transcript",
     "role": "user",
     "text": "what should I focus on this week?",
     "session_id": "vs_3f9a...",
     "timestamp": "2026-05-09T17:22:01.123+00:00"
   }
   ```

5. The model also produces audio (`response.audio.delta` / `response.output_audio.delta`)
   that gets queued straight into the speaker stream, plus an
   assistant transcript (`response.audio_transcript.done`) that's
   forwarded to `on_assistant_transcript_target` if configured.
6. The agent can intersperse precise lines via `voice_actor.say`
   (out-of-band response with explicit instructions) or close out the
   session with `voice_actor.stop_session`.

## Sample agent invocation

```python
await node.invoke("voice_actor.start_session", {
    "voice": "alloy",
    "system_prompt": "You are RAVEN, terse and direct.",
    "on_user_transcript_node": "nexus_agent",
})
```

## Manual smoke test

With `OPENAI_API_KEY` set and the mesh running with
`manifests/voice_actor_demo.yaml`:

```bash
# Open a session with the inspector UI:
open http://localhost:8807
#   click Start session → speak into the mic → hear the model reply

# Or drive it from the shell via the inspector's HTTP wrapper:
curl -X POST http://localhost:8807/api/start \
  -H 'content-type: application/json' \
  -d '{"voice":"alloy","on_user_transcript_node":"nexus_agent"}'

# Force the model to speak a precise line:
curl -X POST http://localhost:8807/api/say \
  -H 'content-type: application/json' \
  -d '{"text":"Hello Colton, RAVEN here."}'

# Close the session:
curl -X POST http://localhost:8807/api/stop
```

User-speech transcripts will land in `nexus_agent.inbox` — check the
agent's logs (e.g. `nodes/nexus_agent/data/logs/`) to see them roll in.

## Robustness notes

- Missing `OPENAI_API_KEY` → tool calls return
  `{"error":"openai_key_missing","detail":"set OPENAI_API_KEY"}`.
- No mic device (e.g. headless host) → mic capture fails to start; the
  session still opens and `voice_actor.say` works for output-only.
- WebSocket drop mid-session → the session is marked inactive, status
  flips to `idle`, and the next `start_session` opens a fresh one. We
  do NOT auto-reconnect — the calling agent decides.
- Audio buffer underruns or overruns → logged at debug, never crash.
- Tests do NOT hit the OpenAI API — they monkey-patch the audio classes
  and only exercise registration, schemas, and graceful-degradation
  error envelopes.

## Files

| path                                         | purpose                                  |
| -------------------------------------------- | ---------------------------------------- |
| `nodes/voice_actor/voice_actor.py`           | mesh node + inspector aiohttp app        |
| `nodes/voice_actor/realtime_client.py`       | aiohttp WebSocket wrapper for Realtime   |
| `nodes/voice_actor/audio_io.py`              | sounddevice mic capture + speaker queue  |
| `nodes/voice_actor/index.html`               | inspector dashboard (vanilla JS + SSE)   |
| `schemas/voice_*.json`                       | tool surface schemas                     |
| `manifests/voice_actor_demo.yaml`            | demo wiring                              |
| `scripts/run_voice_actor.sh`                 | run script                               |
| `tests/test_voice_actor.py`                  | offline tests (no API calls)             |
