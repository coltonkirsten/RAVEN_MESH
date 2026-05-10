import { useEffect, useMemo, useRef, useState } from "react";
import { Save, RotateCcw, X, User, Wrench, ShieldCheck, Boxes } from "lucide-react";
import { reload, writeManifest } from "../lib/api";
import type { AdminState, Node, Relationship, Surface } from "../lib/types";

type Props = { state: AdminState; onReload: () => void };

const NODE_W = 200;
const NODE_H = 92;

type LaidOutNode = Node & { x: number; y: number };

function kindIcon(kind: string) {
  if (kind === "actor") return User;
  if (kind === "approval") return ShieldCheck;
  if (kind === "hybrid") return Boxes;
  return Wrench;
}

function kindColor(kind: string) {
  switch (kind) {
    case "actor":
      return "var(--color-accent)";
    case "approval":
      return "#facc15";
    case "hybrid":
      return "#a78bfa";
    default:
      return "#4ade80";
  }
}

export default function MeshBuilder({ state, onReload }: Props) {
  const [layout, setLayout] = useState<Record<string, { x: number; y: number }>>({});
  const [draftEdges, setDraftEdges] = useState<Relationship[]>([]);
  const [removedEdges, setRemovedEdges] = useState<Set<string>>(new Set());
  const [selectedEdge, setSelectedEdge] = useState<Relationship | null>(null);
  const [pendingFrom, setPendingFrom] = useState<{ nodeId: string; surface: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [yamlPreview, setYamlPreview] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);

  // Initialize layout: simple radial / grid layout if not yet placed.
  useEffect(() => {
    setLayout((current) => {
      const next = { ...current };
      const unplaced = state.nodes.filter((n) => !next[n.id]);
      if (unplaced.length === 0) return current;
      const centerX = 480;
      const centerY = 320;
      const radius = 240;
      state.nodes.forEach((n, i) => {
        if (next[n.id]) return;
        const angle = (i / state.nodes.length) * Math.PI * 2;
        next[n.id] = {
          x: centerX + Math.cos(angle) * radius,
          y: centerY + Math.sin(angle) * radius,
        };
      });
      return next;
    });
  }, [state.nodes]);

  const laidOut: LaidOutNode[] = useMemo(
    () =>
      state.nodes.map((n) => ({
        ...n,
        x: layout[n.id]?.x ?? 0,
        y: layout[n.id]?.y ?? 0,
      })),
    [state.nodes, layout],
  );

  const allEdges: Relationship[] = useMemo(() => {
    const seen = new Set<string>();
    const out: Relationship[] = [];
    [...state.relationships, ...draftEdges].forEach((e) => {
      const key = `${e.from}::${e.to}`;
      if (removedEdges.has(key)) return;
      if (seen.has(key)) return;
      seen.add(key);
      out.push(e);
    });
    return out;
  }, [state.relationships, draftEdges, removedEdges]);

  const dirty =
    draftEdges.length > 0 || removedEdges.size > 0;

  function startDrag(nodeId: string, e: React.PointerEvent) {
    e.preventDefault();
    const svg = svgRef.current!;
    const start = svg.getBoundingClientRect();
    function onMove(ev: PointerEvent) {
      setLayout((cur) => ({
        ...cur,
        [nodeId]: {
          x: ev.clientX - start.left,
          y: ev.clientY - start.top,
        },
      }));
    }
    function onUp() {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }

  function onSurfaceClick(nodeId: string, surfaceName: string) {
    if (!pendingFrom) {
      setPendingFrom({ nodeId, surface: surfaceName });
      return;
    }
    if (pendingFrom.nodeId === nodeId) {
      setPendingFrom(null);
      return;
    }
    const edge: Relationship = {
      from: pendingFrom.nodeId,
      to: `${nodeId}.${surfaceName}`,
    };
    setDraftEdges((prev) => [...prev, edge]);
    setPendingFrom(null);
  }

  function removeEdge(edge: Relationship) {
    const key = `${edge.from}::${edge.to}`;
    setRemovedEdges((prev) => new Set(prev).add(key));
    setDraftEdges((prev) => prev.filter((e) => `${e.from}::${e.to}` !== key));
    setSelectedEdge(null);
  }

  function buildManifestYaml(): string {
    // Reconstruct YAML preserving existing schema paths.
    const lines: string[] = [];
    lines.push("nodes:");
    state.nodes.forEach((n) => {
      lines.push(`  - id: ${n.id}`);
      lines.push(`    kind: ${n.kind}`);
      lines.push(`    runtime: ${n.runtime}`);
      const secret = secretEnvName(n.id);
      lines.push(`    identity_secret: env:${secret}`);
      if (n.metadata && Object.keys(n.metadata).length) {
        lines.push("    metadata:");
        Object.entries(n.metadata).forEach(([k, v]) => {
          lines.push(`      ${k}: ${JSON.stringify(v)}`);
        });
      }
      lines.push("    surfaces:");
      n.surfaces.forEach((s) => {
        lines.push(`      - name: ${s.name}`);
        lines.push(`        type: ${s.type}`);
        lines.push(`        invocation_mode: ${s.invocation_mode}`);
        lines.push(`        schema: ../schemas/${guessSchemaPath(s)}`);
      });
    });
    lines.push("");
    lines.push("relationships:");
    allEdges.forEach((e) => {
      lines.push(`  - { from: ${e.from}, to: ${e.to} }`);
    });
    return lines.join("\n") + "\n";
  }

  async function onSave() {
    const yaml = buildManifestYaml();
    setYamlPreview(yaml);
  }

  async function confirmSave() {
    if (!yamlPreview) return;
    setSaving(true);
    try {
      const r = await writeManifest(yamlPreview);
      if (r.status !== 200) {
        alert(`save failed: ${JSON.stringify(r.data)}`);
        return;
      }
      setDraftEdges([]);
      setRemovedEdges(new Set());
      setYamlPreview(null);
      onReload();
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center">
        <div>
          <div className="text-[15px] font-medium tracking-tight">Mesh Builder</div>
          <div className="text-[11px] text-[var(--color-text-faint)] mono mt-0.5">
            click a surface, then click another to draw an edge · drag nodes to reposition
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={async () => {
              await reload();
              onReload();
              setDraftEdges([]);
              setRemovedEdges(new Set());
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--color-border)] text-[12px] text-[var(--color-text-muted)] hover:text-white hover:border-[var(--color-border-strong)] transition-colors"
          >
            <RotateCcw size={12} /> reload from disk
          </button>
          <button
            onClick={onSave}
            disabled={!dirty || saving}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] transition-colors ${
              dirty
                ? "border border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-black"
                : "border border-[var(--color-border)] text-[var(--color-text-faint)]"
            }`}
          >
            <Save size={12} /> save to manifest
            {dirty && (
              <span className="text-[10px] opacity-70 ml-1">
                +{draftEdges.length} −{removedEdges.size}
              </span>
            )}
          </button>
        </div>
      </div>

      {pendingFrom && (
        <div className="px-6 py-2 border-b border-[var(--color-border)] mono text-[11px] bg-[var(--color-accent-soft)] text-[var(--color-accent)]">
          edge from <strong>{pendingFrom.nodeId}.{pendingFrom.surface}</strong> →
          click another node's surface to complete (or click same node to cancel)
        </div>
      )}

      <div className="flex-1 overflow-auto relative">
        <svg
          ref={svgRef}
          className="block min-w-full min-h-full"
          width="1200"
          height="800"
        >
          <defs>
            <marker
              id="arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#3a3a3a" />
            </marker>
            <marker
              id="arrow-draft"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-accent)" />
            </marker>
          </defs>

          {allEdges.map((edge) => {
            const fromNode = laidOut.find((n) => n.id === edge.from);
            const toNodeId = edge.to.split(".")[0];
            const toNode = laidOut.find((n) => n.id === toNodeId);
            if (!fromNode || !toNode) return null;
            const isDraft = draftEdges.some(
              (d) => d.from === edge.from && d.to === edge.to,
            );
            const isSelected =
              selectedEdge?.from === edge.from && selectedEdge?.to === edge.to;
            return (
              <line
                key={`${edge.from}::${edge.to}`}
                x1={fromNode.x + NODE_W / 2}
                y1={fromNode.y + NODE_H / 2}
                x2={toNode.x + NODE_W / 2}
                y2={toNode.y + NODE_H / 2}
                stroke={
                  isSelected
                    ? "var(--color-accent)"
                    : isDraft
                      ? "var(--color-accent)"
                      : "#2a2a2a"
                }
                strokeWidth={isSelected || isDraft ? 1.5 : 1}
                strokeDasharray={isDraft ? "4 3" : undefined}
                markerEnd={isDraft ? "url(#arrow-draft)" : "url(#arrow)"}
                className="cursor-pointer"
                onClick={() => setSelectedEdge(edge)}
              />
            );
          })}

          {laidOut.map((node) => (
            <NodeCard
              key={node.id}
              node={node}
              onPointerDown={(e) => startDrag(node.id, e)}
              onSurfaceClick={(name) => onSurfaceClick(node.id, name)}
              pendingFrom={pendingFrom}
            />
          ))}
        </svg>

        {selectedEdge && (
          <div className="absolute right-4 top-4 w-[320px] bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg p-4 shadow-xl">
            <div className="flex items-center mb-3">
              <div className="text-[12px] font-medium">Edge</div>
              <button
                onClick={() => setSelectedEdge(null)}
                className="ml-auto text-[var(--color-text-muted)] hover:text-white"
              >
                <X size={14} />
              </button>
            </div>
            <div className="mono text-[12px] space-y-1.5">
              <div className="text-[var(--color-text-muted)]">from</div>
              <div>{selectedEdge.from}</div>
              <div className="text-[var(--color-text-muted)] mt-2">to</div>
              <div>{selectedEdge.to}</div>
            </div>
            <button
              onClick={() => removeEdge(selectedEdge)}
              className="mt-4 w-full px-3 py-1.5 rounded border border-[var(--color-bad)]/40 text-[12px] text-[var(--color-bad)] hover:bg-[var(--color-bad)]/10"
            >
              remove edge
            </button>
          </div>
        )}
      </div>

      {yamlPreview && (
        <YamlPreview
          yaml={yamlPreview}
          onCancel={() => setYamlPreview(null)}
          onConfirm={confirmSave}
          saving={saving}
        />
      )}
    </div>
  );
}

function NodeCard({
  node,
  onPointerDown,
  onSurfaceClick,
  pendingFrom,
}: {
  node: LaidOutNode;
  onPointerDown: (e: React.PointerEvent) => void;
  onSurfaceClick: (surface: string) => void;
  pendingFrom: { nodeId: string; surface: string } | null;
}) {
  const Icon = kindIcon(node.kind);
  const accent = kindColor(node.kind);

  return (
    <foreignObject x={node.x} y={node.y} width={NODE_W} height={NODE_H + node.surfaces.length * 22}>
      <div
        onPointerDown={onPointerDown}
        className={`bg-[var(--color-surface)] border rounded-md select-none ${
          node.connected
            ? "border-[var(--color-border-strong)]"
            : "border-[var(--color-border)] opacity-70"
        }`}
        style={{ borderTop: `2px solid ${accent}` }}
      >
        <div className="px-3 py-2.5 cursor-grab active:cursor-grabbing">
          <div className="flex items-center gap-2">
            <Icon size={13} style={{ color: accent }} strokeWidth={1.5} />
            <div className="flex-1 min-w-0">
              <div className="mono text-[12px] truncate">{node.id}</div>
              <div className="mono text-[10px] text-[var(--color-text-faint)]">
                {node.kind}
              </div>
            </div>
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                node.connected ? "bg-[var(--color-ok)]" : "bg-[var(--color-bad)]"
              }`}
            />
          </div>
        </div>
        <div className="px-2 pb-2 space-y-0.5">
          {node.surfaces.map((s) => (
            <button
              key={s.name}
              onPointerDown={(e) => e.stopPropagation()}
              onClick={(e) => {
                e.stopPropagation();
                onSurfaceClick(s.name);
              }}
              className={`w-full text-left mono text-[10px] px-2 py-1 rounded transition-colors ${
                pendingFrom?.nodeId === node.id && pendingFrom?.surface === s.name
                  ? "bg-[var(--color-accent)] text-black"
                  : "text-[var(--color-text-muted)] hover:bg-[var(--color-surface-2)] hover:text-white"
              }`}
            >
              <span className="text-[var(--color-text-faint)]">·</span> {s.name}
            </button>
          ))}
        </div>
      </div>
    </foreignObject>
  );
}

function YamlPreview({
  yaml,
  onCancel,
  onConfirm,
  saving,
}: {
  yaml: string;
  onCancel: () => void;
  onConfirm: () => void;
  saving: boolean;
}) {
  return (
    <div className="absolute inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-20">
      <div className="bg-[var(--color-surface)] border border-[var(--color-border-strong)] rounded-lg w-[680px] max-h-[80%] flex flex-col shadow-2xl">
        <div className="px-5 py-4 border-b border-[var(--color-border)]">
          <div className="text-[13px] font-medium">Preview manifest</div>
          <div className="text-[11px] text-[var(--color-text-faint)] mono mt-0.5">
            this YAML will replace the current manifest on disk + reload core
          </div>
        </div>
        <pre className="flex-1 overflow-auto mono text-[11px] p-5 text-[var(--color-text-muted)]">
          {yaml}
        </pre>
        <div className="px-5 py-3 border-t border-[var(--color-border)] flex items-center justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded border border-[var(--color-border)] text-[12px] text-[var(--color-text-muted)] hover:text-white"
          >
            cancel
          </button>
          <button
            disabled={saving}
            onClick={onConfirm}
            className="px-3 py-1.5 rounded bg-[var(--color-accent)] text-black text-[12px] font-medium hover:bg-[var(--color-accent)]/90"
          >
            {saving ? "saving..." : "confirm + save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function secretEnvName(nodeId: string): string {
  return `${nodeId.toUpperCase()}_SECRET`;
}

function guessSchemaPath(s: Surface): string {
  // Best-effort filename inference. For perfect fidelity the manifest writer
  // would carry through original schema_path, but for v0 this is good enough.
  if (s.schema?.title) return `${s.schema.title}.json`;
  return `${s.name}.json`;
}
