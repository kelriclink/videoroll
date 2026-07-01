import { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from "react";

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
        "inline-flex items-center justify-center rounded-md border font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
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
    <div className="vr-section">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-lg font-semibold text-slate-950">{title}</div>
          {description ? <div className="mt-1 text-sm text-slate-600">{description}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </div>
  );
}

export function Section({ children, className = "" }: PropsWithChildren<{ className?: string }>) {
  return <div className={`vr-section ${className}`}>{children}</div>;
}

export function DataTable({ children }: PropsWithChildren) {
  return (
    <div className="vr-table-wrap">
      <table className="vr-table">{children}</table>
    </div>
  );
}

export function EmptyState({ children }: PropsWithChildren) {
  return <div className="py-6 text-center text-sm text-slate-500">{children}</div>;
}
