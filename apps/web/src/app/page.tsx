import Link from "next/link";

import { HomeActions } from "@/components/home-actions";
import { Logo } from "@/components/logo";

export default function HomePage() {
  return (
    <div className="flex flex-1 flex-col">
      <header className="mx-auto flex h-16 w-full max-w-5xl items-center justify-between px-4">
        <Logo />
        <Link href="/login" className="text-sm font-medium text-slate-600 hover:text-slate-900">
          Sign in
        </Link>
      </header>

      <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col items-center justify-center px-4 py-16 text-center">
        <span className="rounded-full bg-indigo-50 px-3 py-1 text-xs font-medium text-indigo-700 ring-1 ring-inset ring-indigo-200">
          Early preview
        </span>
        <h1 className="mt-6 text-4xl font-semibold tracking-tight text-slate-900 sm:text-5xl">
          Build AI workflows visually
        </h1>
        <p className="mt-4 max-w-xl text-lg text-slate-600">
          Connect models, data, and apps on a canvas, then run and monitor every step.
        </p>
        <HomeActions />
      </main>
    </div>
  );
}
