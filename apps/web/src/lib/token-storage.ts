import type { TokenResponse } from "./types";

// Phase 1 keeps tokens in localStorage. Moving to httpOnly cookies later only
// requires changing this module and the Authorization header in api.ts.
const ACCESS_KEY = "flowforge.access_token";
const REFRESH_KEY = "flowforge.refresh_token";
const CHANGE_EVENT = "flowforge:auth-change";

function read(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function notify() {
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

export const tokenStorage = {
  getAccessToken: () => read(ACCESS_KEY),
  getRefreshToken: () => read(REFRESH_KEY),

  setTokens(tokens: Pick<TokenResponse, "access_token" | "refresh_token">) {
    try {
      window.localStorage.setItem(ACCESS_KEY, tokens.access_token);
      window.localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    } finally {
      notify();
    }
  },

  clear() {
    if (read(ACCESS_KEY) === null && read(REFRESH_KEY) === null) return;
    try {
      window.localStorage.removeItem(ACCESS_KEY);
      window.localStorage.removeItem(REFRESH_KEY);
    } finally {
      notify();
    }
  },

  /** Subscribe to token changes in this tab and in other tabs. */
  subscribe(callback: () => void): () => void {
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === ACCESS_KEY || event.key === REFRESH_KEY) callback();
    };
    window.addEventListener(CHANGE_EVENT, callback);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(CHANGE_EVENT, callback);
      window.removeEventListener("storage", onStorage);
    };
  },
};

/**
 * Client-side expiry check so we can skip a round trip with an obviously stale token.
 * This does not verify the signature — the API remains the authority.
 */
export function isTokenExpired(token: string, skewSeconds = 10): boolean {
  try {
    const payload = token.split(".")[1];
    if (!payload) return true;
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    const { exp } = JSON.parse(json) as { exp?: number };
    return typeof exp !== "number" || exp * 1000 <= Date.now() + skewSeconds * 1000;
  } catch {
    return true;
  }
}
