import { taskPresentation, type TaskTone } from "../lib/taskPresentation";
import type { TaskStatus } from "../lib/types";

const colors: Record<TaskTone, string> = {
  neutral: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  info: "bg-sky-100 text-sky-800 dark:bg-sky-950/50 dark:text-sky-300",
  warning: "bg-amber-100 text-amber-900 dark:bg-amber-950/50 dark:text-amber-300",
  danger: "bg-rose-100 text-rose-800 dark:bg-rose-950/50 dark:text-rose-300",
  success: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-300",
};

export default function StatusBadge({ status, compact = false }: { status: TaskStatus | string; compact?: boolean }) {
  const presentation = taskPresentation(status);
  return (
    <span
      title={presentation.label}
      className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${colors[presentation.tone]}`}
    >
      {compact ? presentation.shortLabel : presentation.label}
    </span>
  );
}
