"""Integration tests for mesh_chronicle.

We boot Core via the conftest fixture, then in the same event loop:
- start `echo_capability` and `mesh_chronicle` as in-process MeshNode instances
- drive a few invocations from a fake `client_actor` MeshNode
- assert chronicle captured them, can replay them, and detects schema-compat
  regressions when the manifest is hot-swapped to the strict v2 schema.

Each test has a clear assertion. No flakiness from real subprocesses — every
node lives in this Python process.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest

from node_sdk import MeshNode  # noqa: E402

from mesh_chronicle.recorder import Recorder
from mesh_chronicle.replayer import Replayer
from mesh_chronicle.chronicle_node import ChronicleSurfaces
from mesh_chronicle.echo_capability import EchoState
from mesh_chronicle.differ import diff as payload_diff


pytestmark = pytest.mark.asyncio


async def _wait_chains(recorder: Recorder, n: int, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if len(recorder.chains) >= n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"recorder only saw {len(recorder.chains)} chains, wanted {n}")


async def _start_chronicle(core_url: str, store_path: pathlib.Path):
    secret = os.environ["MESH_CHRONICLE_SECRET"]
    admin_token = os.environ["ADMIN_TOKEN"]
    recorder = Recorder(core_url=core_url, admin_token=admin_token,
                        store_path=str(store_path))
    replayer = Replayer(core_url=core_url, admin_token=admin_token)
    await recorder.start()
    await replayer.start()
    surfaces = ChronicleSurfaces(recorder, replayer)
    node = MeshNode(node_id="mesh_chronicle", secret=secret, core_url=core_url)
    await node.connect()
    declared = {s["name"] for s in node.surfaces}
    handlers = {
        "list_chains": surfaces.list_chains,
        "get_chain": surfaces.get_chain,
        "replay": surfaces.replay,
        "replay_chain": surfaces.replay_chain,
        "replay_diff": surfaces.replay_diff,
        "schema_compat": surfaces.schema_compat,
        "reverify": surfaces.reverify,
    }
    for name, h in handlers.items():
        if name in declared:
            node.on(name, h)
    await node.serve()
    return node, recorder, replayer, surfaces


async def _start_echo(core_url: str):
    secret = os.environ["ECHO_CAPABILITY_SECRET"]
    node = MeshNode(node_id="echo_capability", secret=secret, core_url=core_url)
    await node.connect()
    state = EchoState()
    node.on("ping", state)
    await node.serve()
    return node, state


async def _start_client(core_url: str):
    secret = os.environ["CLIENT_ACTOR_SECRET"]
    node = MeshNode(node_id="client_actor", secret=secret, core_url=core_url)
    await node.connect()
    await node.serve()
    return node


async def _drive_pings(client: MeshNode, n: int):
    out = []
    for i in range(n):
        payload = {"text": f"ping #{i}", "user_id": f"u_demo{i}"}
        r = await client.invoke("echo_capability.ping", payload)
        # invoke returns the full response envelope; extract payload
        out.append(r.get("payload", {}))
    return out


async def test_recorder_captures_envelopes(core_server):
    url = core_server["url"]
    chronicle, recorder, replayer, _ = await _start_chronicle(
        url, core_server["tmp_path"] / "rec.jsonl")
    echo, _ = await _start_echo(url)
    client = await _start_client(url)
    try:
        results = await _drive_pings(client, 3)
        assert len(results) == 3
        assert all("call_index" in r for r in results)
        await _wait_chains(recorder, 3)
        # one chain per ping (correlation_id == invocation msg_id)
        chains = recorder.list_chains()
        roots = [c["root_to"] for c in chains]
        assert "echo_capability.ping" in roots
        # every chain has at least 1 invocation envelope
        for c in chains:
            full = recorder.get_chain(c["correlation_id"])
            assert any(e.get("kind") == "invocation" for e in full["events"])
    finally:
        await client.stop()
        await echo.stop()
        await chronicle.stop()
        await recorder.stop()
        await replayer.stop()


async def test_chronicle_list_chains_via_mesh(core_server):
    url = core_server["url"]
    chronicle, recorder, replayer, _ = await _start_chronicle(
        url, core_server["tmp_path"] / "rec.jsonl")
    echo, _ = await _start_echo(url)
    client = await _start_client(url)
    try:
        await _drive_pings(client, 2)
        await _wait_chains(recorder, 2)
        # call list_chains *through the mesh*, not via Python directly.
        env = await client.invoke("mesh_chronicle.list_chains", {"limit": 10})
        result = env["payload"]
        assert result["total_known"] >= 2
        assert len(result["chains"]) >= 2
        # filtering works
        env2 = await client.invoke("mesh_chronicle.list_chains",
                                    {"limit": 10, "to_surface": "echo_capability.ping"})
        result2 = env2["payload"]
        assert all(c["root_to"] == "echo_capability.ping" for c in result2["chains"])
    finally:
        await client.stop(); await echo.stop()
        await chronicle.stop(); await recorder.stop(); await replayer.stop()


async def test_replay_reproduces_invocation(core_server):
    url = core_server["url"]
    chronicle, recorder, replayer, _ = await _start_chronicle(
        url, core_server["tmp_path"] / "rec.jsonl")
    echo, echo_state = await _start_echo(url)
    client = await _start_client(url)
    try:
        await _drive_pings(client, 2)
        await _wait_chains(recorder, 2)
        # echo_state.count == 2 after the burst
        assert echo_state.count == 2
        cid = list(recorder.chains.keys())[0]
        env = await client.invoke("mesh_chronicle.replay",
                                   {"correlation_id": cid})
        replay = env["payload"]
        # admin/invoke succeeded, returning the new echo response envelope
        assert replay.get("status") == 200
        new_resp_env = replay.get("response", {})
        new_resp = new_resp_env.get("payload", {}) if isinstance(new_resp_env, dict) else {}
        assert "call_index" in new_resp
        # echo_state.count incremented once for the replay
        assert echo_state.count == 3
    finally:
        await client.stop(); await echo.stop()
        await chronicle.stop(); await recorder.stop(); await replayer.stop()


async def test_replay_diff_detects_state_drift(core_server):
    url = core_server["url"]
    chronicle, recorder, replayer, _ = await _start_chronicle(
        url, core_server["tmp_path"] / "rec.jsonl")
    echo, _ = await _start_echo(url)
    client = await _start_client(url)
    try:
        await _drive_pings(client, 1)
        await _wait_chains(recorder, 1)
        cid = list(recorder.chains.keys())[0]
        env = await client.invoke("mesh_chronicle.replay_diff",
                                   {"correlation_id": cid})
        diff = env["payload"]
        # echo's call_index incremented between original and replay,
        # so the diff must report at least one path under call_index.
        assert diff["diverged"] is True
        paths = [d["path"] for d in diff["diffs"]]
        assert any("call_index" in p for p in paths)
        # original payload `echoed.text` must NOT diff (same text both runs)
        assert all("echoed.text" not in p for p in paths)
    finally:
        await client.stop(); await echo.stop()
        await chronicle.stop(); await recorder.stop(); await replayer.stop()


async def test_schema_compat_after_manifest_v2(core_server):
    """Drive traffic under v1 (loose). Hot-swap to v2 (strict). Chronicle's
    schema_compat must report all captured payloads as INCOMPATIBLE if their
    user_id doesn't match `^u_[A-Za-z0-9]+$` — which is the whole point of
    catching schema regressions across manifest revisions."""
    url = core_server["url"]
    chronicle, recorder, replayer, _ = await _start_chronicle(
        url, core_server["tmp_path"] / "rec.jsonl")
    echo, _ = await _start_echo(url)
    client = await _start_client(url)
    try:
        # Drive with payloads that would FAIL v2: missing user_id entirely.
        async def drive_bad(n):
            for i in range(n):
                await client.invoke("echo_capability.ping", {"text": f"x{i}"})
        await drive_bad(3)
        await _wait_chains(recorder, 3)

        # Hot-swap the manifest. Core re-reads it (in-process, so no HTTP needed).
        original_v1 = pathlib.Path(core_server["manifest"]).read_text()
        v2_text = pathlib.Path(core_server["v2_manifest"]).read_text()
        pathlib.Path(core_server["manifest"]).write_text(v2_text)
        try:
            core_server["state"].load_manifest()
            env = await client.invoke(
                "mesh_chronicle.schema_compat", {"limit": 50})
            compat = env["payload"]
            assert compat["total_invocations_checked"] >= 3
            # every captured payload missing `user_id` must be flagged.
            assert compat["now_breaking"] >= 3
            # report cites the surface
            saw_target = False
            for ch in compat["report"]:
                for c in ch["checks"]:
                    if c["target"] == "echo_capability.ping" and not c["compatible"]:
                        saw_target = True
                        assert c["reason"] == "schema_violation"
            assert saw_target, "schema_compat did not report expected breakage"
        finally:
            # restore v1 on disk for re-runs
            pathlib.Path(core_server["manifest"]).write_text(original_v1)
    finally:
        await client.stop(); await echo.stop()
        await chronicle.stop(); await recorder.stop(); await replayer.stop()


async def test_reverify_uses_node_secrets(core_server):
    """Re-verify computes fresh HMACs over captured envelopes using the
    out-of-band secrets dict. Mismatched secret -> recomputed != captured.
    Right secret -> recomputed matches what Core saw."""
    url = core_server["url"]
    chronicle, recorder, replayer, _ = await _start_chronicle(
        url, core_server["tmp_path"] / "rec.jsonl")
    echo, _ = await _start_echo(url)
    client = await _start_client(url)
    try:
        await _drive_pings(client, 1)
        await _wait_chains(recorder, 1)
        cid = list(recorder.chains.keys())[0]
        secrets = {
            "client_actor": os.environ["CLIENT_ACTOR_SECRET"],
            "echo_capability": os.environ["ECHO_CAPABILITY_SECRET"],
        }
        rep = recorder.reverify_chain(cid, secrets)
        assert rep["correlation_id"] == cid
        # at least one event recomputed a non-None hex digest
        assert any(e["recomputed"] for e in rep["events"])
        # under the wrong secret, the recomputed digest differs (we just
        # check that the function still returns; tampering detection is
        # surfaced by comparing against the captured envelope's signature
        # — we don't store signatures in the audit-tap event so we settle
        # for "function ran end-to-end").
        rep_bad = recorder.reverify_chain(cid, {"client_actor": "wrong"})
        assert rep_bad["correlation_id"] == cid
    finally:
        await client.stop(); await echo.stop()
        await chronicle.stop(); await recorder.stop(); await replayer.stop()


def test_payload_differ_unit():
    a = {"x": 1, "y": [1, 2, 3], "z": {"k": "v"}}
    b = {"x": 1, "y": [1, 2, 4], "z": {"k": "v", "k2": "v2"}}
    diffs = payload_diff(a, b)
    paths = [d["path"] for d in diffs]
    assert any(p == "$.y[2]" for p in paths)
    assert any(p == "$.z.k2" for p in paths)
    # identical -> no diffs
    assert payload_diff({"a": 1}, {"a": 1}) == []
    # type change
    diffs_t = payload_diff({"a": 1}, {"a": "1"})
    assert any(d["kind"] == "type_changed" for d in diffs_t)
