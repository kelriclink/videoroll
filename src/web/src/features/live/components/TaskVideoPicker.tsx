import { useEffect } from "react";
import { Button } from "../../../components/ui";
import type { MediaCandidate, PlaylistItem } from "../types";
import { CandidateList } from "./LiveSelectionLists";

export function TaskVideoPicker({
  title,
  candidates,
  selected,
  onToggle,
  onApply,
  onClose,
  applying,
}: {
  title: string;
  candidates: MediaCandidate[];
  selected: PlaylistItem[];
  onToggle: (candidate: MediaCandidate, checked: boolean) => void;
  onApply: () => void;
  onClose: () => void;
  applying: boolean;
}) {
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[calc(100vh-2rem)] w-full max-w-3xl flex-col overflow-hidden rounded-md border border-slate-200 bg-white shadow-xl">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
          <div>
            <div className="text-base font-semibold text-slate-950">{title}</div>
            <div className="mt-0.5 text-xs text-slate-500">最近 {candidates.length} 个任务资源</div>
          </div>
          <button type="button" disabled={applying} className="h-8 w-8 rounded-md border border-slate-300 text-lg leading-none text-slate-600 hover:bg-slate-50 disabled:opacity-50" aria-label="关闭" title="关闭" onClick={onClose}>x</button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto px-4 py-2">
          <CandidateList candidates={candidates} selected={selected} disabled={applying} emptyText="最近的任务中没有这类视频资源。" onToggle={onToggle} />
        </div>
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-slate-200 px-4 py-3">
          <div className="text-xs text-slate-500">已选择 {selected.length} 项</div>
          <div className="flex gap-2">
            <Button disabled={applying} onClick={onClose}>取消</Button>
            <Button tone="primary" disabled={applying || selected.length === 0} onClick={onApply}>{applying ? "正在复制..." : "导入并选择"}</Button>
          </div>
        </div>
      </div>
    </div>
  );
}
