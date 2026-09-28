import { API_URL } from "./config";
import { isTokenExpired, tokenStorage } from "./token-storage";
import type { LoginPayload, RegisterPayload, TokenResponse, User } from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
    public readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  /** Attach the stored access token, refreshing it when it has expired or is rejected. */
  auth?: boolean;
  signal?: AbortSignal;
}

interface ValidationIssue {
  loc?: (string | number)[];
  msg?: string;
}

/** Turn FastAPI's `detail` (a string, or a list of validation issues) into one message. */
function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = (detail as ValidationIssue[])
      .map((issue) => {
        const field = issue.loc?.filter((part) => part !== "body").join(".");
        const msg = issue.msg?.replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .filter(Boolean);
    if (messages.length) return messages.join("; ");
  }
  return null;
}

async function send(path: string, options: RequestOptions, token?: string | null): Promise<Response> {
  const { method = "GET", body, signal } = options;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    return await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, `Can't reach the API at ${API_URL}. Is the backend running?`);
  }
}

async function parse<T>(response: Response): Promise<T> {
  const data: unknown = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const detail = (data as { detail?: unknown } | null)?.detail;
    throw new ApiError(response.status, describeDetail(detail) ?? `Request failed (${response.status})`, data);
  }
  return data as T;
}

let refreshInFlight: Promise<boolean> | null = null;

/**
 * Exchange the stored refresh token for a new token pair. Concurrent callers share one
 * request. Resolves false (and clears the session if the API rejected the token) when
 * no new access token could be obtained.
 */
export function refreshSession(): Promise<boolean> {
  refreshInFlight ??= doRefresh().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

async function doRefresh(): Promise<boolean> {
  const refreshToken = tokenStorage.getRefreshToken();
  if (!refreshToken || isTokenExpired(refreshToken, 0)) {
    tokenStorage.clear();
    return false;
  }

  let response: Response;
  try {
    response = await send("/api/auth/refresh", { method: "POST", body: { refresh_token: refreshToken } });
  } catch {
    // Network trouble isn't proof the session is dead; keep the tokens.
    return false;
  }
  if (!response.ok) {
    if (response.status === 401 || response.status === 422) tokenStorage.clear();
    return false;
  }
  tokenStorage.setTokens((await response.json()) as TokenResponse);
  return true;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  if (!options.auth) return parse<T>(await send(path, options));

  // Skip a guaranteed 401 when the stored access token has visibly expired.
  const current = tokenStorage.getAccessToken();
  if ((!current || isTokenExpired(current)) && tokenStorage.getRefreshToken()) {
    await refreshSession();
  }

  const token = tokenStorage.getAccessToken();
  let response = await send(path, options, token);
  if (response.status === 401 && tokenStorage.getRefreshToken()) {
    // Another request may already have refreshed while this one was in flight.
    const refreshed = tokenStorage.getAccessToken() !== token || (await refreshSession());
    if (refreshed) response = await send(path, options, tokenStorage.getAccessToken());
  }
  return parse<T>(response);
}

export const api = {
  auth: {
    login: (payload: LoginPayload) =>
      request<TokenResponse>("/api/auth/login", { method: "POST", body: payload }),
    register: (payload: RegisterPayload) =>
      request<TokenResponse>("/api/auth/register", { method: "POST", body: payload }),
    me: (signal?: AbortSignal) => request<User>("/api/auth/me", { auth: true, signal }),
  },
};
