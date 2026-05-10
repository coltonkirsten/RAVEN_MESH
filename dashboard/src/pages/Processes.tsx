import { useEffect, useRef, useState } from "react";
import { Play, Square, RotateCw, RefreshCw, AlertTriangle } from "lucide-react";
import {
  getProcesses,
  spawnNode,
  stopNode,
  restartNode,
  reconcileMesh,
} from "../lib/api";
import type { ProcessInfo, ProcessesResponse } from "../lib/api";

const REFRESH_MS = 2000;

type BusyMap = Record<string, string | undefined>;

export default function Processes() {
  const [resp, setResp] = useState<ProcessesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<BusyMap>({});
  const [reconciling, setReconciling] = useState(false);
  const mounted = useRef(true);

  async function refresh() {
    try {
      const data = await getProcesses();
      if (!mounted.current) return;
      setResp(data);
      setError(null);
    } catch (e: any) {
      if (!mounted.current) return;
      setError(e.message ?? String(e));
    }
  }

  useEffect(() => {
    mounted.current = true;
    refresh();
    const int = setInterval(refresh, REFRESH_MS);
    return () => {
      mounted.current = false;
      clearInterval(int);
    };
  }, []);

  async function withBusy(node_id: string, label: string, fn: () => Promise<any>) {
    setBusy((b) => ({ ...b, [node_id]: label }));
    try {
      await fn();
      await refresh();
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setBusy((b) => {
        const { [node_id]: _, ...rest } = b;
        return rest;
      });
    }
  }

  async function onReconcile() {
    setReconciling(true);
    try {
      await reconcileMesh();
      await refresh();
    } catch (e: any) {
      setError(e.message ?? String(e));
    } finally {
      setReconciling(false);
    }
  }

  const supervisorEnabled = resp?.supervisor_enabled ?? true;
  const processes = resp?.processes ?? [];

  return (
    <div className="h-full flex flex-col">
      <div className="px-6 py-4 border-b border-[var(--color-border)] flex items-center gap-4">
        <div>
          <div className="text-[15px] font-medium tracking-tight">Processes</div>
          <div className="text-[11px] text-[var(--color-text-faint)] mono mt-0.5">
            child node lifecycle · {processes.length} tracked
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={onReconcile}
            disabled={!supervisorEnabled || reconciling}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--color-border)] text-[12px] text-[var(--color-text-muted)] hover:text-white hover:border-[var(--color-border-strong)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            <RefreshCw size={12} className={reconciling ? "animate-spin" : ""} />
            reconcile all
          </button>
        </div>
      </div>

      {!supervisorEnabled && (
        <div className="px-6 py-3 bg-[var(--color-warn)]/10 border-b border-[var(--color-warn)]/30 text-[12px] mono text-[var(--color-warn)] flex items-center gap-2">
          <AlertTriangle size={13} />
          Supervisor disabled. Boot core with --supervisor to manage processes.
        </div>
      )}

      {error && (
        <div className="px-6 py-2 bg-[var(--color-bad)]/10 border-b border-[var(--color-bad)]/30 text-[12px] mono text-[var(--color-bad)]">
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        <table className="w-full text-[12px] mono">
          <thead className="sticky top-0 bg-[var(--color-bg)]">
            <tr className="text-[var(--color-text-faint)] border-b border-[var(--color-border)]">
              <th className="text-left font-normal py-2 px-3">node_id</th>
              <th className="text-left font-normal w-[120px]">status</th>
              <th className="text-left font-normal w-[80px]">pid</th>
              <th className="text-left font-normal w-[100px]">uptime</th>
              <th className="text-left font-normal w-[80px]">restarts</th>
              <th className="text-left font-normal w-[80px]">exit</th>
              <th className="text-right font-normal py-2 px-3 w-[260px]">actions</th>
            </tr>
          </thead>
          <tbody>
            {processes.map((p) => (
              <ProcessRow
                key={p.node_id}
                p={p}
                busy={busy[p.node_id]}
                onStart={() => withBusy(p.node_id, "start", () => spawnNode(p.node_id))}
                onStop={() => withBusy(p.node_id, "stop", () => stopNode(p.node_id))}
                onRestart={() =>
                  withBusy(p.node_id, "restart", () => restartNode(p.node_id))
                }
              />
            ))}
            {processes.length === 0 && supervisorEnabled && (
              <tr>
                <td colSpan={7} className="text-center py-16 text-[var(--color-text-faint)]">
                  no processes tracked yet — spawn one or run reconcile
                </td>
              </tr>
            )}
            {!supervisorEnabled && (
              <tr>
                <td colSpan={7} className="text-center py-16 text-[var(--color-text-faint)]">
                  process management unavailable
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ProcessRow({
  p,
  busy,
  onStart,
  onStop,
  onRestart,
}: {
  p: ProcessInfo;
  busy: string | undefined;
  onStart: () => void;
  onStop: () => void;
  onRestart: () => void;
}) {
  const isRunning = p.status === "running" || p.status === "starting";
  const isStopped = p.status === "stopped" || p.status === "failed" || p.status === "crashed";
  return (
    <tr className="border-b border-[var(--color-border)]/50 hover:bg-[var(--color-surface)]">
      <td className="py-2 px-3 text-[var(--color-text)]">{p.node_id}</td>
      <td>
        <StatusBadge status={p.status} />
      </td>
      <td className="text-[var(--color-text-faint)]">{p.pid ?? "—"}</td>
      <td className="text-[var(--color-text-faint)]">
        {p.status === "running" ? humanizeSeconds(p.uptime_seconds) : "—"}
      </td>
      <td className="text-[var(--color-text-faint)]">{p.restart_count}</td>
      <td className="text-[var(--color-text-faint)]">
        {p.last_exit_code === null ? "—" : p.last_exit_code}
      </td>
      <td className="text-right py-2 px-3">
        <div className="inline-flex items-center gap-1.5">
          {isStopped && (
            <ActionButton
              icon={<Play size={11} />}
              label="start"
              onClick={onStart}
              disabled={!!busy}
              busyLabel={busy === "start" ? "starting" : undefined}
            />
          )}
          {isRunning && (
            <ActionButton
              icon={<Square size={11} />}
              label="stop"
              onClick={onStop}
              disabled={!!busy}
              busyLabel={busy === "stop" ? "stopping" : undefined}
            />
          )}
          <ActionButton
            icon={<RotateCw size={11} className={busy === "restart" ? "animate-spin" : ""} />}
            label="restart"
            onClick={onRestart}
            disabled={!!busy}
            busyLabel={busy === "restart" ? "restarting" : undefined}
          />
        </div>
      </td>
    </tr>
  );
}

function ActionButton({
  icon,
  label,
  onClick,
  disabled,
  busyLabel,
}: {
  icon: any;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  busyLabel?: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="inline-flex items-center gap-1 px-2 py-1 rounded border border-[var(--color-border)] text-[11px] text-[var(--color-text-muted)] hover:text-white hover:border-[var(--color-border-strong)] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
    >
      {icon}
      {busyLabel ?? label}
    </button>
  );
}

function StatusBadge({ status }: { status: string }) {
  const { color, dot } = statusStyle(status);
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px]"
      style={{ color }}
    >
      <span
        className="inline-block w-1.5 h-1.5 rounded-full"
        style={{ background: dot }}
      />
      {status}
    </span>
  );
}

function statusStyle(status: string): { color: string; dot: string } {
  switch (status) {
    case "running":
      return { color: "var(--color-ok)", dot: "var(--color-ok)" };
    case "crashed":
    case "failed":
      return { color: "var(--color-bad)", dot: "var(--color-bad)" };
    case "starting":
    case "restarting":
    case "stopping":
      return { color: "var(--color-warn)", dot: "var(--color-warn)" };
    default:
      return { color: "var(--color-text-muted)", dot: "var(--color-text-muted)" };
  }
}

function humanizeSeconds(s: number): string {
  if (!s || s < 1) return "<1s";
  const sec = Math.floor(s);
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const r = sec % 60;
  if (m < 60) return r ? `${m}m ${r}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  if (h < 24) return rm ? `${h}h ${rm}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const rh = h % 24;
  return rh ? `${d}d ${rh}h` : `${d}d`;
}
