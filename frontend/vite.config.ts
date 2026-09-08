import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build directly into the FastAPI static dir so the API serves the compiled
// frontend same-origin (SPEC section 2). emptyOutDir keeps stale assets from
// surviving across releases.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/app/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8771",
    },
  },
});
