"use client";

import Link from "next/link";

import { useHasSession } from "@/hooks/use-auth";

const primary =
  "rounded-lg bg-indigo-600 px-5 py-3 text-sm font-semibold text-white shadow-sm hover:bg-indigo-500";
const secondary =
  "rounded-lg bg-white px-5 py-3 text-sm font-semibold text-slate-700 shadow-sm ring-1 ring-inset ring-slate-300 hover:bg-slate-50";

export function HomeActions() {
  const signedIn = useHasSession();

  return (
    <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
      {signedIn ? (
        <Link href="/dashboard" className={primary}>
          Go to dashboard
        </Link>
      ) : (
        <>
          <Link href="/register" className={primary}>
            Create an account
          </Link>
          <Link href="/login" className={secondary}>
            Sign in
          </Link>
        </>
      )}
    </div>
  );
}
