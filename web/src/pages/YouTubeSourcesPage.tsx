import { useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL, YOUTUBE_INGEST_URL } from "../lib/urls";
import { SourceLicense, YouTubeSource } from "../lib/types";

export default function YouTubeSourcesPage() {
  const [sources, setSources] = useState<YouTubeSource[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [sourceType, setSourceType] = useState<"channel" | "playlist">("channel");
  const [sourceId, setSourceId] = useState("");
  const [license, setLicense] = useState<SourceLicense>("own");
  const [proofUrl, setProofUrl] = useState("");
  const [scanLimit, setScanLimit] = useState(20);
  const [autoProcess, setAutoProcess] = useState(true);

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

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-lg font-semibold">YouTube Sources</div>
            <div className="text-sm text-slate-600">白名单源管理（channel / playlist）</div>
          </div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">添加白名单源</div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">source_type</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={sourceType} onChange={(e) => setSourceType(e.target.value as any)}>
              <option value="channel">channel</option>
              <option value="playlist">playlist</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">source_id</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="UC..." value={sourceId} onChange={(e) => setSourceId(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">license</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={license} onChange={(e) => setLicense(e.target.value as any)}>
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
        </div>

        <div className="mt-3">
          <button
            onClick={async () => {
              setError(null);
              try {
                await fetchJson(`${YOUTUBE_INGEST_URL}/youtube/sources`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    source_type: sourceType,
                    source_id: sourceId.trim(),
                    license,
                    proof_url: proofUrl.trim() ? proofUrl.trim() : null,
                    enabled: true,
                  }),
                });
                setSourceId("");
                setProofUrl("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              }
            }}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800"
          >
            添加
          </button>
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">扫描（RSS）并创建任务</div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            type="number"
            className="w-28 rounded border px-3 py-2 text-sm"
            value={scanLimit}
            min={1}
            max={200}
            onChange={(e) => setScanLimit(parseInt(e.target.value || "20", 10))}
          />
          <span className="text-xs text-slate-600">limit</span>
          <label className="ml-3 flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={autoProcess} onChange={(e) => setAutoProcess(e.target.checked)} />
            扫描到新视频后进入自动模式
          </label>
        </div>
        <div className="mt-3 text-xs text-slate-500">
          说明：扫描会发现新视频并创建任务；勾选“自动模式”后，会自动触发下载→字幕/翻译→烧录→投稿（按 Settings · Auto Mode）。
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">Sources</div>
        {!sources ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {sources && sources.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
        {sources ? (
          <div className="mt-2 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Type</th>
                  <th className="py-2 pr-3">ID</th>
                  <th className="py-2 pr-3">License</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.id} className="border-t">
                    <td className="py-2 pr-3 text-xs">{s.source_type}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{s.source_id}</td>
                    <td className="py-2 pr-3 text-xs">{s.license}</td>
                    <td className="py-2 pr-3">
                      <button
                        className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                        onClick={async () => {
                          setError(null);
                          try {
                            const res = await fetchJson<{
                              created_task_ids: string[];
                              discovered_count: number;
                              skipped_duplicates: number;
                              started_pipeline_job_ids?: string[];
                            }>(`${YOUTUBE_INGEST_URL}/youtube/scan`, {
                              method: "POST",
                              headers: { "Content-Type": "application/json" },
                              body: JSON.stringify({
                                source_type: s.source_type,
                                source_id: s.source_id,
                                since: null,
                                limit: scanLimit,
                                auto_process: autoProcess,
                              }),
                            });
                            const started = (res.started_pipeline_job_ids ?? []).length;
                            alert(
                              `discovered=${res.discovered_count} created=${res.created_task_ids.length} skipped=${res.skipped_duplicates}` +
                                (autoProcess ? ` started_pipeline=${started}` : ""),
                            );
                          } catch (e: unknown) {
                            setError(e instanceof Error ? e.message : String(e));
                          }
                        }}
                      >
                        Scan
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4 text-xs text-slate-600">
        <div>提示：扫描创建的任务可在 Tasks 列表里用 status=INGESTED 过滤查看。</div>
        <div className="mt-1">Orchestrator：{ORCHESTRATOR_URL}</div>
      </div>
    </div>
  );
}
