import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The gateway (127.0.0.1:5120) serves the built UI itself. In dev, this server (5121) proxies the API paths to it so
// the UI always talks same-origin.
const API = process.env.SCW_API_ORIGIN ?? "http://127.0.0.1:5120";
const proxy = Object.fromEntries(
  ["/v1", "/metrics", "/healthz", "/readyz", "/health", "/capabilities", "/runs"].map((p) => [p, { target: API, changeOrigin: false }]),
);

export default defineConfig({
  plugins: [react()],
  server: { port: 5121, host: "127.0.0.1", strictPort: true, proxy },
  preview: { port: 5121, host: "127.0.0.1", strictPort: true, proxy },
});
