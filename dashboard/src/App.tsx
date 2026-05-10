import { useEffect, useMemo, useState } from "react";
import { Activity, Network, Boxes, EyeOff, RefreshCw, Cpu } from "lucide-react";
import LiveLogs from "./pages/LiveLogs";
import MeshBuilder from "./pages/MeshBuilder";
import SurfaceInspector from "./pages/SurfaceInspector";
import UiVisibility from "./pages/UiVisibility";
import Processes from "./pages/Processes";
import { getState, reload as adminReload } from "./lib/api";
import type { AdminState } from "./lib/types";

type PageKey = "logs" | "mesh" | "surfaces" | "visibility" | "processes";

const PAGES: { key: PageKey; label: string; icon: any }[] = [
  { key: "logs", label: "Live Logs", icon: Activity },
  { key: "mesh", label: "Mesh Builder", icon: Network },
  { key: "surfaces", label: "Surfaces", icon: Boxes },
  { key: "processes", label: "Processes", icon: Cpu },
  { key: "visibility", label: "UI Visibility", icon: EyeOff },
];

export default function App() {
  const [page, setPage] = useState<PageKey>("logs");
  const [state, setState] = useState<AdminState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloading, setReloading] = useState(false);

  async function refresh() {
    try {
      const s = await getState();
      setState(s);
      setError(null);
    } catch (e: any) {
      setError(e.message ?? String(e));
    }
  }

  useEffect(() => {
    refresh();
    const int = setInterval(refresh, 5000);
    return () => clearInterval(int);
  }, []);

  async function onReload() {
    setReloading(true);
    try {
      await adminReload();
      await refresh();
    } finally {
      setReloading(false);
    }
  }

  const stats = useMemo(() => {
    if (!state) return null;
    const connected = state.nodes.filter((n) => n.connected).length;
    return {
      nodes: state.nodes.length,
      connected,
      edges: state.relationships.length,
    };
  }, [state]);

  return (
    <div className="flex h-screen w-screen text-[14px]">
      <aside className="w-[220px] shrink-0 border-r border-[var(--color-border)] flex flex-col bg-[var(--color-surface)]">
        <div className="px-4 py-5 border-b border-[var(--color-border)]">
          <div className="flex items-center gap-2.5">
            <div className="w-2 h-2 rounded-full bg-[var(--color-accent)] live-dot" />
            <span className="mono text-[13px] tracking-tight font-medium">
              RAVEN_MESH
            </span>
          </div>
          <div className="text-[11px] text-[var(--color-text-faint)] mt-1.5 mono">
            v0 control plane
          </div>
        </div>

        <nav className="flex-1 py-2">
          {PAGES.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setPage(key)}
              className={`w-full flex items-center gap-3 px-4 py-2.5 text-left text-[13px] transition-colors duration-150 ${
                page === key
                  ? "bg-[var(--color-surface-2)] text-white border-l-2 border-[var(--color-accent)]"
                  : "text-[var(--color-text-muted)] hover:text-white border-l-2 border-transparent"
              }`}
            >
              <Icon size={15} strokeWidth={1.5} />
              {label}
            </button>
          ))}
        </nav>

        <div className="p-4 border-t border-[var(--color-border)] text-[11px] mono text-[var(--color-text-faint)] space-y-1">
          {stats ? (
            <>
              <div className="flex justify-between">
                <span>nodes</span>
                <span className="text-[var(--color-text)]">
                  {stats.connected}/{stats.nodes}
                </span>
              </div>
              <div className="flex justify-between">
                <span>edges</span>
                <span className="text-[var(--color-text)]">{stats.edges}</span>
              </div>
            </>
          ) : (
            <span>{error ?? "connecting..."}</span>
          )}
          <button
            onClick={onReload}
            disabled={reloading}
            className="mt-2 w-full flex items-center justify-center gap-1.5 py-1.5 rounded border border-[var(--color-border)] hover:border-[var(--color-border-strong)] text-[var(--color-text-muted)] hover:text-white transition-colors"
          >
            <RefreshCw size={11} className={reloading ? "animate-spin" : ""} />
            <span>reload manifest</span>
          </button>
        </div>
      </aside>

      <main className="flex-1 overflow-hidden bg-[var(--color-bg)] gradient-edge">
        {error && (
          <div className="px-6 py-2 bg-[var(--color-bad)]/10 border-b border-[var(--color-bad)]/30 text-[12px] mono text-[var(--color-bad)]">
            {error}
          </div>
        )}
        {!state && !error && (
          <div className="h-full flex items-center justify-center text-[var(--color-text-muted)] mono text-[13px]">
            connecting to core...
          </div>
        )}
        {state && page === "logs" && <LiveLogs state={state} />}
        {state && page === "mesh" && <MeshBuilder state={state} onReload={refresh} />}
        {state && page === "surfaces" && <SurfaceInspector state={state} />}
        {state && page === "processes" && <Processes />}
        {state && page === "visibility" && (
          <UiVisibility state={state} refresh={refresh} />
        )}
      </main>
    </div>
  );
}
