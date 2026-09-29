import { defineConfig, devices } from "@playwright/test";

// End-to-end tests against the running Docker stack (docker compose up -d):
// the web app on :3000 talking to the real API, worker, Postgres, and Redis.
// No mocks: the run and deploy tests call Gemini (Groq when Gemini is overloaded) and send
// real email through SMTP_USER.
export default defineConfig({
  testDir: "./e2e",
  // Tests share the demo account, so they run one at a time.
  workers: 1,
  fullyParallel: false,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  retries: 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } }],
});
