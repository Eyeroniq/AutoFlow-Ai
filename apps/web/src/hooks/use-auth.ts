"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";

import { ApiError, api } from "@/lib/api";
import { isTokenExpired, tokenStorage } from "@/lib/token-storage";
import type { User } from "@/lib/types";

const noopSubscribe = () => () => {};

/** False during SSR and hydration, true once running in the browser. */
export function useHydrated(): boolean {
  return useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );
}

/** The stored access token, kept in sync across tabs. Always null on the server. */
export function useAccessToken(): string | null {
  return useSyncExternalStore(tokenStorage.subscribe, tokenStorage.getAccessToken, () => null);
}

function useRefreshToken(): string | null {
  return useSyncExternalStore(tokenStorage.subscribe, tokenStorage.getRefreshToken, () => null);
}

/**
 * True when the stored tokens can still authenticate: a live access token, or a live
 * refresh token the API client can trade for one. Null until hydrated.
 */
export function useHasSession(): boolean | null {
  const hydrated = useHydrated();
  const accessToken = useAccessToken();
  const refreshToken = useRefreshToken();
  if (!hydrated) return null;
  const usable = (token: string | null) => token !== null && !isTokenExpired(token);
  return usable(accessToken) || usable(refreshToken);
}

type CurrentUserState =
  | { status: "loading" }
  | { status: "authenticated"; user: User }
  | { status: "error"; message: string };

/**
 * Loads the current user for a protected page. The API client refreshes an expired
 * access token on its own; this redirects to /login only when there's no usable session
 * or the refresh itself was rejected.
 */
export function useCurrentUser(): CurrentUserState {
  const router = useRouter();
  const hasSession = useHasSession();
  const [state, setState] = useState<CurrentUserState>({ status: "loading" });

  useEffect(() => {
    if (hasSession === null) return;
    if (!hasSession) {
      tokenStorage.clear();
      router.replace("/login");
      return;
    }

    const controller = new AbortController();
    api.auth
      .me(controller.signal)
      .then((user) => setState({ status: "authenticated", user }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) {
          tokenStorage.clear();
          router.replace("/login");
          return;
        }
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Something went wrong",
        });
      });
    return () => controller.abort();
  }, [hasSession, router]);

  return state;
}

/** Sends already-signed-in visitors of /login and /register to the dashboard. */
export function useRedirectIfAuthenticated(to = "/dashboard"): void {
  const router = useRouter();
  const hasSession = useHasSession();

  useEffect(() => {
    if (hasSession) router.replace(to);
  }, [hasSession, router, to]);
}
