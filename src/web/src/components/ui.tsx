import { ButtonHTMLAttributes, PropsWithChildren, ReactNode, useEffect, useRef, useState } from "react";

type ButtonTone = "primary" | "secondary" | "danger" | "warning" | "ghost";
type ButtonSize = "sm" | "xs";

const toneClasses: Record<ButtonTone, string> = {
  primary: "border-slate-900 bg-slate-900 text-white hover:bg-slate-800",
  secondary: "border-slate-300 bg-white text-slate-800 hover:bg-slate-50",
  danger: "border-rose-300 bg-white text-rose-700 hover:bg-rose-50",
  warning: "border-amber-300 bg-white text-amber-800 hover:bg-amber-50",
  ghost: "border-transparent bg-transparent text-slate-700 hover:bg-slate-100",
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: "px-3 py-2 text-sm",
  xs: "px-2 py-1 text-xs",
};

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: ButtonTone;
  size?: ButtonSize;
};

export function Button({ tone = "secondary", size = "sm", className = "", ...props }: ButtonProps) {
  return (
    <button
      {...props}
      className={[
        "inline-flex items-center justify-center rounded-lg border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        toneClasses[tone],
        sizeClasses[size],
        className,
      ].join(" ")}
    />
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="px-1 py-1">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-950 sm:text-2xl">{title}</h1>
          {description ? <div className="mt-1.5 max-w-3xl text-sm text-slate-500">{description}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </div>
  );
}

export function Section({ children, className = "" }: PropsWithChildren<{ className?: string }>) {
  return <div className={`vr-section ${className}`}>{children}</div>;
}

export function SettingsSaveBar({
  dirty,
  busy,
  onSave,
  onDiscard,
  saveLabel = "保存配置",
  cleanLabel = "配置已与后端同步",
  dirtyLabel = "有未保存的配置修改",
  extraActions,
}: {
  dirty: boolean;
  busy: boolean;
  onSave: () => void | Promise<void>;
  onDiscard?: () => void;
  saveLabel?: string;
  cleanLabel?: string;
  dirtyLabel?: string;
  extraActions?: ReactNode;
}) {
  return (
    <div className="sticky bottom-3 z-20 rounded-xl border border-slate-200 bg-white/95 p-3 shadow-lg backdrop-blur">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm">
          <span className={["h-2 w-2 rounded-full", dirty ? "bg-amber-500" : "bg-emerald-500"].join(" ")} />
          <span className={dirty ? "text-slate-800" : "text-slate-500"}>{dirty ? dirtyLabel : cleanLabel}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {extraActions}
          {onDiscard ? <Button disabled={busy || !dirty} onClick={onDiscard}>放弃修改</Button> : null}
          <Button tone="primary" disabled={busy || !dirty} onClick={onSave}>
            {busy ? "保存中..." : saveLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

export function TableToolbar({
  title,
  description,
  filters,
  actions,
  meta,
}: {
  title?: ReactNode;
  description?: ReactNode;
  filters?: ReactNode;
  actions?: ReactNode;
  meta?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-3 border-b border-slate-200 pb-4">
      {(title || description || actions || meta) ? (
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          {(title || description || meta) ? (
            <div className="min-w-0">
              {title ? <div className="text-sm font-semibold text-slate-950">{title}</div> : null}
              {description ? <div className="mt-1 text-xs text-slate-500">{description}</div> : null}
              {meta ? <div className="mt-1 text-xs text-slate-500">{meta}</div> : null}
            </div>
          ) : null}
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      ) : null}
      {filters ? <div className="flex flex-col gap-2 lg:flex-row lg:flex-wrap lg:items-center">{filters}</div> : null}
    </div>
  );
}

export function DataTable({
  children,
  className = "",
  wrapClassName = "",
}: PropsWithChildren<{ className?: string; wrapClassName?: string }>) {
  return (
    <div className={`vr-table-wrap ${wrapClassName}`}>
      <table className={`vr-table ${className}`}>{children}</table>
    </div>
  );
}

export function EmptyState({ children }: PropsWithChildren) {
  return <div className="py-6 text-center text-sm text-slate-500">{children}</div>;
}

export function PaginationControls({
  page,
  pageSize,
  totalItems,
  currentCount,
  hasNext,
  onPrev,
  onNext,
  disabled = false,
}: {
  page: number;
  pageSize: number;
  totalItems?: number;
  currentCount: number;
  hasNext?: boolean;
  onPrev: () => void;
  onNext: () => void;
  disabled?: boolean;
}) {
  const knownTotal = typeof totalItems === "number";
  const start = currentCount === 0 ? 0 : page * pageSize + 1;
  const end = currentCount === 0 ? 0 : page * pageSize + currentCount;
  const canNext = hasNext ?? (knownTotal ? end < totalItems : currentCount >= pageSize);
  return (
    <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
      <div className="text-xs text-slate-500">
        {knownTotal ? `显示 ${start}-${end} / ${totalItems}` : `显示 ${start}-${end}`}
      </div>
      <div className="flex items-center gap-2">
        <Button size="xs" disabled={disabled || page === 0} onClick={onPrev}>
          上一页
        </Button>
        <div className="min-w-16 text-center text-xs text-slate-500">第 {page + 1} 页</div>
        <Button size="xs" disabled={disabled || !canNext} onClick={onNext}>
          下一页
        </Button>
      </div>
    </div>
  );
}

export function MoreMenu({ children, label = "更多操作" }: PropsWithChildren<{ label?: string }>) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    }
    window.addEventListener("pointerdown", onPointerDown);
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative inline-flex">
      <button
        type="button"
        className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-slate-200 bg-white text-lg leading-none text-slate-600 hover:bg-slate-50 hover:text-slate-950"
        aria-label={label}
        aria-expanded={open}
        title={label}
        onClick={() => setOpen((value) => !value)}
      >
        ...
      </button>
      {open ? (
        <div className="absolute right-0 top-full z-30 mt-1 min-w-44 overflow-hidden rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
          {children}
        </div>
      ) : null}
    </div>
  );
}

export const menuItemClass =
  "block w-full px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50";
