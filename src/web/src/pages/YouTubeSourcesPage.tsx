import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { YOUTUBE_INGEST_URL } from "../lib/urls";
import { SourceLicense, YouTubeSource } from "../lib/types";

type SourceDraft = Partial<
  Pick<YouTubeSource, "display_name" | "license" | "proof_url" | "enabled" | "scan_interval_minutes" | "scan_limit" | "auto_process">
>;

type ScanResponse = {
  discovered_count: number;
  created_task_ids: string[];
  skipped_duplicates: number;
  started_pipeline_job_ids?: string[];
};

function formatTime(value?: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function currentValue<T>(source: YouTubeSource, draft: SourceDraft | undefined, key: keyof SourceDraft, fallback: T): T {
  const value = draft?.[key];
  return (value ?? ((source as unknown as Record<string, unknown>)[key as string] ?? fallback)) as T;
}

export default function YouTubeSourcesPage() {
  const [sources, setSources] = useState<YouTubeSource[] | null>(null);
  const [drafts, setDrafts] = useState<Record<string, SourceDraft>>({});
  const [busyMap, setBusyMap] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  const [sourceInputs, setSourceInputs] = useState("");
  const [license, setLicense] = useState<SourceLicense>("authorized");
  const [proofUrl, setProofUrl] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [scanIntervalMinutes, setScanIntervalMinutes] = useState(60);
  const [scanLimit, setScanLimit] = useState(20);
  const [autoProcess, setAutoProcess] = useState(true);
  const [adding, setAdding] = useState(false);

  async function refresh() {
    setError(null);
    try {
      const data = await fetchJson<YouTubeSource[]>(`${YOUTUBE_INGEST_URL}/youtube/sources`);
      setSources(data);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  function setBusy(key: string, value: boolean) {
    setBusyMap((prev) => ({ ...prev, [key]: value }));
  }

  function updateDraft(sourceId: string, patch: SourceDraft) {
    setDrafts((prev) => ({ ...prev, [sourceId]: { ...(prev[sourceId] ?? {}), ...patch } }));
  }

  async function addSources() {
    const items = Array.from(
      new Set(
        sourceInputs
          .split(/\r?\n/)
          .map((item) => item.trim())
          .filter(Boolean),
      ),
    );
    if (items.length === 0) {
      setError("请至少输入一个主页链接、@handle、频道 ID 或播放列表 ID。");
      return;
    }

    setAdding(true);
    setError(null);
    let success = 0;
    const failures: string[] = [];
    try {
      for (const item of items) {
        try {
          await fetchJson(`${YOUTUBE_INGEST_URL}/youtube/sources`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              source_url: item,
              license,
              proof_url: proofUrl.trim() ? proofUrl.trim() : null,
              enabled,
              scan_interval_minutes: scanIntervalMinutes,
              scan_limit: scanLimit,
              auto_process: autoProcess,
            }),
          });
          success += 1;
        } catch (e: unknown) {
          failures.push(`${item}: ${e instanceof Error ? e.message : String(e)}`);
        }
      }
      if (success > 0) {
        setSourceInputs("");
      }
      await refresh();
      if (failures.length > 0) {
        setError(failures.slice(0, 3).join("\n"));
      }
      alert(`已处理 ${items.length} 个输入，成功 ${success} 个，失败 ${failures.length} 个。`);
    } finally {
      setAdding(false);
    }
  }

  async function saveSource(source: YouTubeSource) {
    const draft = drafts[source.id];
    if (!draft || Object.keys(draft).length === 0) return;
    setBusy(`save:${source.id}`, true);
    setError(null);
    try {
      await fetchJson(`${YOUTUBE_INGEST_URL}/youtube/sources/${source.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draft),
      });
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[source.id];
        return next;
      });
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(`save:${source.id}`, false);
    }
  }

  async function scanSource(source: YouTubeSource) {
    const draft = drafts[source.id];
    setBusy(`scan:${source.id}`, true);
    setError(null);
    try {
      const res = await fetchJson<ScanResponse>(`${YOUTUBE_INGEST_URL}/youtube/sources/${source.id}/scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          limit: currentValue(source, draft, "scan_limit", source.scan_limit),
          auto_process: currentValue(source, draft, "auto_process", source.auto_process),
        }),
      });
      await refresh();
      alert(
        `discovered=${res.discovered_count} created=${res.created_task_ids.length} skipped=${res.skipped_duplicates} started_pipeline=${(res.started_pipeline_job_ids ?? []).length}`,
      );
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(`scan:${source.id}`, false);
    }
  }

  async function deleteSource(source: YouTubeSource) {
    if (!window.confirm(`删除订阅源？\n${source.display_name || source.source_url || source.source_id}`)) return;
    setBusy(`delete:${source.id}`, true);
    setError(null);
    try {
      await fetchJson(`${YOUTUBE_INGEST_URL}/youtube/sources/${source.id}`, {
        method: "DELETE",
      });
      setDrafts((prev) => {
        const next = { ...prev };
        delete next[source.id];
        return next;
      });
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(`delete:${source.id}`, false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between gap-4">
          <div>
            <div className="text-lg font-semibold">YouTube Sources</div>
            <div className="text-sm text-slate-600">订阅 YouTube 频道 / 播放列表，后台按间隔自动扫描，新视频可直接进入自动模式。</div>
          </div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {error ? <div className="mt-3 whitespace-pre-wrap text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">添加订阅源</div>
        <div className="mt-2 text-xs text-slate-500">
          支持一行一个：频道主页、播放列表链接、<span className="font-mono">@handle</span>、<span className="font-mono">UC...</span>、<span className="font-mono">PL...</span>。
        </div>
        <label className="mt-3 block">
          <div className="mb-1 text-xs text-slate-600">主页链接 / 来源</div>
          <textarea
            className="min-h-32 w-full rounded border px-3 py-2 text-sm"
            placeholder={"https://www.youtube.com/@creator\nhttps://www.youtube.com/channel/UC...\nhttps://www.youtube.com/playlist?list=PL..."}
            value={sourceInputs}
            onChange={(e) => setSourceInputs(e.target.value)}
          />
        </label>

        <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">license</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={license} onChange={(e) => setLicense(e.target.value as SourceLicense)}>
              <option value="own">own</option>
              <option value="authorized">authorized</option>
              <option value="cc">cc</option>
              <option value="unknown">unknown</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">proof_url（可选）</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="https://..." value={proofUrl} onChange={(e) => setProofUrl(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">扫描间隔（分钟）</div>
            <input
              type="number"
              min={1}
              max={1440}
              className="w-full rounded border px-3 py-2 text-sm"
              value={scanIntervalMinutes}
              onChange={(e) => setScanIntervalMinutes(Math.max(1, parseInt(e.target.value || "60", 10) || 60))}
            />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">每次新建上限</div>
            <input
              type="number"
              min={1}
              max={200}
              className="w-full rounded border px-3 py-2 text-sm"
              value={scanLimit}
              onChange={(e) => setScanLimit(Math.max(1, parseInt(e.target.value || "20", 10) || 20))}
            />
          </label>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-4 text-sm text-slate-700">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            启用定时扫描
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={autoProcess} onChange={(e) => setAutoProcess(e.target.checked)} />
            扫描到新视频后直接进入自动模式
          </label>
        </div>

        <div className="mt-3 text-xs text-slate-500">自动模式会继续走下载 → 字幕/翻译 → 烧录 → 投稿，具体参数按 Settings · Auto Mode。</div>

        <div className="mt-4">
          <button
            onClick={() => addSources()}
            disabled={adding}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
          >
            {adding ? "处理中..." : "添加 / 更新订阅"}
          </button>
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">已订阅 Sources</div>
        {!sources ? <div className="mt-2 text-sm text-slate-500">加载中...</div> : null}
        {sources && sources.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无订阅源。</div> : null}
        {sources ? (
          <div className="mt-3 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Creator</th>
                  <th className="py-2 pr-3">Source</th>
                  <th className="py-2 pr-3">License</th>
                  <th className="py-2 pr-3">Scan</th>
                  <th className="py-2 pr-3">Auto</th>
                  <th className="py-2 pr-3">Last Scan</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((source) => {
                  const draft = drafts[source.id];
                  const displayName = currentValue<string | null>(source, draft, "display_name", source.display_name ?? "");
                  const rowLicense = currentValue<SourceLicense>(source, draft, "license", source.license);
                  const rowProofUrl = currentValue<string | null>(source, draft, "proof_url", source.proof_url ?? "");
                  const rowEnabled = currentValue<boolean>(source, draft, "enabled", source.enabled);
                  const rowInterval = currentValue<number>(source, draft, "scan_interval_minutes", source.scan_interval_minutes);
                  const rowLimit = currentValue<number>(source, draft, "scan_limit", source.scan_limit);
                  const rowAutoProcess = currentValue<boolean>(source, draft, "auto_process", source.auto_process);
                  const isSaving = Boolean(busyMap[`save:${source.id}`]);
                  const isScanning = Boolean(busyMap[`scan:${source.id}`]);
                  const isDeleting = Boolean(busyMap[`delete:${source.id}`]);

                  return (
                    <tr key={source.id} className="border-t align-top">
                      <td className="py-3 pr-3">
                        <input
                          className="w-48 rounded border px-2 py-1 text-sm"
                          value={displayName ?? ""}
                          placeholder={source.display_name || "显示名（可选）"}
                          onChange={(e) => updateDraft(source.id, { display_name: e.target.value })}
                        />
                        <div className="mt-2 text-xs text-slate-500">{source.source_type}</div>
                      </td>
                      <td className="py-3 pr-3">
                        <a className="break-all text-xs text-sky-700 underline" href={source.source_url} target="_blank" rel="noreferrer">
                          {source.source_url}
                        </a>
                        <div className="mt-2 font-mono text-xs text-slate-500">{source.source_id}</div>
                        <input
                          className="mt-2 w-72 rounded border px-2 py-1 text-xs"
                          value={rowProofUrl ?? ""}
                          placeholder="proof_url（可选）"
                          onChange={(e) => updateDraft(source.id, { proof_url: e.target.value })}
                        />
                      </td>
                      <td className="py-3 pr-3">
                        <select className="w-32 rounded border px-2 py-1 text-sm" value={rowLicense} onChange={(e) => updateDraft(source.id, { license: e.target.value as SourceLicense })}>
                          <option value="own">own</option>
                          <option value="authorized">authorized</option>
                          <option value="cc">cc</option>
                          <option value="unknown">unknown</option>
                        </select>
                      </td>
                      <td className="py-3 pr-3">
                        <label className="flex items-center gap-2 text-xs text-slate-700">
                          <input type="checkbox" checked={rowEnabled} onChange={(e) => updateDraft(source.id, { enabled: e.target.checked })} />
                          启用
                        </label>
                        <div className="mt-2">
                          <div className="text-xs text-slate-500">间隔（分钟）</div>
                          <input
                            type="number"
                            min={1}
                            max={1440}
                            className="mt-1 w-24 rounded border px-2 py-1 text-sm"
                            value={rowInterval}
                            onChange={(e) => updateDraft(source.id, { scan_interval_minutes: Math.max(1, parseInt(e.target.value || "60", 10) || 60) })}
                          />
                        </div>
                        <div className="mt-2">
                          <div className="text-xs text-slate-500">每次新建上限</div>
                          <input
                            type="number"
                            min={1}
                            max={200}
                            className="mt-1 w-24 rounded border px-2 py-1 text-sm"
                            value={rowLimit}
                            onChange={(e) => updateDraft(source.id, { scan_limit: Math.max(1, parseInt(e.target.value || "20", 10) || 20) })}
                          />
                        </div>
                      </td>
                      <td className="py-3 pr-3">
                        <label className="flex items-center gap-2 text-xs text-slate-700">
                          <input type="checkbox" checked={rowAutoProcess} onChange={(e) => updateDraft(source.id, { auto_process: e.target.checked })} />
                          自动模式
                        </label>
                      </td>
                      <td className="py-3 pr-3">
                        <div className="text-xs text-slate-700">{source.last_scan_finished_at ? `完成：${formatTime(source.last_scan_finished_at)}` : source.last_scan_started_at ? `开始：${formatTime(source.last_scan_started_at)}` : "未扫描"}</div>
                        <div className="mt-1 text-xs text-slate-500">
                          发现 {source.last_scan_discovered_count} / 新建 {source.last_scan_created_count} / 启动 {source.last_scan_started_pipeline_count} / 跳过 {source.last_scan_skipped_duplicates}
                        </div>
                        {source.last_scan_error ? <div className="mt-1 max-w-xs break-all text-xs text-rose-700">{source.last_scan_error}</div> : null}
                      </td>
                      <td className="py-3 pr-3">
                        <div className="flex flex-col gap-2">
                          <button
                            className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                            disabled={isSaving}
                            onClick={() => saveSource(source)}
                          >
                            {isSaving ? "保存中..." : "保存"}
                          </button>
                          <button
                            className="rounded border px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-50"
                            disabled={isScanning}
                            onClick={() => scanSource(source)}
                          >
                            {isScanning ? "扫描中..." : "立即扫描"}
                          </button>
                          <button
                            className="rounded border border-rose-200 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                            disabled={isDeleting}
                            onClick={() => deleteSource(source)}
                          >
                            {isDeleting ? "删除中..." : "删除"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4 text-xs text-slate-600">
        <div>提示：后台会按每个订阅源自己的扫描间隔自动拉取新视频；新任务仍可在 Tasks 里用 status=INGESTED 查看。</div>
      </div>
    </div>
  );
}
