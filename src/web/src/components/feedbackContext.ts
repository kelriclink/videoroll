import { createContext, ReactNode, useContext } from "react";

export type ToastKind = "success" | "error" | "info" | "warning";

export type Toast = {
  id: number;
  kind: ToastKind;
  title: string;
  message?: ReactNode;
};

export type ConfirmOptions = {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger" | "warning";
};

export type FeedbackContextValue = {
  toast: (toast: Omit<Toast, "id">) => void;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
};

export const FeedbackContext = createContext<FeedbackContextValue | null>(null);

export function useToast() {
  const ctx = useContext(FeedbackContext);
  if (!ctx) throw new Error("useToast must be used inside FeedbackProvider");
  return ctx.toast;
}

export function useConfirm() {
  const ctx = useContext(FeedbackContext);
  if (!ctx) throw new Error("useConfirm must be used inside FeedbackProvider");
  return ctx.confirm;
}
