import { useEffect, useMemo, useState } from "react";
import type {
  TranslationContextAmbiguity,
  TranslationContextCharacter,
  TranslationContextMemory,
  TranslationContextTerm,
} from "../../../api/subtitle";
import type { TaskDetailController } from "../useTaskDetailController";

function emptyMemory(): TranslationContextMemory {
  return {
    topic: "",
    style_notes: "",
    characters: [],
    terminology: [],
    ambiguities: [],
  };
}

function cloneMemory(memory: TranslationContextMemory | undefined): TranslationContextMemory {
  if (!memory) return emptyMemory();
  return {
    version: memory.version,
    topic: memory.topic || "",
    style_notes: memory.style_notes || "",
    characters: (memory.characters || []).map((item) => ({
      ...item,
      aliases: [...(item.aliases || [])],
    })),
    terminology: (memory.terminology || []).map((item) => ({ ...item })),
    ambiguities: (memory.ambiguities || []).map((item) => ({ ...item })),
    recent_scene: memory.recent_scene ? { ...memory.recent_scene } : undefined,
  };
}

function metricValue(metrics: Record<string, number> | undefined, key: string) {
  const value = metrics?.[key];
  return typeof value === "number" ? value : 0;
}

export function SubtitleQualityContextPanel({ controller }: { controller: TaskDetailController }) {
  const {
    busy,
    subtitleQuality,
    translationContext,
    subtitleReviewLoading,
    refreshSubtitleReview,
    saveTranslationContext,
    retranslateAffected,
  } = controller;
  const [summary, setSummary] = useState("");
  const [memory, setMemory] = useState<TranslationContextMemory>(emptyMemory);
  const [affectedIndices, setAffectedIndices] = useState<number[]>([]);

  useEffect(() => {
    if (!translationContext) return;
    setSummary(translationContext.summary || "");
    setMemory(cloneMemory(translationContext.memory));
    setAffectedIndices(translationContext.affected_indices || []);
  }, [translationContext]);

  const report = subtitleQuality?.report ?? null;
  const metrics = report?.metrics;
  const issuePreview = useMemo(() => (report?.issues ?? []).slice(0, 12), [report]);

  async function save() {
    const result = await saveTranslationContext(summary, memory);
    if (result) setAffectedIndices(result.affected_indices || []);
  }

  function updateCharacter(index: number, patch: Partial<TranslationContextCharacter>) {
    setMemory((current) => ({
      ...current,
      characters: current.characters.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    }));
  }

  function updateTerm(index: number, patch: Partial<TranslationContextTerm>) {
    setMemory((current) => ({
      ...current,
      terminology: current.terminology.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    }));
  }

  function updateAmbiguity(index: number, patch: Partial<TranslationContextAmbiguity>) {
    setMemory((current) => ({
      ...current,
      ambiguities: current.ambiguities.map((item, itemIndex) =>
        itemIndex === index ? { ...item, ...patch } : item,
      ),
    }));
  }

  const scoreTone =
    (report?.score ?? 100) >= 90
      ? "text-emerald-700"
      : (report?.score ?? 100) >= 75
        ? "text-amber-700"
        : "text-rose-700";

  return (
    <div className="mt-5 grid gap-4 xl:grid-cols-2">
      <div className="rounded border bg-slate-50/50 p-4 dark:bg-slate-900/20">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-sm font-semibold">字幕质量分析</div>
            <div className="mt-1 text-xs text-slate-500">CPS、时间轴、显示时长、术语一致性和当前自适应阈值。</div>
          </div>
          <button
            type="button"
            disabled={subtitleReviewLoading}
            onClick={() => void refreshSubtitleReview()}
            className="rounded border px-3 py-1.5 text-xs hover:bg-white disabled:opacity-50"
          >
            {subtitleReviewLoading ? "刷新中…" : "刷新报告"}
          </button>
        </div>

        {!report ? (
          <div className="mt-4 rounded border border-dashed p-4 text-sm text-slate-500">
            当前任务还没有质量报告。新版字幕任务完成后会自动生成。
          </div>
        ) : (
          <>
            <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-4">
              <div className="rounded border bg-white p-3">
                <div className="text-xs text-slate-500">Quality</div>
                <div className={`mt-1 text-2xl font-semibold ${scoreTone}`}>{report.score}</div>
              </div>
              <div className="rounded border bg-white p-3">
                <div className="text-xs text-slate-500">CPS P95</div>
                <div className="mt-1 text-lg font-semibold">{metricValue(metrics, "cps_p95").toFixed(1)}</div>
              </div>
              <div className="rounded border bg-white p-3">
                <div className="text-xs text-slate-500">高 CPS</div>
                <div className="mt-1 text-lg font-semibold">{metricValue(metrics, "high_cps_count")}</div>
              </div>
              <div className="rounded border bg-white p-3">
                <div className="text-xs text-slate-500">时间轴重叠</div>
                <div className="mt-1 text-lg font-semibold">{metricValue(metrics, "overlap_count")}</div>
              </div>
            </div>

            <div className="mt-3 grid gap-2 text-xs md:grid-cols-2">
              <div className="rounded border bg-white p-3">
                <div className="font-medium text-slate-700">Readability profile</div>
                <div className="mt-1 text-slate-500">
                  target CPS {report.adaptive_profile.readability.target_cps ?? "—"} · line units{" "}
                  {report.adaptive_profile.readability.max_line_units ?? "—"} · min display{" "}
                  {report.adaptive_profile.readability.min_display_seconds ?? "—"}s
                </div>
              </div>
              <div className="rounded border bg-white p-3">
                <div className="font-medium text-slate-700">Scene profile</div>
                <div className="mt-1 text-slate-500">
                  soft gap {report.adaptive_profile.scene.soft_gap_seconds ?? "—"}s · hard gap{" "}
                  {report.adaptive_profile.scene.hard_gap_seconds ?? "—"}s
                </div>
              </div>
            </div>

            <div className="mt-3">
              <div className="text-xs font-semibold text-slate-700">主要问题</div>
              {issuePreview.length === 0 ? (
                <div className="mt-2 text-sm text-emerald-700">没有检测到显著字幕质量问题。</div>
              ) : (
                <div className="mt-2 max-h-52 overflow-auto rounded border bg-white">
                  {issuePreview.map((issue, index) => (
                    <div key={index} className="border-b px-3 py-2 text-xs last:border-b-0">
                      <span className="font-mono text-slate-500">#{String(issue.idx ?? "—")}</span>{" "}
                      <span className="font-medium">{String(issue.type ?? "issue")}</span>
                      {issue.value !== undefined ? (
                        <span className="ml-2 text-slate-500">value={String(issue.value)}</span>
                      ) : null}
                      {issue.expected !== undefined ? (
                        <span className="ml-2 text-slate-500">expected={String(issue.expected)}</span>
                      ) : null}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}
      </div>

      <div className="rounded border bg-slate-50/50 p-4 dark:bg-slate-900/20">
        <div>
          <div className="text-sm font-semibold">翻译上下文审查</div>
          <div className="mt-1 text-xs text-slate-500">
            修改人物译名、术语和歧义后，系统会计算真正受影响的源字幕，只重翻这些 idx。
          </div>
        </div>

        {!translationContext?.available ? (
          <div className="mt-4 rounded border border-dashed p-4 text-sm text-slate-500">
            当前任务还没有结构化翻译上下文。先用新版翻译流水线完整跑一次。
          </div>
        ) : (
          <div className="mt-4 space-y-4">
            <label className="block">
              <div className="mb-1 text-xs font-medium text-slate-600">Topic</div>
              <input
                value={memory.topic}
                onChange={(event) => setMemory((current) => ({ ...current, topic: event.target.value }))}
                className="w-full rounded border bg-white px-3 py-2 text-sm"
              />
            </label>
            <label className="block">
              <div className="mb-1 text-xs font-medium text-slate-600">Style notes</div>
              <textarea
                value={memory.style_notes}
                onChange={(event) => setMemory((current) => ({ ...current, style_notes: event.target.value }))}
                rows={2}
                className="w-full rounded border bg-white px-3 py-2 text-sm"
              />
            </label>
            <label className="block">
              <div className="mb-1 text-xs font-medium text-slate-600">Rolling summary</div>
              <textarea
                value={summary}
                onChange={(event) => setSummary(event.target.value)}
                rows={2}
                className="w-full rounded border bg-white px-3 py-2 text-sm"
              />
            </label>

            <div>
              <div className="flex items-center justify-between">
                <div className="text-xs font-semibold text-slate-700">人物</div>
                <button
                  type="button"
                  className="text-xs text-slate-600 underline"
                  onClick={() =>
                    setMemory((current) => ({
                      ...current,
                      characters: [...current.characters, { name: "", target_name: "", aliases: [] }],
                    }))
                  }
                >
                  添加
                </button>
              </div>
              <div className="mt-2 space-y-2">
                {memory.characters.map((item, index) => (
                  <div key={index} className="grid gap-2 rounded border bg-white p-2 md:grid-cols-[1fr_1fr_auto]">
                    <input
                      value={item.name}
                      placeholder="Source name"
                      onChange={(event) => updateCharacter(index, { name: event.target.value })}
                      className="rounded border px-2 py-1.5 text-sm"
                    />
                    <input
                      value={item.target_name}
                      placeholder="目标译名"
                      onChange={(event) => updateCharacter(index, { target_name: event.target.value })}
                      className="rounded border px-2 py-1.5 text-sm"
                    />
                    <button
                      type="button"
                      className="px-2 text-xs text-rose-600"
                      onClick={() =>
                        setMemory((current) => ({
                          ...current,
                          characters: current.characters.filter((_, itemIndex) => itemIndex !== index),
                        }))
                      }
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between">
                <div className="text-xs font-semibold text-slate-700">术语</div>
                <button
                  type="button"
                  className="text-xs text-slate-600 underline"
                  onClick={() =>
                    setMemory((current) => ({
                      ...current,
                      terminology: [...current.terminology, { source: "", target: "", meaning: "" }],
                    }))
                  }
                >
                  添加
                </button>
              </div>
              <div className="mt-2 space-y-2">
                {memory.terminology.map((item, index) => (
                  <div key={index} className="grid gap-2 rounded border bg-white p-2 md:grid-cols-[1fr_1fr_auto]">
                    <input
                      value={item.source}
                      placeholder="Source term"
                      onChange={(event) => updateTerm(index, { source: event.target.value })}
                      className="rounded border px-2 py-1.5 text-sm"
                    />
                    <input
                      value={item.target}
                      placeholder="目标术语"
                      onChange={(event) => updateTerm(index, { target: event.target.value })}
                      className="rounded border px-2 py-1.5 text-sm"
                    />
                    <button
                      type="button"
                      className="px-2 text-xs text-rose-600"
                      onClick={() =>
                        setMemory((current) => ({
                          ...current,
                          terminology: current.terminology.filter((_, itemIndex) => itemIndex !== index),
                        }))
                      }
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between">
                <div className="text-xs font-semibold text-slate-700">歧义</div>
                <button
                  type="button"
                  className="text-xs text-slate-600 underline"
                  onClick={() =>
                    setMemory((current) => ({
                      ...current,
                      ambiguities: [...current.ambiguities, { term: "", resolution: "" }],
                    }))
                  }
                >
                  添加
                </button>
              </div>
              <div className="mt-2 space-y-2">
                {memory.ambiguities.map((item, index) => (
                  <div key={index} className="grid gap-2 rounded border bg-white p-2 md:grid-cols-[1fr_1fr_auto]">
                    <input
                      value={item.term}
                      placeholder="歧义词"
                      onChange={(event) => updateAmbiguity(index, { term: event.target.value })}
                      className="rounded border px-2 py-1.5 text-sm"
                    />
                    <input
                      value={item.resolution}
                      placeholder="当前语境中的确定含义"
                      onChange={(event) => updateAmbiguity(index, { resolution: event.target.value })}
                      className="rounded border px-2 py-1.5 text-sm"
                    />
                    <button
                      type="button"
                      className="px-2 text-xs text-rose-600"
                      onClick={() =>
                        setMemory((current) => ({
                          ...current,
                          ambiguities: current.ambiguities.filter((_, itemIndex) => itemIndex !== index),
                        }))
                      }
                    >
                      删除
                    </button>
                  </div>
                ))}
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => void save()}
                className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
              >
                保存上下文并分析影响
              </button>
              {affectedIndices.length > 0 ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void retranslateAffected(affectedIndices)}
                  className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-900 hover:bg-amber-100 disabled:opacity-50"
                >
                  只重翻受影响的 {affectedIndices.length} 条
                </button>
              ) : null}
              {affectedIndices.length === 0 ? (
                <span className="text-xs text-slate-500">保存后若影响现有源字幕，这里会出现选择性重翻按钮。</span>
              ) : null}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
