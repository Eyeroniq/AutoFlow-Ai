// Verifying the API's signed session cookie (apps/api/app/core/security.py: create_session_token) in
// the route guard (src/proxy.ts). It is an HS256 JWT signed with the API's JWT_SECRET, so the guard
// can't be satisfied by a forged cookie. Web Crypto only: no dependency, and it runs in the proxy.

const encoder = new TextEncoder();

function base64UrlToBytes(text: string): Uint8Array<ArrayBuffer> {
  const base64 = text.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(text.length / 4) * 4, "=");
  const binary = atob(base64);
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function parseJson(bytes: Uint8Array): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return value !== null && typeof value === "object" ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * True when `token` is a session token signed with `secret` that has not expired.
 * Anything else (missing, malformed, wrong algorithm, wrong type, bad signature, expired) is false.
 */
export async function verifySessionToken(token: string | undefined, secret: string | undefined, nowSeconds = Date.now() / 1000): Promise<boolean> {
  if (!token || !secret) return false;
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  try {
    const header = parseJson(base64UrlToBytes(parts[0]));
    if (!header || header.alg !== "HS256") return false; // never trust the token to choose "none"
    const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    const valid = await crypto.subtle.verify("HMAC", key, base64UrlToBytes(parts[2]), encoder.encode(`${parts[0]}.${parts[1]}`));
    if (!valid) return false;
    const claims = parseJson(base64UrlToBytes(parts[1]));
    return !!claims && claims.type === "session" && typeof claims.exp === "number" && claims.exp > nowSeconds;
  } catch {
    return false;
  }
}
