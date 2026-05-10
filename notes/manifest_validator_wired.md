# Manifest validator wired into Core (warnings-mode)

**Date:** 2026-05-10
**Branch:** simplify-raven
**Plan ref:** notes/2026-05-10_morning_review.md §10

## Call sites wired

1. **Core startup** — `CoreState.load_manifest(source="startup")`
   (default arg). Called from `make_app`. Validator runs after the yaml is
   parsed, before nodes_decl is populated and before the listener starts.
2. **`POST /v0/admin/reload`** — `state.load_manifest(source="/v0/admin/reload")`.
   Validates the freshly-re-parsed manifest after reload.
3. **`POST /v0/admin/manifest`** — pre-write validator call on the parsed
   incoming yaml (`source="/v0/admin/manifest"`), then `state.load_manifest(...,
   validate=False)` so the same manifest isn't validated twice on every POST.

## Validator API used

`core.manifest_validator.validate_manifest(parsed, manifest_dir) -> (errors,
warnings)` — both are `list[str]`. Module already shipped from Wave 2 with 19
passing tests; this PR did not modify it.

A thin wrapper `_run_manifest_validator(parsed, manifest_dir, *, source)` was
added at the top of `core/core.py`. It:
- catches any unexpected validator exception and prints it as a single ERROR
  line (validator promises not to raise; this is a defensive belt),
- prints each warning as `[manifest_validator] WARNING: <msg> (source=<site>)`,
- prints each error as `[manifest_validator] ERROR: <msg> (source=<site>)`,
- always emits a summary `[manifest_validator] X warnings, Y errors
  (warnings-mode: not blocking) (source=<site>)`.

Output goes to stdout via `print(..., flush=True)`, matching the existing
`[core] ...` log style. Per the protocol-constraint rule in §10 of the morning
review, validator output is NOT plumbed into `/v0/admin/state` and there is NO
new endpoint.

`load_manifest` now takes two kwargs: `source: str = "startup"` and
`validate: bool = True`. Both have safe defaults, so all existing callers
(experiments/, peer_link, etc.) keep working unchanged.

## Sample output (broken manifest POST)

Captured running Core against a duplicate-node-id, undeclared-from, missing-
surface, unset-env-secret manifest (4 errors + 1 warning):

```
[manifest_validator] 0 warnings, 0 errors (warnings-mode: not blocking) (source=startup)
[core] listening on http://127.0.0.1:8765  manifest=/tmp/raven_validator_test_dir/demo.yaml
[manifest_validator] WARNING: node 'alpha'.identity_secret: env var 'UNSET_BROKEN_SECRET_VAR_XYZ' is unset; Core will autogenerate a secret and set it (source=/v0/admin/manifest)
[manifest_validator] ERROR: node[1].id: duplicate node id 'alpha' (source=/v0/admin/manifest)
[manifest_validator] ERROR: relationships[0]: 'from' references undeclared node 'ghost' (source=/v0/admin/manifest)
[manifest_validator] ERROR: relationships[0]: surface 'ping' does not exist on node 'alpha' (in 'alpha.ping') (source=/v0/admin/manifest)
[manifest_validator] ERROR: relationships[1]: surface 'nonexistent_surface' does not exist on node 'alpha' (in 'alpha.nonexistent_surface') (source=/v0/admin/manifest)
[manifest_validator] 1 warnings, 4 errors (warnings-mode: not blocking) (source=/v0/admin/manifest)
[manifest_validator] ERROR: node[1].id: duplicate node id 'alpha' (source=/v0/admin/reload)
[manifest_validator] ERROR: relationships[0]: 'from' references undeclared node 'ghost' (source=/v0/admin/reload)
[manifest_validator] ERROR: relationships[0]: surface 'ping' does not exist on node 'alpha' (in 'alpha.ping') (source=/v0/admin/reload)
[manifest_validator] ERROR: relationships[1]: surface 'nonexistent_surface' does not exist on node 'alpha' (in 'alpha.nonexistent_surface') (source=/v0/admin/reload)
[manifest_validator] 0 warnings, 4 errors (warnings-mode: not blocking) (source=/v0/admin/reload)
```

The POST returned `200 {"ok": true, "nodes_declared": 1, "edges": 2}` — i.e.
warnings + errors printed but the request was NOT blocked, and the manifest
was accepted. A subsequent `POST /v0/admin/reload` re-validated the on-disk
copy with `source=/v0/admin/reload` (no warning the second time because the
auto-generated secret had been set into `os.environ` by the first load).

## Test status

`python3 -m pytest -q` → **144 passed, 142 warnings** (the warnings are the
pre-existing `NotAppKeyWarning` from aiohttp, unrelated). No test assertions
broke from the new stdout lines — tests do not assert on Core's stdout
content.

## Out of scope (intentionally deferred)

- Strict-mode (errors block startup or 4xx the POST)
- Plumbing validator output into `/v0/admin/state`
- A dedicated `/v0/admin/validate` endpoint
- Touching the validator's internals or its schema

These are the next-flip items called out in §10 of the morning review.
