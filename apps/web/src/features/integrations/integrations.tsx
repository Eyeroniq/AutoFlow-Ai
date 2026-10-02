"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Blocks, Bot, CircleAlert, CircleCheck, ExternalLink, KeyRound, Mail, MessageSquare, PlugZap, Search, Send, Unplug } from "lucide-react";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { AppShell, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { FormField } from "@/components/ui/form-field";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { Integration, IntegrationConnect, IntegrationTestResult } from "@/lib/types";

// Providers whose endpoint can be changed (a local Ollama, OpenAI or a proxy, any
// OpenAI-compatible server).
const CUSTOM_ENDPOINT = new Set(["ollama", "openai", "custom"]);
const KEYLESS = new Set(["ollama"]);
// The key is optional (many self-hosted OpenAI-compatible servers take none).
const OPTIONAL_KEY = new Set(["custom"]);

const optional = (schema: z.ZodType) => z.preprocess((v) => (v === "" ? undefined : v), schema.optional());
const port = optional(z.coerce.number().int("Whole number").min(1, "1–65535").max(65535, "1–65535"));

function llmSchema(provider: string) {
  if (provider === "custom") {
    return z.object({
      api_key: optional(z.string().trim()),
      base_url: z.url("Enter the endpoint's base URL, e.g. https://api.together.xyz/v1"),
      model: z.string().trim().min(1, "Enter the model to use").max(200),
    });
  }
  return z.object({
    api_key: KEYLESS.has(provider) ? z.string().optional() : z.string().trim().min(1, "Paste your API key"),
    base_url: optional(z.url("Enter a full URL, e.g. http://localhost:11434")),
    model: optional(z.string().trim().max(200)),
  });
}

const searchSchema = z.object({ api_key: z.string().trim().min(1, "Paste your API key") });

const emailSchema = z.object({
  email: z.email("Enter the Gmail address"),
  app_password: z.string().trim().min(1, "Paste a Google App Password"),
  from_name: optional(z.string().trim().max(200)),
  smtp_host: optional(z.string().trim()),
  smtp_port: port,
  smtp_security: optional(z.enum(["auto", "starttls", "ssl"])),
  imap_host: optional(z.string().trim()),
  imap_port: port,
});

const telegramSchema = z.object({
  bot_token: z.string().trim().regex(/^\d{5,}:[A-Za-z0-9_-]{30,}$/, "Paste the token @BotFather gave you (123456789:AAE...)"),
  chat_id: optional(z.string().trim().max(100)),
});

const discordSchema = z.object({
  webhook_url: z
    .string()
    .trim()
    .regex(/^https:\/\/((ptb|canary)\.)?discord(app)?\.com\/api(\/v\d+)?\/webhooks\/\d+\/[\w-]+\/?$/, "Paste a Discord webhook URL (https://discord.com/api/webhooks/...)"),
});

function schemaFor(integration: Integration) {
  if (integration.kind === "email") return emailSchema;
  if (integration.provider === "telegram") return telegramSchema;
  if (integration.provider === "discord") return discordSchema;
  if (integration.kind === "search" || integration.kind === "workspace") return searchSchema;
  return llmSchema(integration.provider);
}

const ICONS = { llm: Bot, email: Mail, search: Search, workspace: Blocks, telegram: Send, discord: MessageSquare } as const;
const TINTS = { llm: "bg-violet-50 text-violet-600", email: "bg-sky-50 text-sky-600", messaging: "bg-teal-50 text-teal-600", search: "bg-amber-50 text-amber-700", workspace: "bg-slate-100 text-slate-700" } as const;

type FormValues = Record<string, string | undefined>;

/** Write-only credential form: secrets go to the API and are cleared from the page. */
export function ConnectForm({ integration, onDone }: { integration: Integration; onDone: (saved: boolean) => void }) {
  const queryClient = useQueryClient();
  const email = integration.kind === "email";
  const schema = schemaFor(integration);
  const form = useForm<FormValues>({ resolver: zodResolver(schema as z.ZodType<FormValues, FormValues>), defaultValues: {} });
  const connect = useMutation({
    meta: { silent: true },
    mutationFn: (body: IntegrationConnect) => api.integrations.connect(integration.provider, body),
    onSuccess: () => {
      form.reset({});
      void queryClient.invalidateQueries({ queryKey: ["integrations"] });
      toast.success(`${integration.label} connected`, "Your credential is stored encrypted and takes priority over the server's.");
      onDone(true);
    },
  });
  const submit = form.handleSubmit((values) => {
    const body = Object.fromEntries(Object.entries(schema.parse(values)).filter(([, v]) => v !== undefined && v !== "")) as IntegrationConnect;
    connect.mutate(body);
  });
  const errors = form.formState.errors;
  const secret = { type: "password", autoComplete: "new-password", spellCheck: false } as const;

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-3 border-t border-slate-100 pt-4" data-testid={`connect-form-${integration.provider}`}>
      {integration.provider === "telegram" ? (
        <>
          <FormField label="Bot token" {...secret} placeholder="From @BotFather: 123456789:AAE..." registration={form.register("bot_token")} error={errors.bot_token} />
          <FormField
            label="Default chat id (optional)"
            autoComplete="off"
            placeholder="Where Telegram nodes send when they name no chat"
            registration={form.register("chat_id")}
            error={errors.chat_id}
          />
          <p className="text-xs text-slate-500">
            Send your bot a message, then open <code className="rounded bg-slate-100 px-1">https://api.telegram.org/bot&lt;token&gt;/getUpdates</code> and copy{" "}
            <code className="rounded bg-slate-100 px-1">message.chat.id</code>.
          </p>
        </>
      ) : integration.provider === "discord" ? (
        <FormField label="Webhook URL" {...secret} placeholder="https://discord.com/api/webhooks/..." registration={form.register("webhook_url")} error={errors.webhook_url} />
      ) : integration.kind === "search" ? (
        <FormField label="API key" {...secret} placeholder="tvly-..." registration={form.register("api_key")} error={errors.api_key} />
      ) : integration.kind === "workspace" ? (
        <FormField
          label={integration.provider === "notion" ? "Integration token" : "Personal access token"}
          {...secret}
          placeholder={integration.provider === "notion" ? "ntn_..." : "pat..."}
          registration={form.register("api_key")}
          error={errors.api_key}
        />
      ) : email ? (
        <>
          <FormField label="Gmail address" type="email" autoComplete="off" placeholder="you@gmail.com" registration={form.register("email")} error={errors.email} />
          <FormField label="App password" {...secret} placeholder="16 characters from myaccount.google.com/apppasswords" registration={form.register("app_password")} error={errors.app_password} />
          <FormField label="From name (optional)" autoComplete="off" registration={form.register("from_name")} error={errors.from_name} />
          <details className="rounded-lg border border-slate-200 px-3 py-2">
            <summary className="cursor-pointer text-xs font-medium text-slate-600">Advanced: SMTP and IMAP servers</summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <FormField label="SMTP host" placeholder="smtp.gmail.com" registration={form.register("smtp_host")} error={errors.smtp_host} />
              <FormField label="SMTP port" inputMode="numeric" placeholder="587" registration={form.register("smtp_port")} error={errors.smtp_port} />
              <label className="space-y-1.5 text-sm font-medium text-slate-700">
                SMTP security
                <select {...form.register("smtp_security")} className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm">
                  <option value="">auto</option>
                  <option value="starttls">starttls</option>
                  <option value="ssl">ssl</option>
                </select>
              </label>
              <FormField label="IMAP host" placeholder="imap.gmail.com" registration={form.register("imap_host")} error={errors.imap_host} />
              <FormField label="IMAP port" inputMode="numeric" placeholder="993" registration={form.register("imap_port")} error={errors.imap_port} />
            </div>
          </details>
        </>
      ) : (
        <>
          {CUSTOM_ENDPOINT.has(integration.provider) && (
            <FormField
              label={KEYLESS.has(integration.provider) ? "Server URL" : integration.provider === "custom" ? "Base URL" : "Base URL (optional)"}
              placeholder={
                integration.provider === "ollama"
                  ? "http://localhost:11434"
                  : integration.provider === "custom"
                    ? "https://api.example.com/v1"
                    : "https://api.openai.com/v1"
              }
              registration={form.register("base_url")}
              error={errors.base_url}
            />
          )}
          {!KEYLESS.has(integration.provider) && (
            <FormField
              label={OPTIONAL_KEY.has(integration.provider) ? "API key (if the endpoint needs one)" : "API key"}
              {...secret}
              placeholder="Paste the key"
              registration={form.register("api_key")}
              error={errors.api_key}
            />
          )}
          {integration.provider === "custom" && (
            <p className="text-xs text-slate-500">
              Any server that speaks the OpenAI chat completions API. The URL must be a public address; private and internal
              addresses are refused unless the server sets HTTP_ALLOW_PRIVATE_NETWORKS.
            </p>
          )}
          <FormField
            label={integration.provider === "custom" ? "Model" : "Default model (optional)"}
            placeholder={integration.default_model ?? ""}
            autoComplete="off"
            registration={form.register("model")}
            error={errors.model}
          />
        </>
      )}
      {connect.isError && <ErrorAlert message={connect.error.message} />}
      <div className="flex justify-end gap-2">
        <Button variant="secondary" size="sm" onClick={() => onDone(false)}>
          Cancel
        </Button>
        <Button type="submit" size="sm" loading={connect.isPending} data-testid={`save-integration-${integration.provider}`}>
          Save
        </Button>
      </div>
    </form>
  );
}

function SourceLine({ integration }: { integration: Integration }) {
  if (integration.source === "user") {
    return (
      <p className="flex items-center gap-1.5 text-sm text-emerald-700">
        <CircleCheck className="size-4" aria-hidden /> Connected with your credential
        {integration.connected_at && <span className="text-xs text-slate-400">· {formatRelative(integration.connected_at)}</span>}
      </p>
    );
  }
  if (integration.source === "server") {
    return (
      <p className="flex items-center gap-1.5 text-sm text-indigo-700">
        <KeyRound className="size-4" aria-hidden /> Using the server&apos;s credential (.env)
      </p>
    );
  }
  return (
    <p className="flex items-center gap-1.5 text-sm text-slate-500">
      <Unplug className="size-4" aria-hidden /> Not connected
    </p>
  );
}

function TestResult({ result }: { result: Pick<IntegrationTestResult, "success" | "latency_ms" | "error"> & { tested_at: string } }) {
  return result.success ? (
    <p className="flex items-center gap-1.5 text-xs text-emerald-700">
      <CircleCheck className="size-3.5" aria-hidden /> Works · {result.latency_ms} ms · {formatRelative(result.tested_at)}
    </p>
  ) : (
    <p className="flex items-start gap-1.5 text-xs text-red-700">
      <CircleAlert className="mt-px size-3.5 shrink-0" aria-hidden /> <span className="break-words">{result.error ?? "The test failed."}</span>
    </p>
  );
}

function IntegrationCard({ integration }: { integration: Integration }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const test = useMutation({
    meta: { silent: true },
    mutationFn: () => api.integrations.test(integration.provider),
    onSettled: () => void queryClient.invalidateQueries({ queryKey: ["integrations"] }),
  });
  const disconnect = useMutation({
    mutationFn: () => api.integrations.disconnect(integration.provider),
    onSuccess: () => {
      setConfirming(false);
      test.reset();
      void queryClient.invalidateQueries({ queryKey: ["integrations"] });
      toast.success(`${integration.label} disconnected`, "Runs fall back to the server's credential, if there is one.");
    },
  });
  const Icon = integration.kind === "messaging" ? ICONS[integration.provider as "telegram" | "discord"] ?? Send : ICONS[integration.kind];
  const latest = test.data ?? integration.last_test;
  const masked = Object.entries(integration.masked ?? {}).filter(([, v]) => v !== null && v !== "");

  return (
    <article className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-white p-4" data-testid={`integration-${integration.provider}`} data-source={integration.source}>
      <div className="flex items-start gap-3">
        <span
          className={`grid size-9 shrink-0 place-items-center rounded-lg ${TINTS[integration.kind]}`}
        >
          <Icon className="size-5" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold text-slate-900">{integration.label}</h3>
          <SourceLine integration={integration} />
        </div>
        {integration.get_key_url && (
          <a href={integration.get_key_url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-500">
            {KEYLESS.has(integration.provider)
              ? "Set up"
              : integration.kind === "email"
                ? "Get an app password"
                : integration.provider === "telegram"
                  ? "Create a bot"
                  : integration.provider === "discord"
                    ? "Create a webhook"
                    : integration.kind === "workspace"
                      ? "Get a token"
                      : "Get a key"}{" "}
            <ExternalLink className="size-3" />
          </a>
        )}
      </div>

      <dl className="space-y-1 text-xs">
        {masked.map(([key, value]) => (
          <div key={key} className="flex gap-2">
            <dt className="w-24 shrink-0 text-slate-400">{key.replace(/_/g, " ")}</dt>
            <dd className="truncate font-mono text-slate-700" data-testid={`masked-${integration.provider}-${key}`}>
              {String(value)}
            </dd>
          </div>
        ))}
        {integration.default_model && (
          <div className="flex gap-2">
            <dt className="w-24 shrink-0 text-slate-400">default model</dt>
            <dd className="truncate font-mono text-slate-700">{integration.default_model}</dd>
          </div>
        )}
      </dl>

      <div data-testid={`integration-test-${integration.provider}`} aria-live="polite">
        {test.isError ? (
          <p className="text-xs text-red-700">{test.error.message}</p>
        ) : latest ? (
          <div title={formatDateTime(latest.tested_at)}>
            <TestResult result={latest} />
          </div>
        ) : null}
      </div>

      {editing ? (
        <ConnectForm
          integration={integration}
          onDone={(saved) => {
            setEditing(false);
            if (saved) test.mutate();
          }}
        />
      ) : (
        <div className="mt-auto flex flex-wrap gap-2 border-t border-slate-100 pt-3">
          <Button size="sm" variant={integration.source === "user" ? "secondary" : "primary"} onClick={() => setEditing(true)} data-testid={`connect-${integration.provider}`}>
            <PlugZap className="size-3.5" aria-hidden /> {integration.source === "user" ? "Replace" : "Connect"}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => test.mutate()}
            loading={test.isPending}
            disabled={integration.source === "none" && !KEYLESS.has(integration.provider)}
            data-testid={`test-${integration.provider}`}
          >
            Test connection
          </Button>
          {integration.source === "user" && (
            <Button size="sm" variant="ghost" className="text-red-600 hover:bg-red-50" onClick={() => setConfirming(true)} data-testid={`disconnect-${integration.provider}`}>
              Disconnect
            </Button>
          )}
        </div>
      )}

      <ConfirmDialog
        open={confirming}
        title={`Disconnect ${integration.label}?`}
        message="Your stored credential is deleted. Runs use the server's credential if one is configured; otherwise nodes using this provider fail validation."
        confirmLabel="Disconnect"
        onConfirm={() => disconnect.mutate()}
        onClose={() => setConfirming(false)}
        busy={disconnect.isPending}
      />
    </article>
  );
}

export function IntegrationsScreen() {
  return (
    <AppShell>
      <Integrations />
    </AppShell>
  );
}

function BackgroundServices() {
  const queryClient = useQueryClient();
  const state = useQuery({ queryKey: ["automations"], queryFn: api.system.automations, refetchInterval: 15_000, meta: { silent: true } });
  const toggle = useMutation({
    mutationFn: ({ name, paused }: { name: "discord" | "telegram"; paused: boolean }) => api.system.pause(name, paused),
    onSuccess: (next) => queryClient.setQueryData(["automations"], next),
    onError: (error) => toast.error("Couldn't change that", error.message),
  });
  if (!state.data) return null; // not the owner of a public demo, or still loading
  const rows = [
    {
      name: "discord" as const,
      title: "Discord voice meetings",
      detail: state.data.discord.paused
        ? "Paused: the bot isn't watching any channel."
        : state.data.discord.watching
          ? "Watching the channel set on a Discord Voice Meeting block. It asks in Telegram before recording."
          : "On, but no pipeline has a Discord Voice Meeting block with a channel yet.",
      paused: state.data.discord.paused,
    },
    {
      name: "telegram" as const,
      title: "Telegram command center",
      detail: state.data.telegram.paused ? "Paused: messages to your bot are ignored (not replayed later)." : "Listening for messages to your bot.",
      paused: state.data.telegram.paused,
    },
  ];
  return (
    <section className="space-y-3" aria-label="Background services" data-testid="background-services">
      <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Background services</h2>
      <div className="grid gap-4 md:grid-cols-2">
        {rows.map((row) => (
          <div key={row.name} className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white p-4">
            <div>
              <h3 className="flex items-center gap-2 font-semibold text-slate-900">
                {row.title}
                <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${row.paused ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}`}>
                  {row.paused ? "paused" : "running"}
                </span>
              </h3>
              <p className="mt-1 text-xs leading-5 text-slate-600">{row.detail}</p>
            </div>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => toggle.mutate({ name: row.name, paused: !row.paused })}
              loading={toggle.isPending && toggle.variables?.name === row.name}
              data-testid={`${row.paused ? "resume" : "pause"}-${row.name}`}
            >
              {row.paused ? "Resume" : "Pause"}
            </Button>
          </div>
        ))}
      </div>
    </section>
  );
}

function Integrations() {
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: api.integrations.list });
  if (integrations.isPending) return <Spinner label="Loading integrations…" />;
  if (integrations.isError) {
    return (
      <div className="space-y-3">
        <ErrorAlert message={integrations.error.message} />
        <Button variant="secondary" onClick={() => void integrations.refetch()}>
          Try again
        </Button>
      </div>
    );
  }
  const groups: [string, Integration[]][] = [
    ["LLM providers", integrations.data.filter((i) => i.kind === "llm")],
    ["Email", integrations.data.filter((i) => i.kind === "email")],
    ["Notifications", integrations.data.filter((i) => i.kind === "messaging")],
    ["Web search", integrations.data.filter((i) => i.kind === "search")],
    ["Workspace apps", integrations.data.filter((i) => i.kind === "workspace")],
  ];
  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Integrations</h1>
        <p className="mt-1 max-w-2xl text-sm text-slate-500">
          Your credentials take priority over the server-wide ones in <code className="rounded bg-slate-100 px-1">.env</code>. They&apos;re
          encrypted at rest and never shown again, only masked. &ldquo;Test connection&rdquo; makes a real, minimal call (no tokens generated,
          no email or message sent).
        </p>
      </div>
      <BackgroundServices />
      {groups.map(([title, items]) =>
        items.length ? (
          <section key={title} className="space-y-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">{title}</h2>
            <div className="grid gap-4 md:grid-cols-2">
              {items.map((integration) => (
                <IntegrationCard key={integration.provider} integration={integration} />
              ))}
            </div>
          </section>
        ) : null,
      )}
    </div>
  );
}
