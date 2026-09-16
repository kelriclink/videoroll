import { Button, EmptyState } from "../../../components/ui";
import type { MediaCandidate, PlaylistItem } from "../types";
import { formatBytes, itemKey } from "../utils";

export function SelectionList({
  title,
  items,
  candidates,
  onMove,
  onRemove,
}: {
  title: string;
  items: PlaylistItem[];
  candidates: Map<string, MediaCandidate>;
  onMove: (index: number, direction: -1 | 1) => void;
  onRemove: (index: number) => void;
}) {
  return (
    <div className="mt-4 rounded-md border border-slate-200 bg-slate-50 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-sm font-medium text-slate-900">{title}</div>
        <div className="text-xs text-slate-500">{items.length} 项</div>
      </div>
      {items.length === 0 ? (
        <div className="mt-2 text-xs text-slate-500">尚未选择资源。</div>
      ) : (
        <ol className="mt-2 space-y-2">
          {items.map((item, index) => {
            const candidate = candidates.get(itemKey(item));
            return (
              <li key={itemKey(item)} className="flex items-center gap-2 rounded border border-slate-200 bg-white px-2 py-2">
                <span className="w-5 text-center text-xs font-semibold text-slate-500">{index + 1}</span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs font-medium text-slate-800">{candidate?.displayName ?? "已删除的资源"}</div>
                  <div className="truncate text-[11px] text-slate-500">{candidate?.subtitle ?? item.id}</div>
                </div>
                <div className="flex gap-1">
                  <Button size="xs" disabled={index === 0} onClick={() => onMove(index, -1)}>↑</Button>
                  <Button size="xs" disabled={index === items.length - 1} onClick={() => onMove(index, 1)}>↓</Button>
                  <Button size="xs" tone="danger" onClick={() => onRemove(index)}>移除</Button>
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

export function CandidateList({
  candidates,
  selected,
  onToggle,
  disabled,
  emptyText,
}: {
  candidates: MediaCandidate[];
  selected: PlaylistItem[];
  onToggle: (candidate: MediaCandidate, checked: boolean) => void;
  disabled: boolean;
  emptyText: string;
}) {
  const selectedKeys = new Set(selected.map(itemKey));
  if (candidates.length === 0) return <EmptyState>{emptyText}</EmptyState>;
  return (
    <div className="mt-3 max-h-72 space-y-2 overflow-auto pr-1">
      {candidates.map((candidate) => {
        const checked = selectedKeys.has(itemKey(candidate.item));
        return (
          <label key={itemKey(candidate.item)} className="flex cursor-pointer items-start gap-3 rounded-md border border-slate-200 bg-white p-3 hover:border-slate-300">
            <input
              type="checkbox"
              className="mt-1"
              checked={checked}
              disabled={disabled}
              onChange={(event) => onToggle(candidate, event.target.checked)}
            />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-slate-900">{candidate.displayName}</span>
              <span className="mt-0.5 block truncate text-xs text-slate-500">{candidate.subtitle}</span>
              <span className="mt-1 block text-[11px] text-slate-400">{candidate.sizeLabel ?? formatBytes(candidate.sizeBytes)}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}

export function ManualCandidateList({
  candidates,
  selected,
  emptyText,
  onSelect,
}: {
  candidates: MediaCandidate[];
  selected: PlaylistItem | null;
  emptyText: string;
  onSelect: (candidate: MediaCandidate) => void;
}) {
  if (candidates.length === 0) return <EmptyState>{emptyText}</EmptyState>;
  return (
    <div className="mt-3 max-h-96 space-y-2 overflow-auto pr-1">
      {candidates.map((candidate) => {
        const active = selected ? itemKey(candidate.item) === itemKey(selected) : false;
        return (
          <button
            key={itemKey(candidate.item)}
            type="button"
            onClick={() => onSelect(candidate)}
            className={`flex w-full items-start gap-3 rounded-md border p-3 text-left transition ${active ? "border-sky-400 bg-sky-50 ring-1 ring-sky-200" : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"}`}
          >
            <span className={`mt-1 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${active ? "border-sky-600 bg-sky-600 text-white" : "border-slate-300 bg-white"}`}>
              {active ? "✓" : null}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-slate-900">{candidate.displayName}</span>
              <span className="mt-0.5 block truncate text-xs text-slate-500">{candidate.subtitle}</span>
              <span className="mt-1 block text-[11px] text-slate-400">{candidate.sizeLabel ?? formatBytes(candidate.sizeBytes)}</span>
            </span>
            <span className="shrink-0 text-xs font-medium text-sky-700">{active ? "已选中" : "选择"}</span>
          </button>
        );
      })}
    </div>
  );
}
