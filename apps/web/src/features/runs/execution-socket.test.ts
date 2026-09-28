import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  token: "token-1",
  refreshOk: true,
  refreshes: 0,
}));

vi.mock("@/lib/api", () => ({
  getFreshAccessToken: async () => auth.token,
  refreshSession: async () => {
    auth.refreshes += 1;
    if (auth.refreshOk) auth.token = `token-${auth.refreshes + 1}`;
    return auth.refreshOk;
  },
}));
vi.mock("@/lib/config", () => ({ WS_URL: "ws://api.test" }));

import { ExecutionSocket, type SocketStatus } from "./execution-socket";

/** Just enough of the browser WebSocket for the client. */
class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close(code = 1000) {
    this.readyState = 3;
    this.onclose?.({ code, reason: "" });
  }
  // test helpers
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  receive(message: object) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  drop(code: number, reason = "") {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }
}

const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1];
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function start() {
  const statuses: [SocketStatus, string | null][] = [];
  const messages: { type: string }[] = [];
  const socket = new ExecutionSocket("exec-1", {
    onMessage: (m) => messages.push(m),
    onStatus: (status, detail) => statuses.push([status, detail]),
  });
  return { socket, statuses, messages };
}

beforeEach(() => {
  FakeSocket.instances = [];
  Object.assign(auth, { token: "token-1", refreshOk: true, refreshes: 0 });
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ExecutionSocket", () => {
  it("authenticates with its first message, keeping the token out of the URL", async () => {
    const { socket, statuses, messages } = start();
    await socket.connect();
    expect(latest().url).toBe("ws://api.test/ws/executions/exec-1");
    latest().open();
    expect(JSON.parse(latest().sent[0])).toEqual({ type: "auth", token: "token-1" });
    latest().receive({ type: "snapshot", seq: 3, resync: false, execution: {} });
    expect(messages.map((m) => m.type)).toEqual(["snapshot"]);
    expect(statuses.map(([s]) => s)).toEqual(["connecting", "open"]);
  });

  it("stops with a clear message on 4404 (not found or not yours)", async () => {
    const { socket, statuses } = start();
    await socket.connect();
    latest().drop(4404);
    expect(statuses.at(-1)).toEqual(["error", "This execution doesn't exist or isn't yours."]);
    await flush();
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("refreshes the session once on 4401 and retries with the new token", async () => {
    const { socket, statuses } = start();
    await socket.connect();
    latest().drop(4401);
    await flush();
    await flush();
    expect(auth.refreshes).toBe(1);
    expect(FakeSocket.instances).toHaveLength(2);
    latest().open();
    expect(JSON.parse(latest().sent[0]).token).toBe("token-2");

    latest().drop(4401, "Invalid token");
    await flush();
    expect(auth.refreshes).toBe(1);
    expect(statuses.at(-1)).toEqual(["error", "Invalid token"]);
  });

  it("says to sign in again when the refresh fails", async () => {
    auth.refreshOk = false;
    const { socket, statuses } = start();
    await socket.connect();
    latest().drop(4401);
    await flush();
    await flush();
    expect(statuses.at(-1)).toEqual(["error", "Your session expired. Sign in again to follow this run."]);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("reconnects with backoff after a dropped connection", async () => {
    vi.useFakeTimers();
    const { socket, statuses } = start();
    await socket.connect();
    latest().open();
    latest().drop(1006);
    expect(statuses.at(-1)).toEqual(["reconnecting", "Connection lost; reconnecting in 1s…"]);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(2);
    latest().drop(1006);
    expect(statuses.at(-1)?.[1]).toBe("Connection lost; reconnecting in 1s…"); // 1000 ms
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(3);
    // A snapshot means it's back in sync: the next drop starts the backoff over.
    latest().open();
    latest().receive({ type: "snapshot", seq: 9, resync: true, execution: {} });
    latest().drop(1006);
    await vi.advanceTimersByTimeAsync(500);
    expect(FakeSocket.instances).toHaveLength(4);
  });

  it("closes quietly once the run has finished", async () => {
    const { socket, statuses } = start();
    await socket.connect();
    latest().open();
    latest().receive({ type: "execution.finished", seq: 12, status: "success" });
    latest().drop(1000);
    await flush();
    expect(statuses.at(-1)).toEqual(["closed", null]);
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("close() stops reconnecting", async () => {
    vi.useFakeTimers();
    const { socket } = start();
    await socket.connect();
    latest().open();
    latest().drop(1006);
    socket.close();
    await vi.advanceTimersByTimeAsync(20_000);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});
