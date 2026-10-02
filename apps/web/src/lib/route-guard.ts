// What the route guard (src/proxy.ts) decides, kept free of Next so it can be unit tested.

/** Pages anyone may open. Everything else needs a signed-in session. */
const PUBLIC_PAGES = ["/"];
/** Sign-in pages: a signed-in visitor is sent on to the dashboard instead. */
const AUTH_PAGES = ["/login", "/register"];

export type GuardDecision = { action: "next" } | { action: "redirect"; to: string };

function isUnder(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`);
}

/** Only same-site paths survive as a post-login destination (no open redirect). */
export function safeNext(value: string | null | undefined): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return null;
  if (AUTH_PAGES.some((page) => isUnder(value.split(/[?#]/)[0], page))) return null;
  return value;
}

export function guard(pathname: string, search: string, signedIn: boolean): GuardDecision {
  if (AUTH_PAGES.some((page) => isUnder(pathname, page))) {
    return signedIn ? { action: "redirect", to: safeNext(new URLSearchParams(search).get("next")) ?? "/dashboard" } : { action: "next" };
  }
  if (PUBLIC_PAGES.includes(pathname)) return { action: "next" };
  if (signedIn) return { action: "next" };
  const target = `${pathname}${search}`;
  return { action: "redirect", to: `/login?next=${encodeURIComponent(target)}` };
}
