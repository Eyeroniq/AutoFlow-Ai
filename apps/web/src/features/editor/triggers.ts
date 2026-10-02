/**
 * Pure helpers for the Triggers panel: presets, status wording, time formatting in a
 * schedule's own time zone, and the webhook example. Unit tested.
 */
import type { ExecutionTrigger, Trigger, TriggerSettings, TriggerType } from "@/lib/types";

export const SCHEDULE_PRESETS: { label: string; cron: string }[] = [
  { label: "Every day 07:30", cron: "30 7 * * *" },
  { label: "Weekdays 09:00", cron: "0 9 * * 1-5" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every 15 min", cron: "*/15 * * * *" },
  { label: "Every minute", cron: "* * * * *" },
];

export const TRIGGER_LABEL: Record<TriggerType, string> = {
  schedule: "Schedule",
  email: "New email",
  webhook: "Webhook",
  telegram: "Telegram message",
};

/** How an execution's trigger reads in lists ("api" was the webhook's old name). */
export const EXECUTION_TRIGGER_LABEL: Record<ExecutionTrigger, string> = {
  manual: "manual",
  schedule: "schedule",
  email: "email",
  webhook: "webhook",
  telegram: "telegram",
  discord_voice: "discord voice",
  event: "event",
  api: "webhook",
};

export type TriggerTone = "off" | "on" | "warning" | "disabled";

/** The one-line state of a trigger for its card header and the top bar dot. */
export function triggerState(trigger: Trigger, settings: TriggerSettings): { tone: TriggerTone; label: string } {
  if (!trigger.enabled && trigger.auto_disabled_at) {
    return { tone: "disabled", label: `Off after ${trigger.consecutive_failures} failed runs in a row` };
  }
  if (!trigger.enabled) return { tone: "off", label: trigger.configured ? "Off" : "Not set up" };
  const limit = settings.max_consecutive_failures;
  if (trigger.consecutive_failures > 0) {
    const left = limit ? ` (switches off at ${limit})` : "";
    return { tone: "warning", label: `On · ${trigger.consecutive_failures} failed in a row${left}` };
  }
  return { tone: "on", label: "On" };
}

/** The dot on the top bar's Triggers button: any trigger switched off by failures wins. */
export function overallTone(triggers: Trigger[], settings: TriggerSettings): TriggerTone {
  const tones = triggers.map((t) => triggerState(t, settings).tone);
  if (tones.includes("disabled")) return "disabled";
  if (tones.includes("warning")) return "warning";
  return tones.includes("on") ? "on" : "off";
}

/** An instant in a given IANA zone, e.g. "Tue 29 Sep, 07:30 (Asia/Kolkata)". */
export function formatInZone(iso: string, timeZone: string, locale?: string): string {
  let text: string;
  try {
    text = new Date(iso).toLocaleString(locale, {
      timeZone,
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    });
  } catch {
    return new Date(iso).toISOString();
  }
  return `${text} (${timeZone})`;
}

/** The browser's zone first, then the rest of the IANA list the browser knows. */
export function timeZones(): string[] {
  const local = browserTimeZone();
  const all = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
  return [...new Set([local, "UTC", ...all])];
}

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** A curl call for the deployment endpoint; the key is shown only by its prefix. */
export function webhookExample(apiUrl: string, endpoint: string, keyPrefix: string): string {
  return [
    `curl -X POST "${apiUrl}${endpoint}" \\`,
    `  -H "Authorization: Bearer ${keyPrefix}…" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -d '{"inputs": {}}'`,
  ].join("\n");
}

/** Only the fields the email form edits, with blanks as null (the API's "no filter"). */
export function emailConfigPayload(values: Record<string, unknown>): Record<string, unknown> {
  const text = (key: string) => {
    const value = values[key];
    return typeof value === "string" && value.trim() ? value.trim() : null;
  };
  const number = (key: string, fallback: number) => {
    const value = Number(values[key]);
    return Number.isFinite(value) && value > 0 ? Math.round(value) : fallback;
  };
  return {
    folder: text("folder") ?? "INBOX",
    from_address: text("from_address"),
    subject: text("subject"),
    unread_only: values.unread_only !== false,
    poll_minutes: number("poll_minutes", 5),
    input_name: text("input_name") ?? "email",
    max_per_poll: number("max_per_poll", 10),
    mark_as_read: values.mark_as_read === true,
    max_body_chars: number("max_body_chars", 5000),
  };
}
