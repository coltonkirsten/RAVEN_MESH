import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const CORE_URL = process.env.MESH_CORE_URL || "http://127.0.0.1:8000";
const PORT = Number(process.env.MESH_DASHBOARD_PORT ?? 5180);

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: PORT,
    host: "127.0.0.1",
    strictPort: true,
    proxy: {
      // Proxy admin calls so we can attach the X-Admin-Token header server-side
      // (EventSource cannot set custom headers).
      "/api/admin": {
        target: CORE_URL,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api\/admin/, "/v0/admin"),
        configure(proxy) {
          proxy.on("proxyReq", (proxyReq) => {
            const token = process.env.ADMIN_TOKEN || "admin-dev-token";
            if (!proxyReq.getHeader("X-Admin-Token")) {
              proxyReq.setHeader("X-Admin-Token", token);
            }
          });
        },
      },
    },
  },
});
