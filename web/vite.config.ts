import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The UI talks to the gateway at VITE_API_URL (default http://127.0.0.1:5120). Dev server: 127.0.0.1:5121.
export default defineConfig({
  plugins: [react()],
  server: { port: 5121, host: "127.0.0.1", strictPort: true },
  preview: { port: 5121, host: "127.0.0.1", strictPort: true },
});
