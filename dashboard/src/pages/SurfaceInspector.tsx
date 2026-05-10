import { useMemo, useState } from "react";
import { Wrench, Inbox, Send } from "lucide-react";
import { adminInvoke } from "../lib/api";
import type { AdminState, Node, Surface } from "../lib/types";

type Props = { state: AdminState };

export default function SurfaceInspector({ state }: Props) {
  const [selectedNodeId, setSelectedNodeId] = useState(state.nodes[0]?.id ?? "");
  const node = state.nodes.find((n) => n.id === selectedNodeId);

  return (
    <div className="h-full flex">
      <div className="w-[200px] border-r border-[var(--color-border)] overflow-y-auto">
        <div className="px-4 py-3 border-b border-[var(--color-border)]">
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
            nodes
          </div>
        </div>
        {state.nodes.map((n) => (
          <button
            key={n.id}
            onClick={() => setSelectedNodeId(n.id)}
            className={`w-full text-left px-4 py-2 mono text-[12px] border-l-2 transition-colors ${
              selectedNodeId === n.id
                ? "bg-[var(--color-surface-2)] text-white border-[var(--color-accent)]"
                : "border-transparent text-[var(--color-text-muted)] hover:text-white hover:bg-[var(--color-surface)]"
            }`}
          >
            <div className="flex items-center gap-2">
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  n.connected ? "bg-[var(--color-ok)]" : "bg-[var(--color-bad)]"
                }`}
              />
              <span className="flex-1 truncate">{n.id}</span>
              <span className="text-[10px] text-[var(--color-text-faint)]">
                {n.surfaces.length}
              </span>
            </div>
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto p-6">
        {node ? <NodePanel node={node} state={state} /> : null}
      </div>
    </div>
  );
}

function NodePanel({ node, state }: { node: Node; state: AdminState }) {
  return (
    <div>
      <div className="mb-6">
        <div className="text-[18px] font-medium tracking-tight">{node.id}</div>
        <div className="mono text-[11px] text-[var(--color-text-faint)] mt-1">
          {node.kind} · {node.runtime} · {node.connected ? "connected" : "offline"}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4">
        {node.surfaces.map((s) => (
          <SurfaceCard key={s.name} node={node} surface={s} state={state} />
        ))}
      </div>
    </div>
  );
}

function SurfaceCard({
  node,
  surface,
  state,
}: {
  node: Node;
  surface: Surface;
  state: AdminState;
}) {
  const [showTry, setShowTry] = useState(false);
  const Icon = surface.type === "tool" ? Wrench : Inbox;
  const target = `${node.id}.${surface.name}`;
  const recent = useMemo(
    () => state.envelope_tail.filter((e) => e.to_surface === target).slice(0, 5),
    [state.envelope_tail, target],
  );

  return (
    <div className="border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)]">
      <div className="px-4 py-3 border-b border-[var(--color-border)] flex items-center gap-3">
        <Icon size={14} className="text-[var(--color-accent)]" strokeWidth={1.5} />
        <div>
          <div className="mono text-[13px]">
            {node.id}.<span className="text-[var(--color-accent)]">{surface.name}</span>
          </div>
          <div className="mono text-[10px] text-[var(--color-text-faint)] mt-0.5">
            {surface.type} · {surface.invocation_mode}
          </div>
        </div>
        <button
          onClick={() => setShowTry((s) => !s)}
          className="ml-auto text-[11px] text-[var(--color-text-muted)] hover:text-white border border-[var(--color-border)] hover:border-[var(--color-border-strong)] px-2.5 py-1 rounded transition-colors"
        >
          {showTry ? "hide try-it" : "try-it"}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-0">
        <div className="border-r border-[var(--color-border)]">
          <div className="px-4 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
            schema
          </div>
          <pre className="mono text-[11px] px-4 pb-4 text-[var(--color-text-muted)] overflow-x-auto">
            {JSON.stringify(surface.schema, null, 2)}
          </pre>
        </div>

        <div>
          <div className="px-4 py-2 text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
            recent invocations
          </div>
          <div className="px-4 pb-4 space-y-2">
            {recent.length === 0 && (
              <div className="text-[11px] mono text-[var(--color-text-faint)] py-3">
                no recent traffic
              </div>
            )}
            {recent.map((e) => (
              <div
                key={e.msg_id ?? e.ts}
                className="border border-[var(--color-border)] rounded p-2 mono text-[11px]"
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`w-1 h-1 rounded-full ${
                      e.route_status === "routed"
                        ? "bg-[var(--color-ok)]"
                        : "bg-[var(--color-bad)]"
                    }`}
                  />
                  <span className="text-[var(--color-text-faint)]">
                    {new Date(e.ts).toLocaleTimeString("en-US", { hour12: false })}
                  </span>
                  <span className="text-[var(--color-text-muted)]">{e.from_node}</span>
                </div>
                <pre className="mt-1 text-[10px] text-[var(--color-text)] truncate">
                  {JSON.stringify(e.payload).slice(0, 120)}
                </pre>
              </div>
            ))}
          </div>
        </div>
      </div>

      {showTry && (
        <TryItPanel node={node} surface={surface} state={state} />
      )}
    </div>
  );
}

function TryItPanel({
  node,
  surface,
  state,
}: {
  node: Node;
  surface: Surface;
  state: AdminState;
}) {
  const [payloadText, setPayloadText] = useState(
    JSON.stringify(scaffoldPayload(surface.schema), null, 2),
  );
  // Pick a from_node that has an edge to this surface, default to human_node.
  const target = `${node.id}.${surface.name}`;
  const candidateFroms = state.relationships
    .filter((r) => r.to === target)
    .map((r) => r.from);
  const defaultFrom = candidateFroms[0] ?? state.nodes[0]?.id ?? "";
  const [fromNode, setFromNode] = useState(defaultFrom);
  const [result, setResult] = useState<any>(null);
  const [running, setRunning] = useState(false);

  async function fire() {
    let payload: any = {};
    try {
      payload = JSON.parse(payloadText);
    } catch (e) {
      setResult({ error: `bad JSON: ${e}` });
      return;
    }
    setRunning(true);
    try {
      const r = await adminInvoke({
        from_node: fromNode,
        target,
        payload,
      });
      setResult(r);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="border-t border-[var(--color-border)] p-4 space-y-3 bg-[var(--color-surface-2)]">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">
          from
        </span>
        <select
          value={fromNode}
          onChange={(e) => setFromNode(e.target.value)}
          className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded px-2 py-1 text-[11px] mono text-[var(--color-text)]"
        >
          {(candidateFroms.length ? candidateFroms : state.nodes.map((n) => n.id)).map(
            (id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ),
          )}
        </select>
      </div>
      <textarea
        value={payloadText}
        onChange={(e) => setPayloadText(e.target.value)}
        rows={6}
        className="w-full mono text-[11px] bg-[var(--color-bg)] border border-[var(--color-border)] rounded p-2.5 text-[var(--color-text)] focus:outline-none focus:border-[var(--color-accent)]"
      />
      <button
        onClick={fire}
        disabled={running}
        className="flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--color-accent)] text-black text-[12px] font-medium hover:bg-[var(--color-accent)]/90 disabled:opacity-50"
      >
        <Send size={12} />
        {running ? "firing..." : `invoke ${target}`}
      </button>
      {result && (
        <pre className="mono text-[11px] bg-[var(--color-bg)] border border-[var(--color-border)] rounded p-2.5 overflow-x-auto text-[var(--color-text-muted)] whitespace-pre-wrap">
          {JSON.stringify(result, null, 2)}
        </pre>
      )}
    </div>
  );
}

function scaffoldPayload(schema: any): any {
  if (!schema || typeof schema !== "object") return {};
  if (schema.type === "object" && schema.properties) {
    const out: Record<string, any> = {};
    Object.entries<any>(schema.properties).forEach(([k, v]) => {
      out[k] = scaffoldPayload(v);
    });
    return out;
  }
  if (schema.enum && schema.enum.length) return schema.enum[0];
  if (schema.type === "string") return "";
  if (schema.type === "number" || schema.type === "integer") return 0;
  if (schema.type === "boolean") return false;
  if (schema.type === "array") return [];
  return null;
}
