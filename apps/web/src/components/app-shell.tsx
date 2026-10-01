"use client";

import { LogOut, UserRound } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, type ReactNode, useContext } from "react";

import { Logo } from "@/components/logo";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Menu } from "@/components/ui/menu";
import { useCurrentUser } from "@/hooks/use-auth";
import { tokenStorage } from "@/lib/token-storage";
import type { User } from "@/lib/types";

const UserContext = createContext<User | null>(null);

export function useUser(): User {
  const user = useContext(UserContext);
  if (!user) throw new Error("useUser must be used inside <RequireAuth>");
  return user;
}

function FullPageStatus({ children }: { children: ReactNode }) {
  return <div className="flex flex-1 items-center justify-center p-8">{children}</div>;
}

export function Spinner({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 text-sm text-slate-500" role="status">
      <span className="size-5 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" />
      {label}
    </div>
  );
}

/** Renders children once the session is confirmed; redirects to /login otherwise. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const state = useCurrentUser();
  if (state.status === "loading") {
    return (
      <FullPageStatus>
        <Spinner label="Loading your workspace…" />
      </FullPageStatus>
    );
  }
  if (state.status === "error") {
    return (
      <FullPageStatus>
        <div className="max-w-md space-y-4">
          <ErrorAlert message={state.message} />
          <Button variant="secondary" onClick={() => window.location.reload()}>
            Try again
          </Button>
        </div>
      </FullPageStatus>
    );
  }
  return <UserContext.Provider value={state.user}>{children}</UserContext.Provider>;
}

export function UserMenu({ tone = "light" }: { tone?: "light" | "dark" }) {
  const user = useUser();
  const router = useRouter();
  const signOut = () => {
    tokenStorage.clear();
    router.replace("/login");
  };
  return (
    <Menu
      label="Account menu"
      trigger={
        <span
          className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-sm ${
            tone === "dark" ? "text-indigo-50 hover:bg-indigo-600" : "text-slate-600 hover:bg-slate-100"
          }`}
        >
          <UserRound className="size-4" aria-hidden />
          <span className="hidden max-w-40 truncate sm:inline">{user.email}</span>
        </span>
      }
      items={[
        { label: "Dashboard", onSelect: () => router.push("/dashboard") },
        { label: "Executions", onSelect: () => router.push("/executions") },
        { label: "Knowledge", onSelect: () => router.push("/knowledge") },
        { label: "Integrations", onSelect: () => router.push("/integrations") },
        { label: "Sign out", onSelect: signOut, icon: <LogOut className="size-4" />, danger: true },
      ]}
    />
  );
}

const NAV = [
  { href: "/dashboard", label: "Dashboard" },
  { href: "/executions", label: "Executions" },
  { href: "/knowledge", label: "Knowledge" },
  { href: "/integrations", label: "Integrations" },
];

function Header() {
  const pathname = usePathname();
  return (
    <header className="border-b border-slate-200 bg-white">
      <div className="mx-auto flex h-14 max-w-6xl items-center gap-6 px-4">
        <Logo />
        <nav className="flex items-center gap-1">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                  active ? "bg-indigo-50 text-indigo-700" : "text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>
        <div className="ml-auto">
          <UserMenu />
        </div>
      </div>
    </header>
  );
}

/** Page chrome for the non-editor screens. */
export function AppShell({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <div className="flex flex-1 flex-col">
        <Header />
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-8">{children}</main>
      </div>
    </RequireAuth>
  );
}
