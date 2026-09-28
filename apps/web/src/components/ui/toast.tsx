"use client";

import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useEffect } from "react";
import { create } from "zustand";

type ToastKind = "error" | "success" | "info";

interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  message?: string;
  action?: { label: string; onClick: () => void };
}

interface ToastState {
  toasts: Toast[];
  push: (toast: Omit<Toast, "id">) => number;
  dismiss: (id: number) => void;
}

let nextId = 1;

const useToasts = create<ToastState>((set) => ({
  toasts: [],
  push: (toast) => {
    const id = nextId++;
    set((state) => ({ toasts: [...state.toasts.slice(-4), { ...toast, id }] }));
    return id;
  },
  dismiss: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}));

type Extra = Pick<Toast, "message" | "action">;

/** Callable from anywhere (event handlers, stores), not just components. */
export const toast = {
  error: (title: string, extra: Extra | string = {}) =>
    useToasts.getState().push({ kind: "error", title, ...(typeof extra === "string" ? { message: extra } : extra) }),
  success: (title: string, extra: Extra | string = {}) =>
    useToasts.getState().push({ kind: "success", title, ...(typeof extra === "string" ? { message: extra } : extra) }),
  info: (title: string, extra: Extra | string = {}) =>
    useToasts.getState().push({ kind: "info", title, ...(typeof extra === "string" ? { message: extra } : extra) }),
  dismiss: (id: number) => useToasts.getState().dismiss(id),
};

const styles: Record<ToastKind, { icon: typeof Info; tone: string }> = {
  error: { icon: CircleAlert, tone: "text-red-600" },
  success: { icon: CircleCheck, tone: "text-emerald-600" },
  info: { icon: Info, tone: "text-indigo-600" },
};

function ToastItem({ toast: item }: { toast: Toast }) {
  const dismiss = useToasts((s) => s.dismiss);
  useEffect(() => {
    const timer = setTimeout(() => dismiss(item.id), item.kind === "error" ? 9000 : 5000);
    return () => clearTimeout(timer);
  }, [dismiss, item.id, item.kind]);
  const { icon: Icon, tone } = styles[item.kind];
  return (
    <div
      role={item.kind === "error" ? "alert" : "status"}
      className="pointer-events-auto flex w-80 gap-3 rounded-lg border border-slate-200 bg-white p-3 shadow-lg"
    >
      <Icon className={`mt-0.5 size-4 shrink-0 ${tone}`} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-slate-900">{item.title}</p>
        {item.message && <p className="mt-0.5 break-words text-xs text-slate-600">{item.message}</p>}
        {item.action && (
          <button
            type="button"
            onClick={() => {
              item.action!.onClick();
              dismiss(item.id);
            }}
            className="mt-1.5 text-xs font-semibold text-indigo-600 hover:text-indigo-500"
          >
            {item.action.label}
          </button>
        )}
      </div>
      <button type="button" onClick={() => dismiss(item.id)} className="self-start text-slate-400 hover:text-slate-600" aria-label="Dismiss">
        <X className="size-4" />
      </button>
    </div>
  );
}

export function Toaster() {
  const toasts = useToasts((s) => s.toasts);
  return (
    <div className="pointer-events-none fixed right-4 top-16 z-[100] flex flex-col gap-2" aria-live="polite">
      {toasts.map((t) => (
        <ToastItem key={t.id} toast={t} />
      ))}
    </div>
  );
}
