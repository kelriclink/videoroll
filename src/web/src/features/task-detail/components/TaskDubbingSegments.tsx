import { useCallback, useEffect, useState } from "react";
import { tasksApi } from "../../../api/tasks";
import type { DubbingJob } from "../../../lib/types";

type Segment = {
  index: number;
  start: number;
  end: number;
  subtitle_text: string;
  speech_text: string;
  overridden: boolean;
  has_audio: boolean;
};

export function TaskDubbingSegments({
  taskId, job, reload,
}: {
  taskId: string;
  job: DubbingJob;
  reload: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [edits, setEdits] = useState<Record<number, string>>({});
  const [savingIndex, setSavingIndex] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const editable = job.status === "failed" || job.status === "queued";

  const fetchSegments = useCallback(async () => {
    try {
      const result = await tasksApi.dubbingSegments(taskId, job.id, offset);
      setSegments(result.items);
      setTotal(result.total);
      setEdits(Object.fromEntries(result.items.map((item) => [item.index, item.speech_text])));
      setError(null);
    } catch (reason) {
      setError(String(reason));
    }
  }, [taskId, job.id, offset]);

  useEffect(() => {
    if (open) void fetchSegments();
  }, [open, fetchSegments]);

  async function saveSegment(item: Segment) {
    const speechText = edits[item.index]?.trim() ?? "";
    if (!speechText || speechText === item.speech_text) return;
    setSavingIndex(item.index);
    setError(null);
    try {
      await tasksApi.updateDubbingSpeech(taskId, job.id, item.index, speechText);
      await fetchSegments();
      await reload();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSavingIndex(null);
    }
  }

  return (
    <div className="mt-3 border-t pt-3">
      <button className="rounded border px-2 py-1 text-xs" type="button"
        onClick={() => setOpen((value) => !value)}>
        {open ? "收起口播片段" : "试听 / 编辑口播片段"}
      </button>
      {open ? (
        <div className="mt-3 space-y-3">
          <p className="text-xs text-slate-500">
            仅修改口播文案，原中文字幕不会变化。过长的语音请改写为简短表述后使用“断点重试”。
          </p>
          {error ? <p className="text-xs text-rose-700">{error}</p> : null}
          {segments.map((item) => (
            <div key={item.index} className="rounded bg-slate-50 p-3 text-xs">
              <div className="font-medium">第 {item.index} 句 · {item.start.toFixed(2)}–{item.end.toFixed(2)} 秒</div>
              <p className="mt-1 text-slate-600">字幕：{item.subtitle_text}</p>
              <textarea
                aria-label={`第 ${item.index} 句口播文案`}
                className="mt-2 w-full rounded border bg-white p-2 text-sm"
                rows={2}
                maxLength={1000}
                disabled={!editable || savingIndex !== null}
                value={edits[item.index] ?? item.speech_text}
                onChange={(e) => setEdits((before) => ({ ...before, [item.index]: e.target.value }))}
              />
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {item.has_audio ? (
                  <audio controls preload="none" src={tasksApi.dubbingPreviewUrl(taskId, job.id, item.index)} />
                ) : <span className="text-slate-500">尚未生成该句音频</span>}
                {editable ? (
                  <button type="button" className="rounded border px-2 py-1"
                    disabled={savingIndex !== null || !edits[item.index]?.trim()
                      || edits[item.index]?.trim() === item.speech_text}
                    onClick={() => void saveSegment(item)}>
                    {savingIndex === item.index ? "保存中…" : "保存口播修改"}
                  </button>
                ) : null}
              </div>
            </div>
          ))}
          {total > 30 ? (
            <div className="flex items-center gap-3 text-xs">
              <button type="button" className="rounded border px-2 py-1"
                disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 30))}>上一页</button>
              <span>{offset + 1}–{Math.min(offset + 30, total)} / {total}</span>
              <button type="button" className="rounded border px-2 py-1"
                disabled={offset + 30 >= total} onClick={() => setOffset(offset + 30)}>下一页</button>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
