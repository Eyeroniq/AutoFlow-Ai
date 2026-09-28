"use client";

import { useRouter } from "next/navigation";

import { Logo } from "@/components/logo";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-auth";
import { tokenStorage } from "@/lib/token-storage";

// Placeholder for Phase 1: proves the protected route + /api/auth/me round trip.
export function DashboardView() {
  const router = useRouter();
  const state = useCurrentUser();

  const signOut = () => {
    tokenStorage.clear();
    router.replace("/login");
  };

  return (
    <div className="flex flex-1 flex-col">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex h-16 max-w-5xl items-center justify-between px-4">
          <Logo />
          {state.status === "authenticated" && (
            <div className="flex items-center gap-4">
              <span className="hidden text-sm text-slate-500 sm:inline">{state.user.email}</span>
              <Button variant="secondary" onClick={signOut}>
                Sign out
              </Button>
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-12">
        {state.status === "loading" && (
          <div className="flex items-center gap-3 text-slate-500" role="status">
            <span className="size-5 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" />
            Loading your workspace…
          </div>
        )}

        {state.status === "error" && (
          <div className="max-w-md space-y-4">
            <ErrorAlert message={state.message} />
            <Button variant="secondary" onClick={() => window.location.reload()}>
              Try again
            </Button>
          </div>
        )}

        {state.status === "authenticated" && (
          <div className="rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
            <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
              Welcome, {state.user.full_name}
            </h1>
            <p className="mt-2 text-slate-500">
              You&apos;re signed in as <span className="font-medium text-slate-700">{state.user.email}</span>.
              Your workflows will live here.
            </p>
          </div>
        )}
      </main>
    </div>
  );
}
