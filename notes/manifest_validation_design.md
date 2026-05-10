# Manifest validation — design + integration plan

Status: validator + schema + tests are landed. **Not yet wired** into
`core.core.CoreState.load_manifest`. This note proposes how to wire it.

## Why

`core/core.py:128-129` adds every relationship to `state.edges` without checking
whether the named nodes exist. `manifests/full_demo.yaml` ships with 10 edges
from `nexus_agent` even though that node is never declared (synthesis worker
`notes/synthesis_20260510.md` §3, point 2). Today those edges are dead but
silently accepted. The next time someone runs strict edge validation, the
lenient semantics will be load-bearing somewhere.

The synthesis worker also flagged that there is no schema validation of the
manifest itself — a misspelled `kind` or `invocation_mode` is accepted, the
node just behaves weirdly later.

## What the validator does

`core/manifest_validator.validate_manifest(manifest, manifest_dir, *, env=None)`
returns `(errors, warnings)` — both `list[str]`. Pure function, never raises.

| Class | Surface |
| --- | --- |
| **error** | manifest fails JSON Schema (`schemas/manifest.json`) — missing `nodes`, bad `kind`, bad `invocation_mode`, `to` without exactly one dot, etc. |
| **error** | duplicate node id |
| **error** | reserved node id (`core`) |
| **error** | node id contains `.` or `/` |
| **error** | duplicate surface name within a node |
| **error** | surface schema file does not exist |
| **error** | surface schema file does not parse as JSON |
| **error** | relationship `from` references undeclared node |
| **error** | relationship `to` references undeclared target node |
| **error** | relationship `to` references unknown surface on a known target node |
| **warning** | `identity_secret: env:VAR` where `VAR` is unset (Core will autogenerate) |

Tests live at `tests/test_manifest_validator.py` (19 tests). One test asserts
that `manifests/full_demo.yaml` produces ≥10 "undeclared node 'nexus_agent'"
errors — this is the regression the synthesis worker called out.

## Proposed integration into core.core

Two-stage rollout. Default is non-breaking; strict mode is opt-in.

### Stage 1 — warnings-only (default), strict opt-in

Modify `CoreState.load_manifest` (`core/core.py:103`) to:

1. Parse the YAML as it does today.
2. Call `validate_manifest(m, self.manifest_path.parent)`.
3. Print every error and warning to stderr in a stable, greppable format
   (e.g. `"[manifest:error] ..."`, `"[manifest:warn] ..."`).
4. If `MESH_STRICT_MANIFEST=1` (env) **or** Core was started with
   `--strict-manifest` (CLI flag): raise on the first error so existing
   `try/except` in `handle_admin_manifest` (`core/core.py:528`) and
   `handle_admin_reload` rolls back the manifest as it does today.
5. Otherwise: continue loading (current lenient behavior). This means today's
   `full_demo.yaml` keeps booting, just with a loud log line per dead edge.

Pseudocode:

```python
def load_manifest(self) -> None:
    self._reset_manifest_state()
    text = self.manifest_path.read_text()
    m = yaml.safe_load(text)
    manifest_dir = self.manifest_path.parent

    errors, warnings = validate_manifest(m, manifest_dir)
    for w in warnings:
        print(f"[manifest:warn] {w}", file=sys.stderr, flush=True)
    for e in errors:
        print(f"[manifest:error] {e}", file=sys.stderr, flush=True)
    strict = os.environ.get("MESH_STRICT_MANIFEST") == "1"
    if errors and strict:
        raise ValueError(f"manifest validation failed ({len(errors)} errors)")

    # ... existing node + edge loading unchanged ...
```

The existing `surfaces[s["name"]] = ...` loop (`core/core.py:111-118`) keeps
its own `schema_path.read_text()` because the validator only validates — it
does not return parsed schemas. Keeping the parse in `load_manifest` avoids
threading state through the validator.

### Stage 2 — flip the default

After at least one cycle of strict mode being optional, fix `full_demo.yaml`
(declare `nexus_agent` as a node, or remove the orphan edges), then default
`MESH_STRICT_MANIFEST=1` and add `--no-strict-manifest` as the escape hatch.

## Admin-endpoint surface

`POST /v0/admin/manifest` (`core/core.py:511`) and `POST /v0/admin/reload`
(`core/core.py:541`) should both:

* Always run validation.
* Return `errors` and `warnings` in the JSON response, even on the success
  path. The dashboard already has an obvious place to render this — the
  Mesh Builder's "Save" button. Surfacing warnings is cheap, costs no UX.
* On error in strict mode: behave exactly as today (rollback to the `.bak`).
  The validator's error list is more useful than the existing
  `{"error": "load_failed", "details": str(e)}` blob, so include the list in
  the response.

Suggested response shape:

```json
{
  "ok": true,
  "manifest_path": "...",
  "nodes_declared": 6,
  "edges": 27,
  "validation": {
    "errors": [],
    "warnings": ["node 'kanban_node'.identity_secret: env var 'KANBAN_NODE_SECRET' is unset; Core will autogenerate a secret and set it"]
  }
}
```

## What I deliberately did not do

* **Did not add the validator call to `core.py`.** The brief says explicitly
  to leave that for review.
* **Did not touch `full_demo.yaml`.** Two reasonable fixes — declare
  `nexus_agent` as a real node, or delete the orphan edges — and the synthesis
  worker's open question (§3, point 2) is whether to fix the manifest first or
  the loader first. Either way, the current behavior is now covered by a
  failing-on-purpose-once-strict test
  (`test_real_full_demo_flags_undeclared_nexus_agent`). When the manifest is
  fixed, that test will need to flip to "passes cleanly."
* **Did not validate `runtime` values.** `runtime: local-process` is currently
  an opaque label (synthesis §4 architecture-gaps). Once the supervisor work
  defines a runtime block schema, that goes here.
* **Did not warn on disconnected nodes** (declared but no edges in or out).
  Likely useful but easy to add later as another warning.

## Open questions for review

1. **Strict-on-by-default or off-by-default at v1?** I picked off-by-default
   for backward compat. If you'd rather flip immediately, fix
   `full_demo.yaml` first and skip Stage 1.
2. **Should the validator parse + cache schemas instead of just verifying they
   parse?** Today the validator reads each surface schema once for parse-check,
   then `load_manifest` reads it again for real use. Trivial duplication. Not
   worth changing unless we hit big-manifest performance issues.
3. **Should `core` reservation extend to other names?** I included only
   `core` (your future self-surface namespace, per synthesis §6 proposal).
   `dashboard_node` and `human_node` are user-defined, not reserved.
4. **Do we want a CLI tool** (`python -m core.manifest_validator <path>`) for
   pre-commit checks? Trivial to add — wraps `validate_manifest` and prints —
   but out of scope for this task.
