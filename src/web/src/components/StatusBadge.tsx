import { TaskStatus } from "../lib/types";

const colors: Record<string, string> = {
  CREATED: "bg-slate-100 text-slate-700",
  INGESTED: "bg-sky-100 text-sky-800",
  DOWNLOADED: "bg-sky-100 text-sky-800",
  AUDIO_EXTRACTED: "bg-sky-100 text-sky-800",
  ASR_DONE: "bg-indigo-100 text-indigo-800",
  TRANSLATED: "bg-indigo-100 text-indigo-800",
  SUBTITLE_READY: "bg-indigo-100 text-indigo-800",
  RENDERED: "bg-emerald-100 text-emerald-800",
  READY_FOR_REVIEW: "bg-amber-100 text-amber-900",
  APPROVED: "bg-amber-100 text-amber-900",
  PUBLISHING: "bg-fuchsia-100 text-fuchsia-800",
  PUBLISHED: "bg-emerald-100 text-emerald-800",
  FAILED: "bg-rose-100 text-rose-800",
  CANCELED: "bg-slate-100 text-slate-700",
};

export default function StatusBadge({ status }: { status: TaskStatus | string }) {
  const cls = colors[status] ?? "bg-slate-100 text-slate-700";
  return <span className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{status}</span>;
}

