"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useState } from "react";

import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import type { PrivacySettings } from "@/lib/types";

import { useEditor } from "./store";
import { useEditorUi } from "./ui-store";

export const privacyKey = (workflowId: string | null) => ["workflow", workflowId, "privacy"] as const;

function Toggle({ label, help, checked, onChange, testId }: { label: string; help: string; checked: boolean; onChange: (v: boolean) => void; testId: string }) {
  return (
    <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-3 hover:bg-slate-50">
      <input type="checkbox" className="mt-0.5 size-4 accent-indigo-600" checked={checked} onChange={(e) => onChange(e.target.checked)} data-testid={testId} />
      <span>
        <span className="block text-sm font-medium text-slate-800">{label}</span>
        <span className="mt-0.5 block text-xs text-slate-500">{help}</span>
      </span>
    </label>
  );
}

export function PrivacyPanel() {
  const workflowId = useEditor((s) => s.workflowId);
  const queryClient = useQueryClient();
  const close = () => useEditorUi.getState().set({ rightPanel: null });
  const settings = useQuery({
    queryKey: privacyKey(workflowId),
    queryFn: () => api.workflows.privacy(workflowId!),
    enabled: Boolean(workflowId),
  });

  return (
    <aside className="flex w-[26rem] shrink-0 flex-col border-l border-slate-200 bg-white" aria-label="Privacy" data-testid="privacy-panel">
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-900">Privacy</h2>
        <button type="button" onClick={close} className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Close panel">
          <X className="size-4" />
        </button>
      </div>
      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        <p className="text-xs text-slate-500">
          Every outbound node (Gmail, Telegram, Discord, HTTP Request, Notion, Airtable) checks its content before sending: it{" "}
          <b>blocks</b> secrets, card numbers, Aadhaar and PAN by default. LLM nodes <b>redact</b> them. Change it per node with{" "}
          <code className="rounded bg-slate-100 px-1">privacy_guard</code> (block, redact, warn, off).
        </p>
        {settings.isPending ? (
          <p className="text-sm text-slate-400">Loading…</p>
        ) : settings.isError ? (
          <ErrorAlert message={settings.error.message} />
        ) : (
          // Keyed by the saved settings, so the form starts from them again after each save.
          <PrivacyForm
            key={JSON.stringify(settings.data)}
            workflowId={workflowId!}
            saved={settings.data}
            onSaved={(data) => queryClient.setQueryData(privacyKey(workflowId), data)}
          />
        )}
      </div>
    </aside>
  );
}

function PrivacyForm({ workflowId, saved, onSaved }: { workflowId: string; saved: PrivacySettings; onSaved: (data: PrivacySettings) => void }) {
  const [draft, setDraft] = useState<PrivacySettings>(saved);
  const [allowlist, setAllowlist] = useState(saved.allowlist.join("\n"));
  const save = useMutation({
    meta: { silent: true },
    mutationFn: (body: PrivacySettings) => api.workflows.savePrivacy(workflowId, body),
    onSuccess: (data) => {
      toast.success("Privacy settings saved", "They apply to runs from now on.");
      onSaved(data);
    },
    onError: (error) => toast.error("Couldn't save the privacy settings", error.message),
  });
  const next = { ...draft, allowlist: allowlist.split("\n").map((line) => line.trim()).filter(Boolean) };
  const dirty = JSON.stringify(next) !== JSON.stringify(saved);

  return (
    <>
      <Toggle
        label="Mask sensitive data in stored step inputs/outputs"
        help="Run history, live updates, and the final output show [REDACTED:TYPE] instead of what was found. The next steps still get the real values."
        checked={draft.mask_stored_io}
        onChange={(v) => setDraft({ ...draft, mask_stored_io: v })}
        testId="privacy-mask"
      />
      <Toggle
        label="Also detect personal data"
        help="Names, emails, phone numbers, and locations count too: for the guard, masking, and the Privacy Report."
        checked={draft.detect_personal_data}
        onChange={(v) => setDraft({ ...draft, detect_personal_data: v })}
        testId="privacy-personal"
      />
      <div className="space-y-1.5">
        <label htmlFor="privacy-allowlist" className="block text-sm font-medium text-slate-800">
          Allowlist
        </label>
        <textarea
          id="privacy-allowlist"
          rows={4}
          value={allowlist}
          onChange={(e) => setAllowlist(e.target.value)}
          placeholder={"support@mycompany.com\nre:TEST-[0-9]+"}
          className="block w-full rounded-lg border border-slate-300 px-3 py-2 font-mono text-xs focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200"
          data-testid="privacy-allowlist"
        />
        <p className="text-xs text-slate-500">Known-safe values to ignore, one per line: exact text, or re: followed by a regex.</p>
      </div>
      {save.isError && <ErrorAlert message={save.error.message} />}
      <Button size="sm" onClick={() => save.mutate(next)} disabled={!dirty} loading={save.isPending} data-testid="privacy-save">
        Save privacy settings
      </Button>
    </>
  );
}
