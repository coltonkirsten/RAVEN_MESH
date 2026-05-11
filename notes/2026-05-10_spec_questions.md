# SPEC.md alignment work — open questions and resolutions

Notes captured while bringing code into line with `docs/SPEC.md` on
`simplify-raven`.

## 1. PRD.md does not exist

Task brief asked to "Update docs/PROTOCOL.md and docs/PRD.md to point at
docs/SPEC.md". Only `docs/PROTOCOL.md` and `docs/PROTOTYPE.md` exist on
this branch — there is no `docs/PRD.md`. **Resolution:** added the
historical-reference banner to `docs/PROTOCOL.md` and `docs/PROTOTYPE.md`
(the two pre-existing `.md` docs in `docs/`); did not invent a PRD.md.

## 2. `core.metrics` payload shape

SPEC §5.2 says `core.metrics` "Return counters and gauges as JSON" but
does not pin the field set. The dropped `/v0/admin/metrics` endpoint used
to return supervisor metrics. **Resolution:** `core.metrics` returns the
same shape Core can build today: counters (nodes_declared / connected,
edges, pending invocations, replay-LRU size, envelope-tail size) plus the
supervisor metrics block when the supervisor is enabled. This stays
internally consistent and is the only data Core has handy without inventing
new instrumentation.

## 3. `/v0/admin/metrics` (out-of-band) format

SPEC §4.5 calls `/v0/admin/metrics` a "Prometheus-format metrics scrape"
but the existing handler returned JSON (supervisor metrics). The spec is
authoritative; the previous JSON behaviour is preserved as
`core.metrics` (a mesh surface, see #2). The out-of-band endpoint now
emits Prometheus exposition text built from the same counters, gated by
`ADMIN_TOKEN`. Minimal counter set chosen to match what Core already
tracks; this can be expanded without a wire-prefix bump.

## 4. `core.audit_query` reading from `audit.log`

SPEC §5.3 says "audit entries matching a filter" — implementation reads
the JSON-per-line `audit.log` (the file Core already writes per §7).
Filtering is a tail-and-filter pass: load up to a generous tail window
(defensive cap), reverse-iterate, apply the conjunctive filter, stop at
`last_n`. This is O(file_size) in the worst case; acceptable for the
current single-process Core. A future Core may want an in-memory ring or
SQLite index. Documented as a known limitation here so the spec stays
authoritative on shape, not implementation.

## 5. `core` listed in `/v0/register` snapshots

SPEC §5.1: "Always present in every running mesh whether or not the
manifest names it. Listed in `/v0/register` snapshots and `/v0/introspect`
output." `/v0/register` returns a per-node snapshot keyed off the
caller's identity. The spec wording could mean either "the `core` node
appears in introspect snapshots" or "every register response includes a
listing of `core` and its surfaces". **Resolution:** `core` is added to
`nodes_decl` at startup (before manifest load), so it appears in
`/v0/introspect` automatically and its surfaces participate in normal
edge resolution. Per-node `/v0/register` responses still return only the
caller's surfaces and the relationships touching them — the natural
behaviour of the existing endpoint — but those relationships will now
include any `(caller, core.*)` edges from the manifest.

## 6. Manifest reload SSE event payload

SPEC §5.4: "Core emits a `manifest_reloaded` SSE event to every still-
connected node's `/v0/stream`." Payload shape unspecified. Resolution:
emit `{"timestamp": <iso>, "edges_changed": bool}`. Minimal and lets the
consumer decide whether to re-introspect.
