import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/health": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/graph": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/inventory": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
