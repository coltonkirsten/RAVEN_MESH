import { useEffect, useMemo, useState } from "react";
import { Eye, EyeOff, RefreshCw, ExternalLink } from "lucide-react";
import { adminInvoke, getUiState } from "../lib/api";
import type { AdminState } from "../lib/types";

type Props = { state: AdminState; refresh: () => void };

const UI_BEARING = ["webui_node", "human_node", "approval_node"];
const PORTS: Record<string, number> = {
  webui_node: 8801,
  human_node: 8802,
  approval_node: 8803,
};

export default function UiVisibility({ state, refresh }: Props) {
  const [uiState, setUiState] = useState<AdminState["node_status"]>(state.node_status);
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  async function pull() {
    try {
      const r = await getUiState();
      setUiState(r.node_status);
    } catch {
      /* ignore */
    }
  }

  useEffect(() => {
    pull();
    const int = setInterval(pull, 2000);
    return () => clearInterval(int);
  }, []);

  const targets = useMemo(() => {
    return UI_BEARING.filter((id) => state.nodes.some((n) => n.id === id));
  }, [state.nodes]);

  async function toggle(nodeId: string, action: "show" | "hide") {
    setBusy((b) => ({ ...b, [nodeId]: true }));
    try {
      // Pick a sender that has an edge to this surface.
      const target = `${nodeId}.ui_visibility`;
      const candidate = state.relationships.find((r) => r.to === target);
      const fromNode = candidate?.from ?? UI_BEARING.find((id) => id !== nodeId) ?? "human_node";
      const r = await adminInvoke({
        from_node: fromNode,
        target,
        payload: { action },
      });
      if (r.status !== 200) {
        alert(`failed: ${JSON.stringify(r.data)}`);
      }
      await pull();
      refresh();
    } finally {
      setBusy((b) => ({ ...b, [nodeId]: false }));
    }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mb-6 flex items-center">
        <div>
          <div className="text-[18px] font-medium tracking-tight">UI Visibility</div>
          <div className="text-[11px] text-[var(--color-text-faint)] mono mt-1">
            hide a node's web UI without taking the node down · sse stays open
          </div>
        </div>
        <button
          onClick={pull}
          className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded border border-[var(--color-border)] text-[12px] text-[var(--color-text-muted)] hover:text-white"
        >
          <RefreshCw size={12} /> refresh
        </button>
      </div>

      <div className="grid grid-cols-1 gap-4 max-w-[640px]">
        {targets.map((nodeId) => {
          const status = uiState[nodeId];
          const visible = status?.visible ?? true;
          const port = PORTS[nodeId];
          return (
            <div
              key={nodeId}
              className="border border-[var(--color-border)] rounded-lg bg-[var(--color-surface)] p-5"
            >
              <div className="flex items-center gap-3">
                <span
                  className={`w-2 h-2 rounded-full ${
                    visible ? "bg-[var(--color-ok)]" : "bg-[var(--color-bad)]"
                  }`}
                />
                <div className="flex-1">
                  <div className="mono text-[14px]">{nodeId}</div>
                  <div className="mono text-[11px] text-[var(--color-text-faint)] mt-1">
                    {visible ? "visible" : "hidden"}
                    {status?.ts && (
                      <> · {new Date(status.ts).toLocaleTimeString("en-US", { hour12: false })}</>
                    )}
                  </div>
                </div>
                {port && (
                  <a
                    href={`http://127.0.0.1:${port}`}
                    target="_blank"
                    rel="noreferrer"
                    className="flex items-center gap-1 mono text-[11px] text-[var(--color-text-muted)] hover:text-[var(--color-accent)]"
                  >
                    :{port} <ExternalLink size={11} />
                  </a>
                )}
                <button
                  onClick={() => toggle(nodeId, visible ? "hide" : "show")}
                  disabled={busy[nodeId]}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-[12px] font-medium transition-colors ${
                    visible
                      ? "border border-[var(--color-bad)]/40 text-[var(--color-bad)] hover:bg-[var(--color-bad)]/10"
                      : "border border-[var(--color-ok)]/40 text-[var(--color-ok)] hover:bg-[var(--color-ok)]/10"
                  }`}
                >
                  {visible ? (
                    <>
                      <EyeOff size={12} /> hide
                    </>
                  ) : (
                    <>
                      <Eye size={12} /> show
                    </>
                  )}
                </button>
              </div>
            </div>
          );
        })}
        {targets.length === 0 && (
          <div className="text-[12px] text-[var(--color-text-faint)] mono">
            no UI-bearing nodes registered yet
          </div>
        )}
      </div>
    </div>
  );
}
