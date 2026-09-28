"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { ExecutionDetail } from "@/lib/types";

import { ExecutionSocket, type SocketStatus } from "./execution-socket";
import { fromExecution, idleRun, isTerminal, reduceRun, type RunState } from "./run-state";

export interface LiveExecution {
  execution: ExecutionDetail | undefined;
  run: RunState;
  error: unknown;
  missing: boolean;
  connection: { status: SocketStatus | "idle"; detail: string | null };
  refetch: () => void;
}

/**
 * An execution from GET /api/executions/{id}, kept live over the WebSocket while it runs.
 * The socket's snapshot replays anything that happened before it connected.
 */
export function useLiveExecution(id: string): LiveExecution {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["execution", id],
    queryFn: ({ signal }) => api.executions.get(id, signal),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
    meta: { silent: true },
  });
  const [live, setLive] = useState<RunState | null>(null);
  const [connection, setConnection] = useState<LiveExecution["connection"]>({ status: "idle", detail: null });

  const running = query.data ? !isTerminal(query.data.status) : false;

  useEffect(() => {
    if (!running) return;
    const socket = new ExecutionSocket(id, {
      onMessage: (message) => {
        setLive((state) => reduceRun(state ?? idleRun, message));
        // The row now has the final output, timings, and worker: reload it once.
        if (message.type === "execution.finished") void queryClient.invalidateQueries({ queryKey: ["execution", id] });
      },
      onStatus: (status, detail) => setConnection({ status, detail }),
    });
    void socket.connect();
    return () => socket.close();
  }, [id, running, queryClient]);

  // Live state while the run is going; the stored row once it's done.
  const run = running && live ? live : query.data ? fromExecution(query.data) : idleRun;

  return {
    execution: query.data,
    run,
    error: query.error,
    missing: query.error instanceof ApiError && query.error.status === 404,
    connection,
    refetch: () => void query.refetch(),
  };
}
