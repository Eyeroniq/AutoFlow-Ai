"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Image as ImageIcon, Upload, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import type { UploadedFile } from "@/lib/types";

/** What the file input accepts; the server checks the real type from the file's content. */
export const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.webp,.bmp,.gif,.txt,application/pdf,image/*,text/plain";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function useUploads() {
  return useQuery({ queryKey: ["files"], queryFn: api.files.list, staleTime: 30_000, meta: { silent: true } });
}

interface UploadState {
  status: "idle" | "uploading" | "error";
  progress: number;
  filename: string | null;
  error: string | null;
}

/** Uploads one file at a time with progress; the uploads list refreshes afterwards. */
export function useFileUpload() {
  const queryClient = useQueryClient();
  const [state, setState] = useState<UploadState>({ status: "idle", progress: 0, filename: null, error: null });
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);

  const upload = async (file: File): Promise<UploadedFile | null> => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setState({ status: "uploading", progress: 0, filename: file.name, error: null });
    try {
      const uploaded = await api.files.upload(file, {
        signal: abort.signal,
        onProgress: (progress) => setState((s) => ({ ...s, progress })),
      });
      queryClient.setQueryData<UploadedFile[]>(["files"], (files) => [uploaded, ...(files ?? []).filter((f) => f.id !== uploaded.id)]);
      setState({ status: "idle", progress: 1, filename: file.name, error: null });
      return uploaded;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setState({ status: "idle", progress: 0, filename: null, error: null });
        return null;
      }
      const message = error instanceof ApiError || error instanceof Error ? error.message : "The upload failed";
      setState({ status: "error", progress: 0, filename: file.name, error: message });
      return null;
    }
  };
  return { ...state, upload, cancel: () => controller.current?.abort() };
}

interface FileChooserProps {
  /** The chosen upload's id (or anything else, e.g. a {{reference}}, which is shown as-is). */
  value: string | null | undefined;
  onChange: (fileId: string | undefined) => void;
  id?: string;
  invalid?: boolean;
  describedBy?: string;
  /** Shown instead of "No file chosen" when empty (e.g. the Input node's default). */
  emptyLabel?: string;
  testId?: string;
  onUploadingChange?: (uploading: boolean) => void;
}

/** Pick one of your uploads, or upload a new file (with a progress bar). */
export function FileChooser({ value, onChange, id, invalid, describedBy, emptyLabel, testId, onUploadingChange }: FileChooserProps) {
  const uploads = useUploads();
  const upload = useFileUpload();
  const picker = useRef<HTMLInputElement>(null);
  const generated = useId();
  const selectId = id ?? generated;
  const selected = uploads.data?.find((f) => f.id === value);
  const uploading = upload.status === "uploading";

  useEffect(() => onUploadingChange?.(uploading), [uploading, onUploadingChange]);

  const onPick = async (file: File | undefined) => {
    if (!file) return;
    const uploaded = await upload.upload(file);
    if (uploaded) onChange(uploaded.id);
    if (picker.current) picker.current.value = "";
  };

  const unknownValue = value && !selected && !uploads.isPending;
  return (
    <div className="space-y-1.5" data-testid={testId}>
      <div className="flex gap-1.5">
        <select
          id={selectId}
          value={selected ? selected.id : ""}
          onChange={(e) => onChange(e.target.value || undefined)}
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          disabled={uploading}
          className={`nodrag min-w-0 flex-1 rounded-md border bg-white px-2 py-1.5 text-sm text-slate-900 shadow-sm focus:outline-none focus:ring-2 ${
            invalid ? "border-red-400 focus:ring-red-100" : "border-slate-300 focus:border-indigo-500 focus:ring-indigo-100"
          }`}
        >
          <option value="">{uploads.isPending ? "Loading your files…" : (emptyLabel ?? "No file chosen")}</option>
          {uploads.data?.map((file) => (
            <option key={file.id} value={file.id}>
              {file.filename} ({formatBytes(file.size_bytes)})
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => picker.current?.click()}
          disabled={uploading}
          className="flex shrink-0 items-center gap-1 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
        >
          <Upload className="size-3.5" aria-hidden /> Upload
        </button>
        <input
          ref={picker}
          type="file"
          accept={ACCEPT}
          className="hidden"
          aria-label="Choose a file to upload"
          data-testid={testId ? `${testId}-input` : undefined}
          onChange={(e) => void onPick(e.target.files?.[0])}
        />
      </div>

      {uploading && (
        <div className="space-y-1" role="status" aria-live="polite">
          <div className="flex items-center justify-between text-[11px] text-slate-600">
            <span className="truncate">Uploading {upload.filename}…</span>
            <span className="flex items-center gap-1.5">
              {Math.round(upload.progress * 100)}%
              <button type="button" onClick={upload.cancel} className="rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700" aria-label="Cancel upload">
                <X className="size-3" />
              </button>
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-indigo-500 transition-[width]"
              style={{ width: `${Math.max(2, Math.round(upload.progress * 100))}%` }}
              data-testid={testId ? `${testId}-progress` : undefined}
            />
          </div>
        </div>
      )}
      {upload.status === "error" && (
        <p className="text-[11px] text-red-600" role="alert">
          {upload.filename}: {upload.error}
        </p>
      )}
      {selected && !uploading && (
        <p className="flex items-center gap-1.5 text-[11px] text-slate-500" data-testid={testId ? `${testId}-selected` : undefined}>
          {selected.content_type.startsWith("image/") ? <ImageIcon className="size-3.5" aria-hidden /> : <FileText className="size-3.5" aria-hidden />}
          {selected.filename} · {selected.content_type} · {formatBytes(selected.size_bytes)}
        </p>
      )}
      {unknownValue && <p className="text-[11px] text-amber-700">{`Not one of your uploads: ${value}`}</p>}
    </div>
  );
}
