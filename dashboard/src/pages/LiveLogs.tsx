import { useEffect, useMemo, useRef, useState } from "react";
import {
  Pause,
  Play,
  Trash2,
  X,
  Search,
  CheckCircle2,
  AlertTriangle,
  XCircle,
} from "lucide-react";
import { subscribeStream } from "../lib/api";
import type { AdminState, EnvelopeEvent, Relationship } from "../lib/types";

type Props = { state: AdminState };

const MAX_KEEP = 800;

export default function LiveLogs({ state }: Props) {
  const [events, setEvents] = useState<EnvelopeEvent[]>(() => state.envelope_tail);
  const [paused, setPaused] = useState(false);
  const [streamStatus, setStreamStatus] = useState<string>("connecting");
  const [filterNode, setFilterNode] = useState<string>("");
  const [filterSurface, setFilterSurface] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [selected, setSelected] = useState<EnvelopeEvent | null>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  useEffect(() => {
    const unsub = subscribeStream(
      (evt) => {
        if (pausedRef.current) return;
        setEvents((prev) => {
          const next = [evt, ...prev];
          if (next.length > MAX_KEEP) next.length = MAX_KEEP;
          return next;
        });
      },
      (status) => setStreamStatus(status),
    );
    return unsub;
  }, []);

  const allNodes = useMemo(
    () => Array.from(new Set(state.nodes.map((n) => n.id))).sort(),
    [state.nodes],
  );
  const allSurfaces = useMemo(() => {
    const out: string[] = [];
    state.nodes.forEach((n) =>
      n.surfaces.forEach((s) => out.push(`${n.id}.${s.name}`)),
    );
    return out.sort();
  }, [state.nodes]);

  const filtered = useMemo(() => {
    return events.filter((e) => {
      if (filterNode && e.from_node !== filterNode) return false;
      if (filterSurface && e.to_surface !== filterSurface) return false;
      if (filterStatus && e.route_status !== filterStatus) return false;
      if (search) {
        const s = search.toLowerCase();
        const hay = `${e.msg_id ?? ""} ${e.correlation_id ?? ""} ${JSON.stringify(e.payload ?? {})}`.toLowerCase();
        if (!hay.includes(s)) return false;
      }
      return true;
    });
  }, [events, filterNode, filterSurface, filterStatus, search]);

  return (
    <div className="h-full flex flex-col">
      <Header
        streamStatus={streamStatus}
        paused={paused}
        onPause={() => setPaused((p) => !p)}
        onClear={() => setEvents([])}
        count={filtered.length}
        total={events.length}
      />

      <div className="px-6 py-3 border-b border-[var(--color-border)] flex items-center gap-2 flex-wrap">
        <FilterSelect
          value={filterNode}
          onChange={setFilterNode}
          options={allNodes}
          label="node"
        />
        <FilterSelect
          value={filterSurface}
          onChange={setFilterSurface}
          options={allSurfaces}
          label="surface"
        />
        <FilterSelect
          value={filterStatus}
          onChange={setFilterStatus}
          options={["routed", "denied_no_relationship", "denied_signature_invalid", "denied_schema_invalid", "denied_node_unreachable", "denied_unknown_surface", "timeout"]}
          label="status"
        />
        <div className="flex items-center gap-2 ml-auto bg-[var(--color-surface)] border border-[var(--color-border)] px-2.5 py-1.5 rounded text-[12px]">
          <Search size={13} className="text-[var(--color-text-faint)]" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search msg_id / payload"
            className="bg-transparent outline-none w-[220px] mono text-[12px] placeholder:text-[var(--color-text-faint)]"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-[12px] mono">
          <thead className="sticky top-0 bg-[var(--color-bg)]">
            <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
              <th className="text-left font-normal py-2 px-3 w-[160px]">timestamp</th>
              <th className="text-left font-normal w-[40px]">·</th>
              <th className="text-left font-normal">flow</th>
              <th className="text-left font-normal w-[180px]">msg_id</th>
              <th className="text-right font-normal py-2 px-3 w-[120px]">status</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((evt, i) => (
              <Row
                key={`${evt.msg_id ?? i}-${evt.ts}-${i}`}
                evt={evt}
                onClick={() => setSelected(evt)}
                isSelected={selected === evt}
              />
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center py-16 text-[var(--color-text-faint)]">
                  no envelopes yet — fire something through the mesh
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && (
        <Drawer
          evt={selected}
          state={state}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

function Header({
  streamStatus,
  paused,
  onPause,
  onClear,
  count,
  total,
}: {
  streamStatus: string;
  paused: boolean;
  onPause: () => void;
  onClear: () => void;
  count: number;
  total: number;
}) {
  return (
    <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center gap-4">
      <div>
        <div className="text-[15px] font-medium tracking-tight">Live Logs</div>
        <div className="text-[11px] text-[var(--color-text-faint)] mono mt-0.5">
          envelopes flowing through core · {count}/{total} shown
        </div>
      </div>
      <div className="ml-auto flex items-center gap-2">
        <span
          className={`mono text-[11px] px-2 py-1 rounded border ${
            streamStatus === "open"
              ? "border-[var(--color-ok)]/40 text-[var(--color-ok)]"
              : "border-[var(--color-border)] text-[var(--color-text-muted)]"
          }`}
        >
          {streamStatus === "open" ? "● live" : `○ ${streamStatus}`}
        </span>
        <button
          onClick={onPause}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--color-border)] text-[12px] text-[var(--color-text-muted)] hover:text-white hover:border-[var(--color-border-strong)] transition-colors"
        >
          {paused ? <Play size={12} /> : <Pause size={12} />}
          {paused ? "resume" : "pause"}
        </button>
        <button
          onClick={onClear}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--color-border)] text-[12px] text-[var(--color-text-muted)] hover:text-white hover:border-[var(--color-border-strong)] transition-colors"
        >
          <Trash2 size={12} /> clear
        </button>
      </div>
    </div>
  );
}

function FilterSelect({
  value,
  onChange,
  options,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  label: string;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="bg-[var(--color-surface)] border border-[var(--color-border)] px-2.5 py-1.5 rounded text-[12px] mono text-[var(--color-text-muted)] hover:border-[var(--color-border-strong)] focus:outline-none focus:border-[var(--color-accent)]"
    >
      <option value="">all {label}s</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  );
}

function Row({
  evt,
  onClick,
  isSelected,
}: {
  evt: EnvelopeEvent;
  onClick: () => void;
  isSelected: boolean;
}) {
  const time = new Date(evt.ts).toLocaleTimeString("en-US", { hour12: false });
  const ms = evt.ts.split(".")[1]?.slice(0, 3) ?? "000";
  return (
    <tr
      onClick={onClick}
      className={`row-enter cursor-pointer border-b border-[var(--color-border)]/50 transition-colors ${
        isSelected
          ? "bg-[var(--color-accent-soft)]"
          : "hover:bg-[var(--color-surface)]"
      }`}
    >
      <td className="py-2 px-3 text-[var(--color-text-faint)]">
        {time}.{ms}
      </td>
      <td>
        <DirectionDot direction={evt.direction} />
      </td>
      <td className="text-[var(--color-text)]">
        <span className="text-[var(--color-text-muted)]">{evt.from_node}</span>
        <span className="text-[var(--color-text-faint)] mx-1.5">→</span>
        <span>{evt.to_surface}</span>
      </td>
      <td className="text-[var(--color-text-faint)]">
        {(evt.msg_id ?? "").slice(0, 8)}
      </td>
      <td className="text-right py-2 px-3">
        <StatusPill status={evt.route_status} signatureValid={evt.signature_valid} />
      </td>
    </tr>
  );
}

function DirectionDot({ direction }: { direction: string }) {
  const color = direction === "in" ? "var(--color-accent)" : "#a78bfa";
  return (
    <span
      className="inline-block w-1.5 h-1.5 rounded-full"
      style={{ background: color }}
      title={direction}
    />
  );
}

function StatusPill({
  status,
  signatureValid,
}: {
  status: string;
  signatureValid: boolean;
}) {
  if (!signatureValid) {
    return (
      <span className="inline-flex items-center gap-1 text-[var(--color-warn)] text-[11px]">
        <AlertTriangle size={11} /> {status}
      </span>
    );
  }
  if (status === "routed") {
    return (
      <span className="inline-flex items-center gap-1 text-[var(--color-ok)] text-[11px]">
        <CheckCircle2 size={11} /> routed
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-[var(--color-bad)] text-[11px]">
      <XCircle size={11} /> {status}
    </span>
  );
}

function Drawer({
  evt,
  state,
  onClose,
}: {
  evt: EnvelopeEvent;
  state: AdminState;
  onClose: () => void;
}) {
  const targetNodeId = evt.to_surface?.split(".")[0];
  const targetSurfaceName = evt.to_surface?.split(".")[1];
  const fromNode = state.nodes.find((n) => n.id === evt.from_node);
  const targetNode = state.nodes.find((n) => n.id === targetNodeId);
  const surface = targetNode?.surfaces.find((s) => s.name === targetSurfaceName);
  const edge: Relationship | undefined = state.relationships.find(
    (r) => r.from === evt.from_node && r.to === evt.to_surface,
  );

  return (
    <div className="absolute right-0 top-0 h-full w-[520px] bg-[var(--color-surface)] border-l border-[var(--color-border)] shadow-2xl flex flex-col z-10">
      <div className="px-5 py-4 border-b border-[var(--color-border)] flex items-center">
        <div>
          <div className="text-[13px] font-medium">Envelope detail</div>
          <div className="mono text-[11px] text-[var(--color-text-faint)] mt-0.5">
            {evt.msg_id}
          </div>
        </div>
        <button
          onClick={onClose}
          className="ml-auto text-[var(--color-text-muted)] hover:text-white"
        >
          <X size={16} />
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-5 space-y-5">
        <Section title="Routing">
          <Row2 k="from" v={`${evt.from_node} (${fromNode?.kind ?? "?"})`} />
          <Row2 k="to" v={evt.to_surface ?? "?"} />
          <Row2 k="direction" v={evt.direction} />
          <Row2 k="kind" v={evt.kind ?? "?"} />
          <Row2 k="status" v={evt.route_status} />
          <Row2
            k="signature"
            v={evt.signature_valid ? "valid" : "INVALID"}
          />
          <Row2 k="correlation" v={evt.correlation_id ?? "?"} />
          <Row2 k="ts" v={evt.ts} />
        </Section>

        <Section title="Edge (relationship that authorized this)">
          {edge ? (
            <div className="mono text-[12px] text-[var(--color-ok)]">
              ✓ {edge.from} → {edge.to}
            </div>
          ) : (
            <div className="mono text-[12px] text-[var(--color-bad)]">
              ✗ no matching edge
            </div>
          )}
        </Section>

        <Section title="Payload">
          <pre className="mono text-[11px] bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded p-3 overflow-x-auto whitespace-pre-wrap break-words text-[var(--color-text)]">
            {JSON.stringify(evt.payload, null, 2)}
          </pre>
        </Section>

        {evt.wrapped && (
          <Section title="Wrapped envelope">
            <pre className="mono text-[11px] bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded p-3 overflow-x-auto whitespace-pre-wrap break-words text-[var(--color-text-muted)]">
              {JSON.stringify(evt.wrapped, null, 2)}
            </pre>
          </Section>
        )}

        <Section title="Surface">
          {surface ? (
            <div className="space-y-1.5">
              <Row2 k="type" v={surface.type} />
              <Row2 k="invocation_mode" v={surface.invocation_mode} />
              <pre className="mono text-[11px] bg-[var(--color-surface-2)] border border-[var(--color-border)] rounded p-3 overflow-x-auto whitespace-pre-wrap break-words text-[var(--color-text-muted)] mt-2">
                {JSON.stringify(surface.schema, null, 2)}
              </pre>
            </div>
          ) : (
            <div className="text-[12px] text-[var(--color-text-faint)]">
              surface not found
            </div>
          )}
        </Section>

        <Section title="Endpoints">
          <div className="space-y-1.5">
            <NodeMini node={fromNode ?? null} label="sender" />
            <NodeMini node={targetNode ?? null} label="receiver" />
          </div>
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: any }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-2">
        {title}
      </div>
      {children}
    </div>
  );
}

function Row2({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3 mono text-[12px] py-0.5">
      <span className="text-[var(--color-text-faint)]">{k}</span>
      <span className="text-[var(--color-text)] text-right break-all">{v}</span>
    </div>
  );
}

function NodeMini({
  node,
  label,
}: {
  node: { id: string; kind: string; connected: boolean } | null;
  label: string;
}) {
  if (!node) {
    return (
      <div className="border border-[var(--color-border)] rounded p-2.5 text-[12px] text-[var(--color-text-faint)] mono">
        {label}: unknown
      </div>
    );
  }
  return (
    <div className="border border-[var(--color-border)] rounded p-2.5 mono text-[12px] flex items-center gap-2">
      <span className="text-[var(--color-text-faint)] text-[10px] uppercase">
        {label}
      </span>
      <span className="text-[var(--color-text)]">{node.id}</span>
      <span className="text-[var(--color-text-faint)]">·</span>
      <span className="text-[var(--color-text-muted)]">{node.kind}</span>
      <span className="ml-auto">
        {node.connected ? (
          <span className="text-[var(--color-ok)]">● connected</span>
        ) : (
          <span className="text-[var(--color-bad)]">○ offline</span>
        )}
      </span>
    </div>
  );
}
