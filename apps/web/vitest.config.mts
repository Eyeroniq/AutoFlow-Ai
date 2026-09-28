import { defineConfig } from "vitest/config";

// Unit tests for the editor's logic (store, autocomplete, schema forms, run state). They
// need no DOM, so they run in Node. Vite resolves the tsconfig `@/*` alias itself.
export default defineConfig({
  resolve: { tsconfigPaths: true },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
