import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
  },
  build: {
    // recharts + react in one vendor chunk keeps the main bundle lean enough;
    // fine-grained splitting is not worth the cache complexity at this size.
    chunkSizeWarningLimit: 900,
  },
});
