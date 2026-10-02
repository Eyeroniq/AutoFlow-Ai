import { CalendarClock, Hand, Mail, Mic, type LucideIcon, Send, Webhook, Zap } from "lucide-react";

import type { ExecutionTrigger } from "@/lib/types";

const TRIGGERS: Record<ExecutionTrigger, { icon: LucideIcon; label: string; tone: string }> = {
  manual: { icon: Hand, label: "manual", tone: "bg-slate-100 text-slate-600" },
  schedule: { icon: CalendarClock, label: "schedule", tone: "bg-violet-50 text-violet-700" },
  email: { icon: Mail, label: "email", tone: "bg-sky-50 text-sky-700" },
  webhook: { icon: Webhook, label: "webhook", tone: "bg-amber-50 text-amber-800" },
  telegram: { icon: Send, label: "telegram", tone: "bg-cyan-50 text-cyan-700" },
  discord_voice: { icon: Mic, label: "discord voice", tone: "bg-indigo-50 text-indigo-700" },
  // Deployment runs from before triggers existed.
  api: { icon: Webhook, label: "webhook", tone: "bg-amber-50 text-amber-800" },
  event: { icon: Zap, label: "event", tone: "bg-slate-100 text-slate-600" },
};

/** What started a run: manual, schedule, email, or webhook. */
export function TriggerBadge({ trigger }: { trigger: ExecutionTrigger }) {
  const { icon: Icon, label, tone } = TRIGGERS[trigger] ?? TRIGGERS.event;
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] font-medium ${tone}`} data-testid="trigger-badge" data-trigger={label}>
      <Icon className="size-3" aria-hidden />
      {label}
    </span>
  );
}
