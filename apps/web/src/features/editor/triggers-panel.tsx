"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, CircleAlert, Copy, ExternalLink, Mail, RefreshCw, Rocket, ShieldAlert, Webhook, X } from "lucide-react";
import Link from "next/link";
import { type ReactNode, useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import { API_URL } from "@/lib/config";
import { formatDateTime, formatRelative, formatUntil } from "@/lib/format";
import type { SchedulePreview, Trigger, TriggerSettings, TriggersResponse, TriggerType } from "@/lib/types";

import { useEditor } from "./store";
import {
  browserTimeZone,
  emailConfigPayload,
  formatInZone,
  SCHEDULE_PRESETS,
  timeZones,
  TRIGGER_LABEL,
  type TriggerTone,
  triggerState,
  webhookExample,
} from "./triggers";
import { useEditorUi } from "./ui-store";

const inputClass =
  "nodrag block w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100";

const TONE: Record<TriggerTone, string> = {
  off: "bg-slate-100 text-slate-600",
  on: "bg-emerald-50 text-emerald-700",
  warning: "bg-amber-50 text-amber-800",
  disabled: "bg-red-50 text-red-700",
};

export const triggersKey = (workflowId: string | null) => ["triggers", workflowId] as const;

/** The workflow's triggers, kept fresh while something may be firing. */
export function useTriggers() {
  const workflowId = useEditor((s) => s.workflowId);
  return useQuery({
    queryKey: triggersKey(workflowId),
    queryFn: () => api.workflows.triggers(workflowId!),
    enabled: Boolean(workflowId),
    refetchInterval: (query) => (query.state.data?.triggers.some((t) => t.enabled) ? 15_000 : false),
    meta: { silent: true },
  });
}

function Switch({ checked, onChange, label, busy }: { checked: boolean; onChange: (next: boolean) => void; label: string; busy?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={busy}
      onClick={() => onChange(!checked)}
      data-testid={`switch-${label.toLowerCase().replace(/\s+/g, "-")}`}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors disabled:opacity-50 ${checked ? "bg-emerald-500" : "bg-slate-300"}`}
    >
      <span className={`inline-block size-4 rounded-full bg-white shadow transition-transform ${checked ? "translate-x-4" : "translate-x-0.5"}`} />
    </button>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: (id: string) => ReactNode }) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="mb-1 block text-xs font-medium text-slate-700">
        {label}
      </label>
      {children(id)}
      {hint && <p className="mt-1 text-[11px] leading-4 text-slate-500">{hint}</p>}
    </div>
  );
}

/** Status lines shared by every trigger: auto-disable, last problem, next and last run. */
function Activity({ trigger, settings, nextLabel, timeZone }: { trigger: Trigger; settings: TriggerSettings; nextLabel: string; timeZone?: string }) {
  return (
    <div className="space-y-1.5 text-[11px] text-slate-600">
      {trigger.auto_disabled_at && trigger.disabled_reason && (
        <p className="flex gap-1.5 rounded-md bg-red-50 p-2 text-red-700" role="alert" data-testid={`disabled-reason-${trigger.type}`}>
          <ShieldAlert className="mt-px size-3.5 shrink-0" aria-hidden /> <span className="break-words">{trigger.disabled_reason}</span>
        </p>
      )}
      {trigger.last_error && !trigger.auto_disabled_at && (
        <p className="flex gap-1.5 rounded-md bg-amber-50 p-2 text-amber-900" title={formatDateTime(trigger.last_error_at)}>
          <CircleAlert className="mt-px size-3.5 shrink-0" aria-hidden /> <span className="break-words">{trigger.last_error}</span>
        </p>
      )}
      {trigger.enabled && trigger.next_run_at && (
        <p data-testid={`next-run-${trigger.type}`}>
          <span className="text-slate-400">{nextLabel}:</span>{" "}
          {timeZone ? formatInZone(trigger.next_run_at, timeZone) : formatDateTime(trigger.next_run_at)}{" "}
          <span className="text-slate-400">({formatUntil(trigger.next_run_at)})</span>
        </p>
      )}
      {trigger.last_run ? (
        <p className="flex items-center gap-1.5" data-testid={`last-run-${trigger.type}`}>
          <span className="text-slate-400">Last run:</span>
          <StatusBadge status={trigger.last_run.status} />
          <Link href={`/executions/${trigger.last_run.execution_id}`} className="text-indigo-600 hover:underline">
            {formatRelative(trigger.last_run.created_at)}
          </Link>
        </p>
      ) : (
        trigger.configured && <p className="text-slate-400">No runs yet.</p>
      )}
      {trigger.enabled && settings.max_consecutive_failures > 0 && trigger.consecutive_failures > 0 && (
        <p className="text-amber-800">
          {trigger.consecutive_failures} failed in a row; it switches off at {settings.max_consecutive_failures}.
        </p>
      )}
    </div>
  );
}

function Card({
  icon,
  trigger,
  settings,
  onToggle,
  busy,
  children,
}: {
  icon: ReactNode;
  trigger: Trigger;
  settings: TriggerSettings;
  onToggle: (enabled: boolean) => void;
  busy: boolean;
  children: ReactNode;
}) {
  const state = triggerState(trigger, settings);
  return (
    <section className="space-y-3 border-b border-slate-100 px-4 py-4" data-testid={`trigger-${trigger.type}`} data-enabled={trigger.enabled}>
      <div className="flex items-center gap-2">
        <span className="grid size-7 place-items-center rounded-md bg-indigo-50 text-indigo-600">{icon}</span>
        <h3 className="flex-1 text-sm font-semibold text-slate-900">{TRIGGER_LABEL[trigger.type]}</h3>
        <span className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${TONE[state.tone]}`} data-testid={`trigger-state-${trigger.type}`}>
          {state.label}
        </span>
        <Switch checked={trigger.enabled} onChange={onToggle} label={`${TRIGGER_LABEL[trigger.type]} trigger`} busy={busy} />
      </div>
      {children}
    </section>
  );
}

// --- schedule ---------------------------------------------------------------------------------

function useSchedulePreview(cron: string, timeZone: string) {
  const [preview, setPreview] = useState<SchedulePreview | null>(null);
  useEffect(() => {
    if (!cron.trim()) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      api.triggers
        .previewSchedule({ cron, timezone: timeZone, count: 3 })
        .then((result) => !cancelled && setPreview(result))
        .catch(() => undefined);
    }, 350);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [cron, timeZone]);
  return cron.trim() ? preview : null;
}

function ScheduleCard({ trigger, settings, save, busy }: CardProps) {
  const [cron, setCron] = useState(String(trigger.config.cron ?? "0 8 * * *"));
  const [timeZone, setTimeZone] = useState(String(trigger.config.timezone ?? browserTimeZone()));
  const preview = useSchedulePreview(cron, timeZone);
  const zones = timeZones();
  const listId = useId();
  const dirty = cron !== trigger.config.cron || timeZone !== trigger.config.timezone;
  const config = { cron, timezone: timeZone, inputs: trigger.config.inputs ?? {} };

  return (
    <Card icon={<CalendarClock className="size-4" />} trigger={trigger} settings={settings} busy={busy} onToggle={(enabled) => save("schedule", enabled, config)}>
      <div className="flex flex-wrap gap-1">
        {SCHEDULE_PRESETS.map((preset) => (
          <button
            key={preset.cron}
            type="button"
            onClick={() => setCron(preset.cron)}
            className={`rounded-full px-2 py-0.5 text-[11px] ${cron === preset.cron ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}
          >
            {preset.label}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-[1fr_1fr] gap-2">
        <Field label="Cron" hint="minute hour day month weekday">
          {(id) => <input id={id} value={cron} onChange={(e) => setCron(e.target.value)} className={`${inputClass} font-mono`} data-testid="schedule-cron" />}
        </Field>
        <Field label="Time zone">
          {(id) => (
            <>
              <input id={id} list={listId} value={timeZone} onChange={(e) => setTimeZone(e.target.value)} className={inputClass} data-testid="schedule-timezone" />
              <datalist id={listId}>
                {zones.map((zone) => (
                  <option key={zone} value={zone} />
                ))}
              </datalist>
            </>
          )}
        </Field>
      </div>
      {preview && (
        <div className="text-[11px]" data-testid="schedule-preview">
          {preview.valid ? (
            <>
              <p className="text-slate-500">Next runs{preview.interval ? " (every interval, through DST changes)" : ""}:</p>
              <ul className="mt-0.5 space-y-0.5 font-mono text-slate-700">
                {preview.next.map((time) => (
                  <li key={time}>{formatInZone(time, timeZone)}</li>
                ))}
              </ul>
            </>
          ) : (
            <p className="text-red-600">{preview.error}</p>
          )}
        </div>
      )}
      {dirty && (
        <Button size="sm" onClick={() => save("schedule", trigger.enabled || !trigger.configured, config)} loading={busy} disabled={preview?.valid === false} data-testid="save-schedule">
          Save schedule{!trigger.configured ? " and turn it on" : ""}
        </Button>
      )}
      <Activity trigger={trigger} settings={settings} nextLabel="Next run" timeZone={timeZone} />
    </Card>
  );
}

// --- email ------------------------------------------------------------------------------------

function EmailCard({ trigger, settings, save, busy }: CardProps) {
  const workflowId = useEditor((s) => s.workflowId);
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>(trigger.config);
  const [open, setOpen] = useState(trigger.configured);
  const set = (key: string, value: unknown) => setValues((v) => ({ ...v, [key]: value }));
  const payload = emailConfigPayload(values);
  const dirty = JSON.stringify(payload) !== JSON.stringify(emailConfigPayload(trigger.config));
  const check = useMutation({
    meta: { silent: true },
    mutationFn: () => api.workflows.checkEmail(workflowId!),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: triggersKey(workflowId) });
      const started = result.runs.filter((r) => r.outcome === "started").length;
      if (result.error) toast.error("Couldn't check the mailbox", result.error);
      else toast.info(`Checked ${String(payload.folder)}`, `${result.found} new matching email${result.found === 1 ? "" : "s"}; started ${started} run${started === 1 ? "" : "s"}.`);
    },
    onError: (error) => toast.error("Couldn't check the mailbox", error.message),
  });
  return (
    <Card icon={<Mail className="size-4" />} trigger={trigger} settings={settings} busy={busy} onToggle={(enabled) => save("email", enabled, payload)}>
      <p className="text-[11px] text-slate-500">
        Checks your Gmail (Integrations) for unread mail that matches, and starts one run per new email with it as the input. Enabling it
        starts from now: mail already in the folder never triggers.
      </p>
      {open ? (
        <EmailForm values={values} set={set} warnings={trigger.warnings} />
      ) : (
        <Button size="sm" variant="secondary" onClick={() => setOpen(true)} data-testid="setup-email">
          Set up
        </Button>
      )}
      {open && (
        <div className="flex flex-wrap gap-2">
          {(dirty || !trigger.configured) && (
            <Button size="sm" onClick={() => save("email", trigger.enabled || !trigger.configured, payload)} loading={busy} data-testid="save-email">
              {trigger.configured ? "Save" : "Save and turn it on"}
            </Button>
          )}
          {trigger.enabled && (
            <Button size="sm" variant="secondary" onClick={() => check.mutate()} loading={check.isPending} data-testid="check-email">
              <RefreshCw className="size-3.5" aria-hidden /> Check now
            </Button>
          )}
        </div>
      )}
      {trigger.mailbox?.last_poll_at && <p className="text-[11px] text-slate-500">Last checked {formatRelative(trigger.mailbox.last_poll_at)}.</p>}
      <Activity trigger={trigger} settings={settings} nextLabel="Next check" />
    </Card>
  );
}

function EmailForm({ values, set, warnings }: { values: Record<string, unknown>; set: (key: string, value: unknown) => void; warnings: string[] }) {
  const text = (key: string) => (values[key] === null || values[key] === undefined ? "" : String(values[key]));
  return (
    <>
      <div className="grid grid-cols-2 gap-2">
        <Field label="From contains">{(id) => <input id={id} value={text("from_address")} onChange={(e) => set("from_address", e.target.value)} placeholder="anyone" className={inputClass} />}</Field>
        <Field label="Subject contains">
          {(id) => <input id={id} value={text("subject")} onChange={(e) => set("subject", e.target.value)} placeholder="anything" className={inputClass} data-testid="email-subject" />}
        </Field>
        <Field label="Folder">{(id) => <input id={id} value={text("folder")} onChange={(e) => set("folder", e.target.value)} className={inputClass} />}</Field>
        <Field label="Check every (minutes)">
          {(id) => <input id={id} type="number" min={1} value={text("poll_minutes")} onChange={(e) => set("poll_minutes", e.target.value)} className={inputClass} />}
        </Field>
        <Field label="Input name" hint="The Input node (type JSON) that receives the email.">
          {(id) => <input id={id} value={text("input_name")} onChange={(e) => set("input_name", e.target.value)} className={`${inputClass} font-mono`} />}
        </Field>
        <Field label="Max emails per check">
          {(id) => <input id={id} type="number" min={1} max={50} value={text("max_per_poll")} onChange={(e) => set("max_per_poll", e.target.value)} className={inputClass} />}
        </Field>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-700">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={values.unread_only !== false} onChange={(e) => set("unread_only", e.target.checked)} className="size-4 rounded border-slate-300" />
          Unread only
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={values.mark_as_read === true} onChange={(e) => set("mark_as_read", e.target.checked)} className="size-4 rounded border-slate-300" />
          Mark as read
        </label>
      </div>
      <p className="text-[11px] text-slate-500">Gmail marks mail you send to yourself as read: to test with one, turn off &ldquo;Unread only&rdquo;.</p>
      {warnings.map((warning) => (
        <p key={warning} className="flex gap-1.5 rounded-md bg-amber-50 p-2 text-[11px] text-amber-900">
          <CircleAlert className="mt-px size-3.5 shrink-0" aria-hidden /> {warning}
        </p>
      ))}
    </>
  );
}

// --- webhook ----------------------------------------------------------------------------------

function WebhookCard({ trigger, settings, save, busy }: CardProps) {
  const setUi = useEditorUi((s) => s.set);
  const hook = trigger.webhook;
  const copy = (text: string, what: string) => {
    void navigator.clipboard?.writeText(text);
    toast.info("Copied", what);
  };
  return (
    <Card icon={<Webhook className="size-4" />} trigger={trigger} settings={settings} busy={busy} onToggle={(enabled) => save("webhook", enabled, {})}>
      {hook ? (
        <div className="space-y-2">
          <p className="text-[11px] text-slate-500">Other services start a run by POSTing to the deployment endpoint with its API key.</p>
          <div className="flex items-center gap-1 rounded-md bg-slate-50 p-2">
            <code className="min-w-0 flex-1 truncate text-[11px] text-slate-700" title={`${API_URL}${hook.endpoint}`}>
              {`${API_URL}${hook.endpoint}`}
            </code>
            <button type="button" onClick={() => copy(`${API_URL}${hook.endpoint}`, "Webhook URL")} className="rounded p-1 text-slate-400 hover:text-indigo-600" aria-label="Copy the webhook URL">
              <Copy className="size-3.5" />
            </button>
          </div>
          <pre className="overflow-x-auto rounded-md bg-slate-900 p-2 text-[10px] leading-4 text-slate-100">{webhookExample(API_URL, hook.endpoint, hook.api_key_prefix)}</pre>
          {trigger.warnings.map((warning) => (
            <p key={warning} className="text-[11px] text-amber-800">{warning}</p>
          ))}
          <Button size="sm" variant="secondary" onClick={() => setUi({ deployOpen: true })}>
            <Rocket className="size-3.5" aria-hidden /> {hook.behind ? "Redeploy" : "Deployment and key"}
          </Button>
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-[11px] text-slate-500">
            Deploy the pipeline to get a webhook URL and API key. Calls run the deployed snapshot and show up with trigger &ldquo;webhook&rdquo;.
          </p>
          <Button size="sm" onClick={() => setUi({ deployOpen: true })} data-testid="webhook-deploy">
            <Rocket className="size-3.5" aria-hidden /> Deploy
          </Button>
        </div>
      )}
      <Activity trigger={trigger} settings={settings} nextLabel="Next" />
    </Card>
  );
}

// --- limits and the panel ---------------------------------------------------------------------

function Limits({ data, onSave, busy }: { data: TriggersResponse; onSave: (settings: TriggerSettings) => void; busy: boolean }) {
  const [perHour, setPerHour] = useState(String(data.settings.max_runs_per_hour));
  const [failures, setFailures] = useState(String(data.settings.max_consecutive_failures));
  const next = { max_runs_per_hour: Number(perHour), max_consecutive_failures: Number(failures) };
  const valid = Number.isInteger(next.max_runs_per_hour) && next.max_runs_per_hour >= 1 && Number.isInteger(next.max_consecutive_failures) && next.max_consecutive_failures >= 0;
  const dirty = next.max_runs_per_hour !== data.settings.max_runs_per_hour || next.max_consecutive_failures !== data.settings.max_consecutive_failures;
  return (
    <section className="space-y-3 px-4 py-4" data-testid="trigger-limits">
      <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Safety limits</h3>
      <div className="grid grid-cols-2 gap-2">
        <Field label="Triggered runs per hour" hint={`${data.runs_last_hour} in the last hour. More are skipped.`}>
          {(id) => <input id={id} type="number" min={1} max={1000} value={perHour} onChange={(e) => setPerHour(e.target.value)} className={inputClass} />}
        </Field>
        <Field label="Switch off after N failures" hint="In a row, per trigger. 0 = never.">
          {(id) => <input id={id} type="number" min={0} max={100} value={failures} onChange={(e) => setFailures(e.target.value)} className={inputClass} />}
        </Field>
      </div>
      {dirty && (
        <Button size="sm" onClick={() => onSave(next)} disabled={!valid} loading={busy}>
          Save limits
        </Button>
      )}
    </section>
  );
}

interface CardProps {
  trigger: Trigger;
  settings: TriggerSettings;
  save: (type: TriggerType, enabled: boolean, config?: Record<string, unknown>) => void;
  busy: boolean;
}

export function TriggersPanel() {
  const workflowId = useEditor((s) => s.workflowId);
  const saveState = useEditor((s) => s.save.status);
  const queryClient = useQueryClient();
  const triggers = useTriggers();
  const close = () => useEditorUi.getState().set({ rightPanel: null });
  const store = (data: TriggersResponse) => queryClient.setQueryData(triggersKey(workflowId), data);

  const saveTrigger = useMutation({
    meta: { silent: true },
    mutationFn: ({ type, enabled, config }: { type: TriggerType; enabled: boolean; config?: Record<string, unknown> }) =>
      api.workflows.saveTrigger(workflowId!, type, { enabled, config }),
    onSuccess: (data, variables) => {
      store(data);
      const saved = data.triggers.find((t) => t.type === variables.type);
      toast.success(`${TRIGGER_LABEL[variables.type]} trigger ${saved?.enabled ? "on" : "off"}`);
    },
    onError: (error, variables) => toast.error(`Couldn't save the ${TRIGGER_LABEL[variables.type].toLowerCase()} trigger`, error.message),
  });
  const saveLimits = useMutation({
    meta: { silent: true },
    mutationFn: (settings: TriggerSettings) => api.workflows.saveTriggerSettings(workflowId!, settings),
    onSuccess: (data) => {
      store(data);
      toast.success("Limits saved");
    },
    onError: (error) => toast.error("Couldn't save the limits", error.message),
  });

  const save: CardProps["save"] = (type, enabled, config) => saveTrigger.mutate({ type, enabled, config });
  const busy = saveTrigger.isPending;
  const data = triggers.data;

  return (
    <aside className="flex w-[26rem] shrink-0 flex-col border-l border-slate-200 bg-white" aria-label="Triggers" data-testid="triggers-panel">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-900">Triggers</h2>
        <button type="button" onClick={close} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Close panel">
          <X className="size-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto">
        <p className="border-b border-slate-100 px-4 py-2 text-[11px] text-slate-500">
          Triggers run the saved pipeline{saveState === "dirty" || saveState === "saving" ? " (your latest edits are being saved)" : ""}. Runs show their
          trigger in{" "}
          <Link href={`/executions?workflow_id=${workflowId ?? ""}`} className="inline-flex items-center gap-0.5 font-medium text-indigo-600">
            Executions <ExternalLink className="size-3" />
          </Link>
          .
        </p>
        {triggers.isPending ? (
          <p className="p-4 text-sm text-slate-400">Loading triggers…</p>
        ) : triggers.isError || !data ? (
          <div className="space-y-2 p-4">
            <p className="text-sm text-red-600">{triggers.error?.message ?? "Couldn't load the triggers."}</p>
            <Button size="sm" variant="secondary" onClick={() => void triggers.refetch()}>
              Try again
            </Button>
          </div>
        ) : (
          <>
            {data.triggers.map((trigger) => {
              const props = { trigger, settings: data.settings, save, busy };
              // Remount a card when its saved config changes, so its form shows what was saved.
              const key = `${trigger.type}:${JSON.stringify(trigger.config)}`;
              if (trigger.type === "schedule") return <ScheduleCard key={key} {...props} />;
              if (trigger.type === "email") return <EmailCard key={key} {...props} />;
              return <WebhookCard key={key} {...props} />;
            })}
            <Limits key={JSON.stringify(data.settings)} data={data} onSave={(settings) => saveLimits.mutate(settings)} busy={saveLimits.isPending} />
          </>
        )}
      </div>
    </aside>
  );
}
