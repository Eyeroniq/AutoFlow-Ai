"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ExternalLink, ShieldCheck, X } from "lucide-react";
import { useEffect } from "react";

import { api } from "@/lib/api";

import { ConnectForm } from "@/features/integrations/integrations";

import { HOW_TO } from "./connect-model";
import { useEditorUi } from "./ui-store";

/** The integrations list and the deployment mode, shared by the nodes that ask for a connection. */
export function useConnections() {
  const integrations = useQuery({ queryKey: ["integrations"], queryFn: api.integrations.list, staleTime: 30_000, meta: { silent: true } });
  const system = useQuery({ queryKey: ["system"], queryFn: api.system.get, staleTime: 5 * 60_000, meta: { silent: true } });
  return { integrations: integrations.data, system: system.data };
}

/**
 * A slide-over for connecting an account without leaving the editor: opened from the "Connect your own
 * Gmail" prompt on a node. It holds the same write-only form as the Integrations page, with the steps for
 * getting the token or webhook one click away.
 */
export function ConnectSlideOver() {
  const provider = useEditorUi((s) => s.connectProvider);
  const close = () => useEditorUi.getState().set({ connectProvider: null });
  const { integrations, system } = useConnections();
  const integration = integrations?.find((i) => i.provider === provider);
  const howTo = provider ? HOW_TO[provider] : undefined;

  useEffect(() => {
    if (!provider) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") useEditorUi.getState().set({ connectProvider: null });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [provider]);

  if (!provider || !integration) return null;
  const demo = system?.public_demo && !system.is_owner;
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-slate-900/30" onMouseDown={close} data-testid="connect-slideover-backdrop">
      <aside
        role="dialog"
        aria-modal="true"
        aria-label={`Connect your own ${integration.label}`}
        data-testid="connect-slideover"
        onMouseDown={(e) => e.stopPropagation()}
        className="flex h-full w-full max-w-md flex-col bg-white shadow-2xl"
      >
        <header className="flex items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-slate-900">Connect your own {integration.label}</h2>
            <p className="mt-0.5 text-xs text-slate-500">Your credential is stored encrypted and used only by your workflows.</p>
          </div>
          <button type="button" onClick={close} aria-label="Close" className="text-slate-400 hover:text-slate-600">
            <X className="size-4" />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {demo && (
            <p className="flex gap-2 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900" data-testid="demo-boundary-note">
              <ShieldCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
              <span>
                This is a public demo, so its owner&apos;s own {integration.label} account is never available to visitors. It would let anyone
                use their inbox, bot, server, or workspace. Connect yours and this node works as you.
              </span>
            </p>
          )}

          {howTo && (
            <details className="group rounded-lg border border-slate-200" data-testid="connect-howto">
              <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-sm font-medium text-slate-800">
                How do I get this?
                <ChevronDown className="size-4 text-slate-400 transition-transform group-open:rotate-180" aria-hidden />
              </summary>
              <div className="space-y-2 border-t border-slate-100 px-3 py-3 text-sm text-slate-700">
                <p className="font-medium">{howTo.title}</p>
                <ol className="list-decimal space-y-1 pl-5">
                  {howTo.steps.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
                {howTo.note && <p className="text-xs text-slate-500">{howTo.note}</p>}
                {howTo.link && (
                  <a
                    href={howTo.link.href}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs font-medium text-indigo-600 hover:text-indigo-500"
                  >
                    {howTo.link.label} <ExternalLink className="size-3" aria-hidden />
                  </a>
                )}
              </div>
            </details>
          )}

          <ConnectForm integration={integration} onDone={close} />
        </div>
      </aside>
    </div>
  );
}
