/// <reference types="vitest/config" />
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { svelteTesting } from "@testing-library/svelte/vite";
import { defineConfig } from "vite";

// The admin API in development. Overridable because the API's port is its
// own decision, not this bundle's; in production nothing is proxied -- the
// same origin that serves these files serves /api.
const ADMIN_API_ORIGIN = process.env.ADMIN_API_ORIGIN ?? "http://127.0.0.1:8083";

export default defineConfig({
  plugins: [svelte(), svelteTesting()],
  // Relative asset URLs, so the bundle works wherever the API chooses to
  // mount it -- "/", "/console/", anywhere. An absolute base is a second
  // thing that has to be kept in step with the server, and the failure is
  // silent: a blank page with two 404s in the network tab.
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // One operator, one tab, on a LAN. Splitting this bundle buys nothing
    // and makes the served file list something the API has to enumerate.
    sourcemap: true,
  },
  server: {
    port: 5174,
    proxy: {
      "/api": { target: ADMIN_API_ORIGIN, changeOrigin: true },
    },
  },
  test: {
    // Node by default: domain and view-model tests need no DOM, and that is
    // the point of the view-models. A test that renders a view asks for
    // jsdom in its own first line.
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
