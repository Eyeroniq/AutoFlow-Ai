import type { NextConfig } from "next";

// Docker Desktop bind mounts (Windows/macOS hosts) don't deliver file-change events,
// so docker-compose sets this to make the dev server poll. Leave it unset when
// running `npm run dev` directly on the host — polling costs CPU.
const pollIntervalMs = Number(process.env.NEXT_DEV_POLL_INTERVAL_MS) || undefined;

const nextConfig: NextConfig = {
  ...(pollIntervalMs && { watchOptions: { pollIntervalMs } }),
};

export default nextConfig;
