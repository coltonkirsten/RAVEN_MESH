"""mesh_chronicle — causal-chain time-travel debugger node.

A normal mesh node (registers, signs envelopes, exposes surfaces) plus a
small embedded HTTP server for the human-facing inspector UI.

Surfaces (all callable through the normal mesh path):

    chronicle.list_chains    -> recent captured causal chains, with filters
    chronicle.get_chain      -> full envelope tree for a correlation_id
    chronicle.replay         -> re-invoke one captured invocation; returns new response
    chronicle.replay_chain   -> re-invoke every invocation in the chain
    chronicle.replay_diff    -> replay + diff against original response
    chronicle.schema_compat  -> which captured payloads now fail current schemas
    chronicle.reverify       -> recompute HMAC over captured envelopes

The inspector at http://127.0.0.1:9100/inspector wraps these surfaces in
a single-page UI for humans. The UI itself talks to the chronicle over
plain HTTP because it's a sibling-process tool, not a mesh participant.

Opinionated layer end-to-end. Does not modify core/, node_sdk/, or any
schema in /schemas/. Uses the v0 admin surface (admin/stream and
admin/invoke) as-is.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import signal
import sys
from typing import Any

from aiohttp import web

# Repo root is two levels up from this file's parent.
_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from node_sdk import MeshNode, MeshDeny  # noqa: E402

from mesh_chronicle.recorder import Recorder  # noqa: E402
from mesh_chronicle.replayer import Replayer  # noqa: E402
from mesh_chronicle.differ import diff as payload_diff  # noqa: E402

log = logging.getLogger("chronicle.node")


# ---------- mesh surface handlers ----------

class ChronicleSurfaces:
    def __init__(self, recorder: Recorder, replayer: Replayer):
        self.recorder = recorder
        self.replayer = replayer

    async def list_chains(self, env: dict) -> dict:
        p = env.get("payload", {})
        chains = self.recorder.list_chains(
            limit=int(p.get("limit", 50)),
            offset=int(p.get("offset", 0)),
            from_node=p.get("from_node"),
            to_surface=p.get("to_surface"),
            status=p.get("status"),
        )
        return {"chains": chains, "total_known": len(self.recorder.chains)}

    async def get_chain(self, env: dict) -> dict:
        cid = env.get("payload", {}).get("correlation_id")
        if not cid:
            raise MeshDeny("missing_correlation_id")
        chain = self.recorder.get_chain(cid)
        if chain is None:
            raise MeshDeny("unknown_chain", correlation_id=cid)
        return chain

    async def replay(self, env: dict) -> dict:
        cid = env.get("payload", {}).get("correlation_id")
        msg_id = env.get("payload", {}).get("msg_id")
        if not cid:
            raise MeshDeny("missing_correlation_id")
        chain = self.recorder.get_chain(cid)
        if chain is None:
            raise MeshDeny("unknown_chain", correlation_id=cid)
        target_evt = None
        for evt in chain["events"]:
            if evt.get("kind") != "invocation":
                continue
            if evt.get("direction") != "in":
                continue
            if msg_id and evt.get("msg_id") != msg_id:
                continue
            target_evt = evt
            break
        if target_evt is None:
            raise MeshDeny("no_invocation_in_chain")
        result = await self.replayer.replay_one(target_evt)
        return result

    async def replay_chain(self, env: dict) -> dict:
        cid = env.get("payload", {}).get("correlation_id")
        if not cid:
            raise MeshDeny("missing_correlation_id")
        chain = self.recorder.get_chain(cid)
        if chain is None:
            raise MeshDeny("unknown_chain", correlation_id=cid)
        return await self.replayer.replay_chain(chain)

    async def replay_diff(self, env: dict) -> dict:
        cid = env.get("payload", {}).get("correlation_id")
        msg_id = env.get("payload", {}).get("msg_id")
        if not cid:
            raise MeshDeny("missing_correlation_id")
        chain = self.recorder.get_chain(cid)
        if chain is None:
            raise MeshDeny("unknown_chain", correlation_id=cid)
        target_evt = None
        original_response = None
        for evt in chain["events"]:
            if (evt.get("kind") == "invocation"
                    and evt.get("direction") == "in"
                    and (not msg_id or evt.get("msg_id") == msg_id)):
                target_evt = evt
            if (evt.get("kind") == "response"
                    and target_evt is not None
                    and evt.get("correlation_id") == target_evt.get("correlation_id")):
                original_response = evt.get("payload")
        if target_evt is None:
            raise MeshDeny("no_invocation_in_chain")
        replay = await self.replayer.replay_one(target_evt)
        new_response_env = replay.get("response") or {}
        # admin/invoke returns either a response *envelope* (200) or an error
        # body. Pull payload when present, fall back to the whole body so the
        # diff still surfaces the regression.
        if isinstance(new_response_env, dict) and "payload" in new_response_env:
            new_response = new_response_env["payload"]
        else:
            new_response = new_response_env
        diffs = payload_diff(original_response, new_response)
        return {
            "captured_msg_id": target_evt.get("msg_id"),
            "target": target_evt.get("to_surface"),
            "original_response": original_response,
            "replayed_response": new_response,
            "replay_status": replay.get("status"),
            "diffs": diffs,
            "diverged": bool(diffs),
        }

    async def schema_compat(self, env: dict) -> dict:
        cids = env.get("payload", {}).get("correlation_ids")
        if cids:
            chains = []
            for cid in cids:
                ch = self.recorder.get_chain(cid)
                if ch is not None:
                    chains.append(ch)
        else:
            limit = int(env.get("payload", {}).get("limit", 50))
            chains = []
            for s in self.recorder.list_chains(limit=limit):
                ch = self.recorder.get_chain(s["correlation_id"])
                if ch is not None:
                    chains.append(ch)
        return await self.replayer.schema_compat(chains)

    async def reverify(self, env: dict) -> dict:
        cid = env.get("payload", {}).get("correlation_id")
        if not cid:
            raise MeshDeny("missing_correlation_id")
        # Pull current secrets via /v0/admin/state — the replayer already
        # has a session for this. State doesn't return raw secrets (good!),
        # so reverify only succeeds when the chronicle was also told the
        # secrets out of band (CHRONICLE_SECRETS_JSON env). When absent,
        # we report that signature_valid was true at capture time.
        env_secrets = os.environ.get("CHRONICLE_SECRETS_JSON")
        secrets: dict[str, str] = {}
        if env_secrets:
            try:
                secrets = json.loads(env_secrets)
            except json.JSONDecodeError:
                pass
        return self.recorder.reverify_chain(cid, secrets)


# ---------- inspector HTTP server ----------

def _make_inspector(surfaces: ChronicleSurfaces, recorder: Recorder) -> web.Application:
    app = web.Application()

    async def index(_req: web.Request) -> web.Response:
        index_path = _HERE.parent / "web" / "index.html"
        return web.Response(
            body=index_path.read_text().encode(),
            content_type="text/html",
        )

    async def api_list(req: web.Request) -> web.Response:
        from_node = req.query.get("from_node") or None
        to_surface = req.query.get("to_surface") or None
        status = req.query.get("status") or None
        chains = recorder.list_chains(
            from_node=from_node, to_surface=to_surface, status=status,
            limit=int(req.query.get("limit", "100")),
        )
        return web.json_response({
            "connected": recorder.connected,
            "total_known": len(recorder.chains),
            "chains": chains,
        })

    async def api_chain(req: web.Request) -> web.Response:
        cid = req.match_info["cid"]
        chain = recorder.get_chain(cid)
        if chain is None:
            return web.json_response({"error": "unknown_chain"}, status=404)
        return web.json_response(chain)

    async def api_replay(req: web.Request) -> web.Response:
        body = await req.json()
        result = await surfaces.replay({"payload": body})
        return web.json_response(result)

    async def api_replay_diff(req: web.Request) -> web.Response:
        body = await req.json()
        result = await surfaces.replay_diff({"payload": body})
        return web.json_response(result)

    async def api_schema_compat(req: web.Request) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        result = await surfaces.schema_compat({"payload": body})
        return web.json_response(result)

    app.router.add_get("/inspector", index)
    app.router.add_get("/api/chains", api_list)
    app.router.add_get("/api/chains/{cid}", api_chain)
    app.router.add_post("/api/replay", api_replay)
    app.router.add_post("/api/replay_diff", api_replay_diff)
    app.router.add_post("/api/schema_compat", api_schema_compat)
    return app


# ---------- main ----------

async def run(node_id: str, secret: str, core_url: str, admin_token: str,
              store_path: str, inspector_host: str, inspector_port: int) -> int:
    recorder = Recorder(core_url=core_url, admin_token=admin_token,
                        store_path=store_path)
    replayer = Replayer(core_url=core_url, admin_token=admin_token)
    surfaces = ChronicleSurfaces(recorder, replayer)
    await recorder.start()
    await replayer.start()

    node = MeshNode(node_id=node_id, secret=secret, core_url=core_url)
    await node.connect()
    handlers = {
        "list_chains": surfaces.list_chains,
        "get_chain": surfaces.get_chain,
        "replay": surfaces.replay,
        "replay_chain": surfaces.replay_chain,
        "replay_diff": surfaces.replay_diff,
        "schema_compat": surfaces.schema_compat,
        "reverify": surfaces.reverify,
    }
    declared = {s["name"] for s in node.surfaces}
    for name, handler in handlers.items():
        if name in declared:
            node.on(name, handler)
    await node.serve()

    inspector_app = _make_inspector(surfaces, recorder)
    inspector_runner = web.AppRunner(inspector_app)
    await inspector_runner.setup()
    site = web.TCPSite(inspector_runner, inspector_host, inspector_port)
    await site.start()

    print(f"[{node_id}] chronicle ready. surfaces={sorted(declared)} "
          f"inspector=http://{inspector_host}:{inspector_port}/inspector",
          flush=True)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    await inspector_runner.cleanup()
    await node.stop()
    await replayer.stop()
    await recorder.stop()
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--node-id", default=os.environ.get("CHRONICLE_NODE_ID", "mesh_chronicle"))
    p.add_argument("--secret-env", default=None)
    p.add_argument("--core-url", default=os.environ.get("MESH_CORE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--admin-token-env", default="ADMIN_TOKEN")
    p.add_argument("--store",
                   default=os.environ.get("CHRONICLE_STORE", ".chronicle/recordings.jsonl"))
    p.add_argument("--inspector-host", default=os.environ.get("CHRONICLE_INSPECTOR_HOST", "127.0.0.1"))
    p.add_argument("--inspector-port", type=int,
                   default=int(os.environ.get("CHRONICLE_INSPECTOR_PORT", "9100")))
    args = p.parse_args()
    secret_env = args.secret_env or f"{args.node_id.upper()}_SECRET"
    secret = os.environ.get(secret_env)
    if not secret:
        print(f"missing env var {secret_env}", file=sys.stderr)
        return 2
    admin_token = os.environ.get(args.admin_token_env, "admin-dev-token")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(
        args.node_id, secret, args.core_url, admin_token,
        args.store, args.inspector_host, args.inspector_port,
    ))


if __name__ == "__main__":
    sys.exit(main())
