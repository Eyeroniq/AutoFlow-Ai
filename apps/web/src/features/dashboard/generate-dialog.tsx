"use client";

import { useMutation } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { toast } from "@/components/ui/toast";
import { ApiError, api } from "@/lib/api";

const EXAMPLES = [
  "Summarize my unread emails and Telegram me the summary",
  "Extract the vendor, amounts, and dates from an uploaded invoice",
  "Search the web for news about pgvector and email me a short summary with links",
];

/** The problems the server lists when no draft validated (422 `detail.problems`). */
export function generationProblems(error: unknown): string[] {
  if (!(error instanceof ApiError)) return [];
  const detail = (error.body as { detail?: { problems?: unknown } } | undefined)?.detail;
  return Array.isArray(detail?.problems) ? detail.problems.map(String) : [];
}

export function GenerateDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [prompt, setPrompt] = useState("");
  const generate = useMutation({
    meta: { silent: true },
    mutationFn: (text: string) => api.workflows.generate(text),
    onSuccess: (result) => {
      toast.success(
        `Generated “${result.workflow.name}”`,
        result.warnings.length ? `Before it runs: ${result.warnings.join(" · ")}` : "Review it, then save or run.",
      );
      router.push(`/pipelines/${result.workflow.id}`);
    },
  });
  const problems = generationProblems(generate.error);
  const valid = prompt.trim().length >= 5;
  return (
    <Dialog
      open
      wide
      title="Generate a pipeline with AI"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => generate.mutate(prompt.trim())} disabled={!valid} loading={generate.isPending} data-testid="generate-submit">
            <Sparkles className="size-4" aria-hidden /> Generate
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <label htmlFor="generate-prompt" className="block text-sm font-medium text-slate-800">
          Describe what it should do
        </label>
        <textarea
          id="generate-prompt"
          rows={4}
          autoFocus
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && valid) generate.mutate(prompt.trim());
          }}
          placeholder="e.g. Every morning, read my unread emails, summarize them, and send me the summary on Telegram"
          className="block w-full rounded-lg border border-slate-300 px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200"
          data-testid="generate-prompt"
        />
        <div className="flex flex-wrap gap-1.5">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => setPrompt(example)}
              className="rounded-full bg-slate-100 px-2.5 py-1 text-xs text-slate-600 hover:bg-indigo-50 hover:text-indigo-700"
            >
              {example}
            </button>
          ))}
        </div>
        <p className="text-xs text-slate-500">
          An LLM drafts the nodes and their settings from the node library; FlowForge checks the draft (and has it fixed, up to three tries) and
          only opens it once it validates. It opens in the editor for you to review before anything runs.
        </p>
        {generate.isPending && <p className="text-sm text-slate-500">Designing the pipeline… (this takes 5–20 seconds)</p>}
        {generate.isError && (
          <div className="space-y-2" data-testid="generate-error">
            <ErrorAlert message={generate.error.message} />
            {problems.length > 0 && (
              <ul className="list-disc space-y-0.5 pl-5 text-xs text-red-700">
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}
