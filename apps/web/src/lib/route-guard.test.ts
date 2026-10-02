import { describe, expect, it } from "vitest";

import { guard, safeNext } from "./route-guard";
import { verifySessionToken } from "./session-token";

// Minted by the API's own code path (python-jose, HS256): {sub, type: "session", exp: 2100-01-01}.
const SECRET = "unit-test-secret-0123456789";
const API_TOKEN =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEiLCJ0eXBlIjoic2Vzc2lvbiIsImlhdCI6MTcwMDAwMDAwMCwiZXhwIjo0MTAyNDQ0ODAwLCJqdGkiOiJhYmMifQ.i9o6ZQQ1lMzCu8YBt0sji_t9H5p1yQCgdgMcvegCm6Y";

function b64url(value: string | Uint8Array): string {
  const bytes = typeof value === "string" ? new TextEncoder().encode(value) : value;
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sign(claims: object, secret = SECRET, header: object = { alg: "HS256", typ: "JWT" }): Promise<string> {
  const body = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(claims))}`;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return `${body}.${b64url(new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body))))}`;
}

describe("verifySessionToken", () => {
  it("accepts a token signed by the API", async () => {
    expect(await verifySessionToken(API_TOKEN, SECRET)).toBe(true);
  });

  it("rejects a wrong secret, a missing token, or a missing secret", async () => {
    expect(await verifySessionToken(API_TOKEN, "another-secret")).toBe(false);
    expect(await verifySessionToken(undefined, SECRET)).toBe(false);
    expect(await verifySessionToken(API_TOKEN, undefined)).toBe(false);
    expect(await verifySessionToken("", SECRET)).toBe(false);
  });

  it("rejects an expired token and a token of another type", async () => {
    expect(await verifySessionToken(await sign({ type: "session", exp: 1000 }), SECRET, 2000)).toBe(false);
    expect(await verifySessionToken(await sign({ type: "access", exp: 4102444800 }), SECRET)).toBe(false);
    expect(await verifySessionToken(await sign({ type: "session" }), SECRET)).toBe(false);
    expect(await verifySessionToken(await sign({ type: "session", exp: 4102444800 }), SECRET)).toBe(true);
  });

  it("rejects forgeries: tampered claims, the none algorithm, and malformed values", async () => {
    const [header, , signature] = API_TOKEN.split(".");
    const tampered = `${header}.${b64url(JSON.stringify({ sub: "admin", type: "session", exp: 4102444800 }))}.${signature}`;
    expect(await verifySessionToken(tampered, SECRET)).toBe(false);
    const none = `${b64url(JSON.stringify({ alg: "none" }))}.${b64url(JSON.stringify({ type: "session", exp: 4102444800 }))}.`;
    expect(await verifySessionToken(none, SECRET)).toBe(false);
    expect(await verifySessionToken(await sign({ type: "session", exp: 4102444800 }, SECRET, { alg: "HS512" }), SECRET)).toBe(false);
    for (const junk of ["1", "a.b", "a.b.c.d", "!!!.???.***"]) expect(await verifySessionToken(junk, SECRET)).toBe(false);
  });
});

describe("guard", () => {
  it("sends a signed-out visitor from a protected page to sign-in, remembering where they were going", () => {
    expect(guard("/dashboard", "", false)).toEqual({ action: "redirect", to: "/login?next=%2Fdashboard" });
    expect(guard("/executions/abc", "?tab=logs", false)).toEqual({ action: "redirect", to: "/login?next=%2Fexecutions%2Fabc%3Ftab%3Dlogs" });
    for (const page of ["/knowledge", "/resume", "/integrations", "/pipelines/x", "/anything/else"]) {
      expect(guard(page, "", false).action).toBe("redirect");
    }
  });

  it("lets signed-in visitors through, and everyone reach the landing page and the sign-in pages", () => {
    expect(guard("/dashboard", "", true)).toEqual({ action: "next" });
    expect(guard("/", "", false)).toEqual({ action: "next" });
    expect(guard("/login", "", false)).toEqual({ action: "next" });
    expect(guard("/register", "", false)).toEqual({ action: "next" });
  });

  it("sends a signed-in visitor away from the sign-in pages, to where they were going if it is safe", () => {
    expect(guard("/login", "", true)).toEqual({ action: "redirect", to: "/dashboard" });
    expect(guard("/login", "?next=%2Fresume", true)).toEqual({ action: "redirect", to: "/resume" });
    expect(guard("/register", "?next=https%3A%2F%2Fevil.example", true)).toEqual({ action: "redirect", to: "/dashboard" });
  });

  it("only follows same-site next paths", () => {
    expect(safeNext("/resume")).toBe("/resume");
    expect(safeNext("/executions/1?x=2")).toBe("/executions/1?x=2");
    for (const bad of [null, "", "resume", "//evil.example", "https://evil.example", "/\\evil", "/login", "/register?x=1"]) {
      expect(safeNext(bad)).toBeNull();
    }
  });
});
