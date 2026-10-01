"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, FileText, Image as ImageIcon, Library, Plus, RotateCcw, Search, Trash2, Upload } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { AppShell, Spinner } from "@/components/app-shell";
import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ConfirmDialog, Dialog } from "@/components/ui/dialog";
import { FormField } from "@/components/ui/form-field";
import { toast } from "@/components/ui/toast";
import { api } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import type { DocumentStatus, EmbeddingProvider, KnowledgeBase, KnowledgeDocument, KnowledgeSearchHit } from "@/lib/types";

const PROVIDERS: { value: EmbeddingProvider; label: string }[] = [
  { value: "gemini", label: "Gemini (gemini-embedding, free key)" },
  { value: "openai", label: "OpenAI (text-embedding-3-small)" },
  { value: "ollama", label: "Ollama (nomic-embed-text, local)" },
  { value: "mock", label: "Mock (word matching, no key; for trying it out)" },
];

const ACCEPT = "application/pdf,image/png,image/jpeg,image/tiff,image/webp,image/bmp,image/gif,text/plain,.txt,.md,.csv";

const DOC_STATUS: Record<DocumentStatus, string> = {
  pending: "bg-slate-100 text-slate-700 ring-slate-200",
  processing: "bg-blue-50 text-blue-700 ring-blue-200",
  ready: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
};

const METHOD: Record<string, string> = {
  text: "plain text",
  text_layer: "PDF text",
  ocr: "OCR",
  mixed: "PDF text + OCR",
};

function DocStatus({ status }: { status: DocumentStatus }) {
  return (
    <span
      data-testid="document-status"
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${DOC_STATUS[status]}`}
    >
      <span className={`size-1.5 rounded-full bg-current ${status === "processing" ? "animate-pulse" : ""}`} aria-hidden />
      {status}
    </span>
  );
}

// --- List ------------------------------------------------------------------------------------

const createSchema = z
  .object({
    name: z
      .string()
      .trim()
      .min(1, "Give it a name")
      .max(100, "At most 100 characters")
      .refine((v) => !/[{}]/.test(v), "No { or } in the name"),
    description: z.string().max(2000),
    embedding_provider: z.enum(["gemini", "openai", "ollama", "mock"]),
    chunk_size: z.number({ error: "Enter a number" }).int().min(200, "At least 200").max(8000, "At most 8000"),
    chunk_overlap: z.number({ error: "Enter a number" }).int().min(0, "At least 0").max(2000, "At most 2000"),
  })
  .refine((v) => v.chunk_overlap < v.chunk_size, { path: ["chunk_overlap"], message: "Must be smaller than the chunk size" });

type CreateValues = z.infer<typeof createSchema>;

function CreateDialog({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: "", description: "", embedding_provider: "gemini", chunk_size: 1000, chunk_overlap: 150 },
  });
  const create = useMutation({
    meta: { silent: true },
    mutationFn: api.knowledgeBases.create,
    onSuccess: (kb) => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });
      router.push(`/knowledge/${kb.id}`);
    },
  });
  const submit = form.handleSubmit((values) => create.mutate(values));
  const errors = form.formState.errors;
  return (
    <Dialog
      open
      title="New knowledge base"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} loading={create.isPending} data-testid="create-kb">
            Create
          </Button>
        </>
      }
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        {create.isError && <ErrorAlert message={create.error.message} />}
        <FormField label="Name" placeholder="Company handbook" autoFocus registration={form.register("name")} error={errors.name} />
        <FormField label="Description (optional)" registration={form.register("description")} error={errors.description} />
        <div className="space-y-1.5">
          <label htmlFor="kb-provider" className="block text-sm font-medium text-slate-700">
            Embedding model
          </label>
          <select
            id="kb-provider"
            {...form.register("embedding_provider")}
            className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200"
          >
            {PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
          <p className="text-xs text-slate-500">Turns text into vectors for searching by meaning. It can&apos;t change later.</p>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <FormField label="Chunk size (characters)" type="number" registration={form.register("chunk_size", { valueAsNumber: true })} error={errors.chunk_size} />
          <FormField label="Overlap (characters)" type="number" registration={form.register("chunk_overlap", { valueAsNumber: true })} error={errors.chunk_overlap} />
        </div>
      </form>
    </Dialog>
  );
}

export function KnowledgeListScreen() {
  return (
    <AppShell>
      <KnowledgeList />
    </AppShell>
  );
}

function KnowledgeList() {
  const [creating, setCreating] = useState(false);
  const bases = useQuery({ queryKey: ["knowledge-bases"], queryFn: api.knowledgeBases.list });
  return (
    <div className="space-y-4">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Knowledge bases</h1>
          <p className="mt-1 text-sm text-slate-500">
            Add documents, then search them by meaning with the Retriever node (or the test search here).
          </p>
        </div>
        <Button onClick={() => setCreating(true)} data-testid="new-kb">
          <Plus className="size-4" aria-hidden /> New knowledge base
        </Button>
      </div>
      {bases.isPending ? (
        <Spinner label="Loading knowledge bases…" />
      ) : bases.isError ? (
        <ErrorAlert message={bases.error.message} />
      ) : bases.data.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white p-12 text-center">
          <Library className="mx-auto size-8 text-slate-300" aria-hidden />
          <p className="mt-3 text-sm font-medium text-slate-700">No knowledge bases yet</p>
          <p className="mt-1 text-sm text-slate-500">Create one and upload PDFs, scans, or text files.</p>
          <Button className="mt-4" onClick={() => setCreating(true)}>
            <Plus className="size-4" aria-hidden /> New knowledge base
          </Button>
        </div>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {bases.data.map((kb) => (
            <li key={kb.id}>
              <Link
                href={`/knowledge/${kb.id}`}
                data-testid="kb-card"
                className="block h-full rounded-xl border border-slate-200 bg-white p-4 shadow-sm hover:border-indigo-300"
              >
                <div className="flex items-center gap-2">
                  <Library className="size-4 text-cyan-600" aria-hidden />
                  <span className="truncate font-medium text-slate-900">{kb.name}</span>
                </div>
                {kb.description && <p className="mt-1 line-clamp-2 text-sm text-slate-500">{kb.description}</p>}
                <p className="mt-3 text-xs text-slate-500">
                  {kb.document_count} document{kb.document_count === 1 ? "" : "s"} · {kb.chunk_count} chunks ·{" "}
                  {kb.embedding_provider}
                </p>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {creating && <CreateDialog onClose={() => setCreating(false)} />}
    </div>
  );
}

// --- Detail ----------------------------------------------------------------------------------

export function KnowledgeDetailScreen({ id }: { id: string }) {
  return (
    <AppShell>
      <KnowledgeDetail id={id} />
    </AppShell>
  );
}

const busy = (docs: KnowledgeDocument[] | undefined) => docs?.some((d) => d.status === "pending" || d.status === "processing");

function KnowledgeDetail({ id }: { id: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [deleting, setDeleting] = useState(false);
  const kb = useQuery({ queryKey: ["knowledge-bases", id], queryFn: () => api.knowledgeBases.get(id) });
  const documents = useQuery({
    queryKey: ["knowledge-bases", id, "documents"],
    queryFn: () => api.knowledgeBases.documents(id),
    // Watch processing until every document is ready or failed.
    refetchInterval: (query) => (busy(query.state.data) ? 1500 : false),
  });
  const remove = useMutation({
    mutationFn: () => api.knowledgeBases.remove(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["knowledge-bases"] });
      toast.success("Knowledge base deleted");
      router.push("/knowledge");
    },
  });

  if (kb.isPending) return <Spinner label="Loading…" />;
  if (kb.isError) return <ErrorAlert message={kb.error.message} />;
  const base = kb.data;
  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <Link href="/knowledge" className="inline-flex items-center gap-1 text-sm text-slate-500 hover:text-slate-700">
            <ArrowLeft className="size-3.5" aria-hidden /> Knowledge bases
          </Link>
          <h1 className="mt-1 truncate text-xl font-semibold text-slate-900" data-testid="kb-name">
            {base.name}
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            {base.description ? `${base.description} · ` : ""}
            {base.embedding_provider}
            {base.embedding_model ? ` (${base.embedding_model})` : ""} · {base.dimensions} dimensions · chunks of {base.chunk_size}{" "}
            characters, {base.chunk_overlap} overlap
          </p>
          <p className="mt-1 text-xs text-slate-500">
            In a pipeline, set a Retriever&apos;s <code className="rounded bg-slate-100 px-1">knowledge_base</code> to{" "}
            <code className="rounded bg-slate-100 px-1">{base.name}</code>.
          </p>
        </div>
        <Button variant="danger" size="sm" onClick={() => setDeleting(true)}>
          <Trash2 className="size-3.5" aria-hidden /> Delete
        </Button>
      </div>

      <Documents kb={base} documents={documents} />
      <TestSearch kb={base} hasReady={Boolean(documents.data?.some((d) => d.status === "ready"))} />

      <ConfirmDialog
        open={deleting}
        title="Delete knowledge base?"
        message={<>“{base.name}”, its documents, and all their chunks will be deleted. The uploaded files stay.</>}
        onConfirm={() => remove.mutate()}
        onClose={() => setDeleting(false)}
        busy={remove.isPending}
      />
    </div>
  );
}

function Documents({ kb, documents }: { kb: KnowledgeBase; documents: ReturnType<typeof useQuery<KnowledgeDocument[]>> }) {
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<{ name: string; fraction: number } | null>(null);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["knowledge-bases", kb.id] });
    void queryClient.invalidateQueries({ queryKey: ["knowledge-bases"], exact: true });
  };
  const upload = useMutation({
    meta: { silent: true },
    mutationFn: async (files: File[]) => {
      for (const file of files) {
        setProgress({ name: file.name, fraction: 0 });
        await api.knowledgeBases.upload(kb.id, file, { onProgress: (fraction) => setProgress({ name: file.name, fraction }) });
        refresh();
      }
    },
    onSettled: () => {
      setProgress(null);
      refresh();
    },
    onError: (error) => toast.error("Upload failed", error.message),
  });
  const retry = useMutation({ mutationFn: (docId: string) => api.knowledgeBases.retry(kb.id, docId), onSuccess: refresh });
  const remove = useMutation({
    mutationFn: (docId: string) => api.knowledgeBases.removeDocument(kb.id, docId),
    onSuccess: () => {
      refresh();
      toast.success("Document removed");
    },
  });

  return (
    <section className="space-y-3">
      <div className="flex items-end justify-between gap-4">
        <h2 className="text-base font-semibold text-slate-900">Documents</h2>
        <div className="flex items-center gap-3">
          {progress && (
            <span className="text-xs text-slate-500" data-testid="upload-progress">
              Uploading {progress.name}… {Math.round(progress.fraction * 100)}%
            </span>
          )}
          <input
            ref={input}
            type="file"
            multiple
            accept={ACCEPT}
            className="hidden"
            data-testid="kb-file-input"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              e.target.value = "";
              if (files.length) upload.mutate(files);
            }}
          />
          <Button size="sm" onClick={() => input.current?.click()} loading={upload.isPending} data-testid="kb-upload">
            <Upload className="size-3.5" aria-hidden /> Add documents
          </Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">PDFs (scanned pages are OCR&apos;d), images, and plain-text files.</p>
      {documents.isPending ? (
        <Spinner label="Loading documents…" />
      ) : documents.isError ? (
        <ErrorAlert message={documents.error.message} />
      ) : documents.data.length === 0 ? (
        <p className="rounded-xl border border-dashed border-slate-300 bg-white p-6 text-center text-sm text-slate-500">
          No documents yet. Add one to start searching.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-xs font-medium text-slate-500">
              <tr>
                <th className="px-4 py-2.5">File</th>
                <th className="px-4 py-2.5">Status</th>
                <th className="px-4 py-2.5">Chunks</th>
                <th className="px-4 py-2.5">Added</th>
                <th className="px-4 py-2.5">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {documents.data.map((doc) => (
                <tr key={doc.id} data-testid="document-row" data-name={doc.filename}>
                  <td className="max-w-72 px-4 py-3">
                    <div className="flex items-center gap-2">
                      {doc.source_type === "image" ? (
                        <ImageIcon className="size-4 shrink-0 text-slate-400" aria-hidden />
                      ) : (
                        <FileText className="size-4 shrink-0 text-slate-400" aria-hidden />
                      )}
                      <span className="truncate font-medium text-slate-900">{doc.filename}</span>
                    </div>
                    {doc.error && <p className="mt-1 text-xs text-red-600">{doc.error}</p>}
                  </td>
                  <td className="px-4 py-3">
                    <DocStatus status={doc.status} />
                  </td>
                  <td className="px-4 py-3 text-slate-600">
                    {doc.status === "ready" ? (
                      <>
                        {doc.chunk_count}
                        <span className="text-xs text-slate-400">
                          {" "}
                          · {doc.char_count.toLocaleString()} chars{doc.method ? ` · ${METHOD[doc.method] ?? doc.method}` : ""}
                        </span>
                      </>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-4 py-3 text-slate-600" title={formatDateTime(doc.created_at)}>
                    {formatRelative(doc.created_at)}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      {doc.status === "failed" && (
                        <Button variant="secondary" size="sm" onClick={() => retry.mutate(doc.id)} aria-label={`Retry ${doc.filename}`}>
                          <RotateCcw className="size-3.5" aria-hidden /> Retry
                        </Button>
                      )}
                      <Button variant="ghost" size="sm" onClick={() => remove.mutate(doc.id)} aria-label={`Remove ${doc.filename}`}>
                        <Trash2 className="size-3.5" aria-hidden />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function TestSearch({ kb, hasReady }: { kb: KnowledgeBase; hasReady: boolean }) {
  const [query, setQuery] = useState("");
  const search = useMutation({
    meta: { silent: true },
    mutationFn: (q: string) => api.knowledgeBases.search(kb.id, q, 5),
  });
  return (
    <section className="space-y-3">
      <h2 className="text-base font-semibold text-slate-900">Test search</h2>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) search.mutate(query.trim());
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={hasReady ? "Ask something the documents answer…" : "Add a document first"}
          aria-label="Search query"
          data-testid="kb-search-input"
          className="block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200"
        />
        <Button type="submit" loading={search.isPending} disabled={!query.trim()} data-testid="kb-search">
          <Search className="size-4" aria-hidden /> Search
        </Button>
      </form>
      {search.isError && <ErrorAlert message={search.error.message} />}
      {search.data &&
        (search.data.results.length === 0 ? (
          <p className="text-sm text-slate-500">No chunks found{hasReady ? "" : ": no document is ready yet"}.</p>
        ) : (
          <ol className="space-y-2" data-testid="kb-results">
            {search.data.results.map((hit) => (
              <SearchResult key={hit.chunk_id} hit={hit} />
            ))}
          </ol>
        ))}
    </section>
  );
}

function SearchResult({ hit }: { hit: KnowledgeSearchHit }) {
  return (
    <li className="rounded-xl border border-slate-200 bg-white p-3" data-testid="kb-result">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span className="rounded bg-indigo-50 px-1.5 py-0.5 font-semibold text-indigo-700">[{hit.rank}]</span>
        <span className="font-medium text-slate-700">{hit.filename}</span>
        {hit.page ? <span>page {hit.page}</span> : null}
        <span>chunk {hit.chunk_index}</span>
        <span className="ml-auto tabular-nums" title="Cosine similarity: 1 is identical in meaning">
          score {hit.score.toFixed(3)}
        </span>
      </div>
      <p className="mt-2 whitespace-pre-wrap text-sm text-slate-700">{hit.content}</p>
    </li>
  );
}
