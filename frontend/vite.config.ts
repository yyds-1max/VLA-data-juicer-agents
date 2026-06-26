import react from "@vitejs/plugin-react";
import { defineConfig, type UserConfig } from "vite";
import type { InlineConfig } from "vitest";

const config: UserConfig & { test: InlineConfig } = {
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
};

export default defineConfig(config);
