import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the Vite server proxies /api to a locally running backend, so
// the SPA and the API share an origin exactly as they do behind nginx in AKS.
export default defineConfig({
  plugins: [react()],
  // MSAL is most of the bundle; not worth splitting for an internal tool.
  build: { chunkSizeWarningLimit: 800 },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
