import Link from "next/link";

export function Logo() {
  return (
    <Link href="/" className="inline-flex items-center gap-2 text-slate-900">
      <span className="grid size-8 place-items-center rounded-lg bg-indigo-600 text-sm font-bold text-white">
        F
      </span>
      <span className="text-lg font-semibold tracking-tight">FlowForge AI</span>
    </Link>
  );
}
