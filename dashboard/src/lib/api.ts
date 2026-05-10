import type { AdminState, EnvelopeEvent } from "./types";

const BASE = "/api/admin";

export async function getState(): Promise<AdminState> {
  const r = await fetch(`${BASE}/state`);
  if (!r.ok) throw new Error(`state ${r.status}`);
  return r.json();
}

export async function getUiState(): Promise<{ node_status: AdminState["node_status"] }> {
  const r = await fetch(`${BASE}/ui_state`);
  if (!r.ok) throw new Error(`ui_state ${r.status}`);
  return r.json();
}

export async function adminInvoke(args: {
  from_node: string;
  target: string;
  payload: Record<string, any>;
}): Promise<any> {
  const r = await fetch(`${BASE}/invoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  return r.json().then((data) => ({ status: r.status, data }));
}

export async function reload(): Promise<any> {
  const r = await fetch(`${BASE}/reload`, { method: "POST" });
  return r.json();
}

export async function writeManifest(yaml: string): Promise<any> {
  const r = await fetch(`${BASE}/manifest`, {
    method: "POST",
    headers: { "Content-Type": "text/plain" },
    body: yaml,
  });
  return { status: r.status, data: await r.json() };
}

/**
 * Subscribe to /v0/admin/stream via fetch+ReadableStream so the proxy can
 * inject auth headers for us. Returns an unsubscribe function.
 */
export function subscribeStream(
  onEvent: (evt: EnvelopeEvent) => void,
  onStatus?: (status: "connecting" | "open" | "closed" | "error") => void,
): () => void {
  const ctrl = new AbortController();
  let cancelled = false;

  (async () => {
    onStatus?.("connecting");
    while (!cancelled) {
      try {
        const r = await fetch(`${BASE}/stream`, { signal: ctrl.signal });
        if (!r.ok || !r.body) {
          onStatus?.("error");
          await sleep(1000);
          continue;
        }
        onStatus?.("open");
        const reader = r.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        while (!cancelled) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const block = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const lines = block.split("\n");
            let event = "";
            const datas: string[] = [];
            for (const line of lines) {
              if (line.startsWith("event:")) event = line.slice(6).trim();
              else if (line.startsWith("data:")) datas.push(line.slice(5).replace(/^ /, ""));
            }
            if (event === "envelope" && datas.length) {
              try {
                const data = JSON.parse(datas.join("\n"));
                onEvent(data);
              } catch (e) {
                /* ignore */
              }
            }
          }
        }
        onStatus?.("closed");
      } catch (e: any) {
        if (e.name === "AbortError") return;
        onStatus?.("error");
        await sleep(1500);
      }
    }
  })();

  return () => {
    cancelled = true;
    ctrl.abort();
  };
}

function sleep(ms: number) {
  return new Promise((res) => setTimeout(res, ms));
}
