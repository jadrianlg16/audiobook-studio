import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// In dev the two backends are proxied so the app can always call the same
// relative paths it uses in production behind nginx: "/api" (gateway) and
// "/studio" (studio service). Both hosts are overridable by env so this file
// carries no machine-specific value.
const GATEWAY = process.env.VITE_GATEWAY ?? "http://localhost:8080";
const STUDIO = process.env.VITE_STUDIO ?? "http://localhost:5174";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target: GATEWAY,
        changeOrigin: true,
      },
      // The studio serves the same paths without the prefix, so strip it.
      "/studio": {
        target: STUDIO,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/studio/, ""),
      },
    },
  },
});
