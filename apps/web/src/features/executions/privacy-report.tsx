"use client";

import { useQuery } from "@tanstack/react-query";
import { ShieldCheck, ShieldAlert } from "lucide-react";

import { api } from "@/lib/api";
import type { PrivacyReport as Report } from "@/lib/types";

const LABELS: Record<string, string> = {
  CREDIT_CARD: "card number", AADHAAR: "Aadhaar", PAN: "PAN", EMAIL: "email", IN_PHONE: "Indian phone", PHONE: "phone",
  PERSON: "name", LOCATION: "location", PASSWORD: "password", JWT: "JWT", PRIVATE_KEY: "private key",
  CONNECTION_STRING: "database password", HIGH_ENTROPY_SECRET: "secret-looking token", AWS_ACCESS_KEY: "AWS key",
  GOOGLE_API_KEY: "Google API key", GITHUB_TOKEN: "GitHub token", STRIPE_KEY: "Stripe key", SLACK_TOKEN: "Slack token",
  OPENAI_KEY: "OpenAI key", ANTHROPIC_KEY: "Anthropic key", GROQ_KEY: "Groq key", TELEGRAM_BOT_TOKEN: "Telegram token",
  IP_ADDRESS: "IP address", IBAN: "IBAN",
};
const CATEGORY: Record<string, string> = {
  secret: "secrets", financial: "financial", government_id: "government IDs", personal: "personal data",
};
const ACTION: Record<string, string> = {
  blocked: "bg-red-50 text-red-700 ring-red-200",
  redacted: "bg-amber-50 text-amber-800 ring-amber-200",
  warned: "bg-sky-50 text-sky-700 ring-sky-200",
};

const label = (type: string) => LABELS[type] ?? type.toLowerCase().replaceAll("_", " ");
const counts = (map: Record<string, number>, name: (k: string) => string) =>
  Object.entries(map)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} ${name(k)}`)
    .join(", ");

/** The run's Privacy Report: counts and types only (the API never sends values). */
export function PrivacyReport({ executionId, status }: { executionId: string; status: string }) {
  const detail = useQuery({
    queryKey: ["execution", executionId, "privacy", status],
    queryFn: () => api.executions.get(executionId),
    meta: { silent: true },
  });
  const report: Report | null | undefined = detail.data?.privacy_report;
  if (!report) return null;
  const clean = report.total === 0 && Object.keys(report.guard_actions).length === 0;
  return (
    <section className="rounded-xl border border-slate-200 bg-white" data-testid="privacy-report">
      <div className="flex items-center gap-2 border-b border-slate-100 px-4 py-2.5">
        {clean ? <ShieldCheck className="size-4 text-emerald-600" aria-hidden /> : <ShieldAlert className="size-4 text-amber-600" aria-hidden />}
        <h2 className="text-sm font-semibold text-slate-900">Privacy report</h2>
        {report.masked && <span className="ml-auto text-[11px] text-slate-500">stored data masked</span>}
      </div>
      <div className="space-y-3 px-4 py-3 text-sm">
        {clean ? (
          <p className="text-slate-500" data-testid="privacy-summary">No secrets, card numbers, or IDs found in this run.</p>
        ) : (
          <>
            <p className="text-slate-800" data-testid="privacy-summary">
              {report.total} found{report.total ? `: ${counts(report.by_category, (c) => CATEGORY[c] ?? c)}` : ""}
            </p>
            {report.total > 0 && <p className="text-xs text-slate-500">{counts(report.by_type, label)}</p>}
            <ul className="space-y-1.5">
              {report.nodes.map((node) => (
                <li key={node.node_key} className="flex flex-wrap items-center gap-2 text-xs" data-testid="privacy-node" data-node={node.node_key}>
                  <span className="font-medium text-slate-700">{node.label}</span>
                  {node.total > 0 && <span className="text-slate-500">{counts(node.by_type, label)}</span>}
                  {node.guard && ACTION[node.guard.action] && (
                    <span className={`rounded-full px-2 py-0.5 font-medium ring-1 ring-inset ${ACTION[node.guard.action]}`}>
                      guard {node.guard.action}
                    </span>
                  )}
                </li>
              ))}
            </ul>
            <p className="text-[11px] text-slate-400">Counts and types only; the values are never shown or stored here.</p>
          </>
        )}
      </div>
    </section>
  );
}
