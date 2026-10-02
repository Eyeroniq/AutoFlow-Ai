import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { guard } from "@/lib/route-guard";
import { verifySessionToken } from "@/lib/session-token";

// The server-side route guard (named `proxy` since Next.js 16; it was `middleware`). A protected page is
// never rendered for a browser without a valid session: it gets a redirect to /login first, before any
// client JavaScript runs. The session is the API's signed httpOnly cookie (set on sign-in and refresh);
// the signature is checked here with the shared JWT_SECRET. Authorization of data stays with the API,
// which checks the bearer token on every request; this guard decides who is shown the application.
const COOKIE = process.env.SESSION_COOKIE_NAME ?? "flowforge_session";

export async function proxy(request: NextRequest) {
  const signedIn = await verifySessionToken(request.cookies.get(COOKIE)?.value, process.env.JWT_SECRET);
  const decision = guard(request.nextUrl.pathname, request.nextUrl.search, signedIn);
  if (decision.action === "next") return NextResponse.next();
  const response = NextResponse.redirect(new URL(decision.to, request.url));
  response.headers.set("Cache-Control", "no-store");
  return response;
}

export const config = {
  // Pages only: not Next's own assets, images, or files in public/.
  matcher: ["/((?!_next/static|_next/image|favicon.ico|.*\\.(?:png|jpg|jpeg|svg|gif|webp|ico|txt|xml|webmanifest|woff2?)$).*)"],
};
