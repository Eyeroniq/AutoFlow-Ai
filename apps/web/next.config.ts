import type { NextConfig } from "next";

// Docker Desktop bind mounts (Windows/macOS hosts) don't deliver file-change events,
// so docker-compose sets this to make the dev server poll. Leave it unset when
// running `npm run dev` directly on the host — polling costs CPU.
const pollIntervalMs = Number(process.env.NEXT_DEV_POLL_INTERVAL_MS) || undefined;

const nextConfig: NextConfig = {
  // The production image (Dockerfile.prod) runs the small standalone server.
  ...(process.env.NEXT_STANDALONE === "1" && { output: "standalone" as const }),
  // The dev-mode badge sits over the canvas controls and shows up in screenshots.
  // Build and runtime errors still open the error overlay.
  devIndicators: false,
  ...(pollIntervalMs && { watchOptions: { pollIntervalMs } }),
};

export default nextConfig;
