/**
 * Client for WS /ws/executions/{id} (see the README's "Real-time events").
 *
 * Authenticates with a first message {"type": "auth", "token": <access token>} so the token
 * stays out of URLs. Close codes: 1000 = finished (every event delivered); 4401 = auth
 * failed (refresh the token once and retry); 4404 = not found / not yours. Any other drop
 * reconnects with backoff, and the server's snapshot on reconnect replays what was missed.
 */
import { getFreshAccessToken, refreshSession } from "@/lib/api";
import { WS_URL } from "@/lib/config";
import type { ExecutionEvent } from "@/lib/types";

export type SocketStatus = "connecting" | "open" | "reconnecting" | "closed" | "error";

export interface SocketCallbacks {
  onMessage: (message: ExecutionEvent) => void;
  onStatus: (status: SocketStatus, detail: string | null) => void;
}

const MAX_RECONNECTS = 8;
const PING_MS = 25_000;

export class ExecutionSocket {
  private ws: WebSocket | null = null;
  private stopped = false;
  private finished = false;
  private attempts = 0;
  private refreshedAfter4401 = false;
  private pingTimer: ReturnType<typeof setInterval> | undefined;
  private retryTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    readonly executionId: string,
    private readonly callbacks: SocketCallbacks,
  ) {}

  async connect(): Promise<void> {
    if (this.stopped) return;
    this.callbacks.onStatus(this.attempts ? "reconnecting" : "connecting", null);
    const token = await getFreshAccessToken();
    if (this.stopped) return;
    if (!token) {
      this.callbacks.onStatus("error", "You're signed out. Sign in again to follow this run.");
      return;
    }
    const ws = new WebSocket(`${WS_URL}/ws/executions/${encodeURIComponent(this.executionId)}`);
    this.ws = ws;

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "auth", token }));
      this.callbacks.onStatus("open", null);
      this.pingTimer = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
      }, PING_MS);
    };

    ws.onmessage = (event) => {
      let message: ExecutionEvent;
      try {
        message = JSON.parse(String(event.data)) as ExecutionEvent;
      } catch {
        return;
      }
      if (message.type === "snapshot") this.attempts = 0; // authenticated and in sync
      if (message.type === "execution.finished") this.finished = true;
      this.callbacks.onMessage(message);
    };

    ws.onclose = (event) => {
      clearInterval(this.pingTimer);
      if (this.ws === ws) this.ws = null;
      if (this.stopped) return;
      if (event.code === 4401) {
        if (!this.refreshedAfter4401) {
          this.refreshedAfter4401 = true;
          void refreshSession().then((ok) =>
            ok ? this.connect() : this.callbacks.onStatus("error", "Your session expired. Sign in again to follow this run."),
          );
          return;
        }
        this.callbacks.onStatus("error", event.reason || "Not authorized to watch this run. Sign in again.");
        return;
      }
      if (event.code === 4404) {
        this.callbacks.onStatus("error", "This execution doesn't exist or isn't yours.");
        return;
      }
      if (this.finished || event.code === 1000) {
        this.callbacks.onStatus("closed", null);
        return;
      }
      this.attempts += 1;
      if (this.attempts > MAX_RECONNECTS) {
        this.callbacks.onStatus("error", "Lost the live connection. Reload the page to try again.");
        return;
      }
      const delay = Math.min(10_000, 500 * 2 ** (this.attempts - 1));
      this.callbacks.onStatus("reconnecting", `Connection lost; reconnecting in ${Math.ceil(delay / 1000)}s…`);
      this.retryTimer = setTimeout(() => void this.connect(), delay);
    };
  }

  close(): void {
    this.stopped = true;
    clearInterval(this.pingTimer);
    clearTimeout(this.retryTimer);
    const ws = this.ws;
    this.ws = null;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) ws.close(1000);
  }
}
