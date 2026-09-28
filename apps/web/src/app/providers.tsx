"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { toast } from "@/components/ui/toast";

function describe(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong";
}

let browserQueryClient: QueryClient | undefined;

function makeQueryClient() {
  return new QueryClient({
    // Failures surface as toasts unless a query/mutation opts out with meta.silent.
    queryCache: new QueryCache({
      onError: (error, query) => {
        if (!query.meta?.silent) toast.error("Couldn't load data", describe(error));
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _vars, _ctx, mutation) => {
        if (!mutation.meta?.silent) toast.error("Request failed", describe(error));
      },
    }),
    defaultOptions: {
      queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5_000 },
    },
  });
}

function getQueryClient() {
  // A fresh client per server render; one shared client in the browser.
  if (typeof window === "undefined") return makeQueryClient();
  browserQueryClient ??= makeQueryClient();
  return browserQueryClient;
}

export function Providers({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={getQueryClient()}>{children}</QueryClientProvider>;
}
