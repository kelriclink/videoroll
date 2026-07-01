import { PropsWithChildren, ReactNode, useCallback, useMemo, useState } from "react";
import { ConfirmOptions, FeedbackContext, Toast, ToastKind } from "./feedbackContext";

type ConfirmState = ConfirmOptions & {
  resolve: (value: boolean) => void;
};

const toastClasses: Record<ToastKind, string> = {
  success: "border-emerald-200 bg-emerald-50 text-emerald-900",
  error: "border-rose-200 bg-rose-50 text-rose-900",
  info: "border-sky-200 bg-sky-50 text-sky-900",
  warning: "border-amber-200 bg-amber-50 text-amber-950",
};

function clampToastText(value: ReactNode): ReactNode {
  if (typeof value !== "string") return value;
  const text = value.trim();
  return text.length > 700 ? `${text.slice(0, 699)}...` : text;
}

export function FeedbackProvider({ children }: PropsWithChildren) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirmState, setConfirmState] = useState<ConfirmState | null>(null);

  const dismissToast = useCallback((id: number) => {
    setToasts((items) => items.filter((item) => item.id !== id));
  }, []);

  const toast = useCallback(
    (next: Omit<Toast, "id">) => {
      const id = Date.now() + Math.floor(Math.random() * 10000);
      setToasts((items) => [...items, { ...next, id }].slice(-4));
      window.setTimeout(() => dismissToast(id), next.kind === "error" ? 9000 : 5000);
    },
    [dismissToast],
  );

  const confirm = useCallback((options: ConfirmOptions) => {
    return new Promise<boolean>((resolve) => {
      setConfirmState({ ...options, resolve });
    });
  }, []);

  const value = useMemo(() => ({ toast, confirm }), [toast, confirm]);

  function closeConfirm(result: boolean) {
    const current = confirmState;
    if (!current) return;
    setConfirmState(null);
    current.resolve(result);
  }

  const confirmTone = confirmState?.tone ?? "default";
  const confirmButtonClass =
    confirmTone === "danger"
      ? "bg-rose-700 text-white hover:bg-rose-800"
      : confirmTone === "warning"
        ? "bg-amber-600 text-white hover:bg-amber-700"
        : "bg-slate-900 text-white hover:bg-slate-800";

  return (
    <FeedbackContext.Provider value={value}>
      {children}

      <div className="pointer-events-none fixed right-4 top-4 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
        {toasts.map((item) => (
          <div key={item.id} className={`pointer-events-auto rounded-md border p-3 shadow-lg ${toastClasses[item.kind]}`}>
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-sm font-semibold">{item.title}</div>
                {item.message ? <div className="mt-1 whitespace-pre-wrap break-words text-xs">{clampToastText(item.message)}</div> : null}
              </div>
              <button
                type="button"
                className="rounded px-1 text-xs opacity-70 hover:bg-white/60 hover:opacity-100"
                onClick={() => dismissToast(item.id)}
                aria-label="Dismiss"
              >
                x
              </button>
            </div>
          </div>
        ))}
      </div>

      {confirmState ? (
        <div className="fixed inset-0 z-40 flex items-center justify-center bg-slate-950/40 px-4 py-6">
          <div className="w-full max-w-md rounded-md border border-slate-200 bg-white p-4 shadow-xl">
            <div className="text-base font-semibold text-slate-950">{confirmState.title}</div>
            {confirmState.message ? <div className="mt-2 whitespace-pre-wrap break-words text-sm text-slate-700">{confirmState.message}</div> : null}
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="rounded-md border px-3 py-2 text-sm hover:bg-slate-50" onClick={() => closeConfirm(false)}>
                {confirmState.cancelLabel ?? "取消"}
              </button>
              <button type="button" className={`rounded-md px-3 py-2 text-sm ${confirmButtonClass}`} onClick={() => closeConfirm(true)}>
                {confirmState.confirmLabel ?? "确认"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </FeedbackContext.Provider>
  );
}
