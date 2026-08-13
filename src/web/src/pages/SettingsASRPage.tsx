import { useEffect, useMemo, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type WhisperSettings = {
  asr_engine: string;
  whisper_model: string;
  whisper_model_dir: string;
  whisper_device: string;
  whisper_compute_type: string;
  openvino_model: string;
  openvino_device: string;
  openvino_num_beams: number;
  openvino_max_new_tokens: number;
  openvino_vad_enabled: boolean;
  openvino_vad_threshold: number;
  whisper_cpu_threads: number;
  whisper_num_workers: number;
  whisper_cpu_threads_effective: number;
  whisper_num_workers_effective: number;
  faster_whisper_installed: boolean;
  openvino_installed: boolean;
  external_whisper_base_url: string;
  external_whisper_model: string;
  external_whisper_api_key_set: boolean;
  groq_whisper_model: string;
  groq_whisper_api_key_set: boolean;
  cloudflare_workers_ai_account_id: string;
  cloudflare_workers_ai_model: string;
  cloudflare_workers_ai_api_key_set: boolean;
};

type ASRDefaults = {
  default_engine: string;
  default_language: string;
  default_model: string;
  openvino_device: string;
  openvino_num_beams: number;
  openvino_max_new_tokens: number;
  openvino_vad_enabled: boolean;
  openvino_vad_threshold: number;
  model_download_proxy?: string;
  external_whisper_base_url: string;
  external_whisper_model: string;
  external_whisper_api_key_set: boolean;
  groq_whisper_model: string;
  groq_whisper_api_key_set: boolean;
  cloudflare_workers_ai_account_id: string;
  cloudflare_workers_ai_model: string;
  cloudflare_workers_ai_api_key_set: boolean;
};

type ExternalWhisperTestResponse = {
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  text: string;
  error?: string | null;
};

type CloudflareWorkersAITestResponse = {
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  text: string;
  segments: number;
  error?: string | null;
};

type GroqWhisperTestResponse = {
  ok: boolean;
  status_code?: number | null;
  elapsed_ms: number;
  text: string;
  segments: number;
  error?: string | null;
};

type WhisperModelInfo = {
  name: string;
  path: string;
  size_bytes?: number | null;
};

type ModelProxyTestResponse = {
  ok: boolean;
  url: string;
  used_proxy?: string | null;
  status_code?: number | null;
  elapsed_ms: number;
  error?: string | null;
};

function formatBytes(n?: number | null): string {
  if (!n || n <= 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u++;
  }
  return `${v.toFixed(u === 0 ? 0 : 1)} ${units[u]}`;
}

export default function SettingsASRPage() {
  const confirm = useConfirm();
  const [settings, setSettings] = useState<WhisperSettings | null>(null);
  const [asrDefaults, setAsrDefaults] = useState<ASRDefaults | null>(null);
  const [models, setModels] = useState<WhisperModelInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [downloadModel, setDownloadModel] = useState("tiny");
  const [downloadEngine, setDownloadEngine] = useState("faster-whisper");
  const [downloadName, setDownloadName] = useState("");
  const [downloadRevision, setDownloadRevision] = useState("");
  const [downloadForce, setDownloadForce] = useState(false);

  const [uploadName, setUploadName] = useState("");
  const [uploadFile, setUploadFile] = useState<File | null>(null);

  const [defaultEngine, setDefaultEngine] = useState("faster-whisper");
  const [defaultLanguage, setDefaultLanguage] = useState("auto");
  const [defaultModel, setDefaultModel] = useState("");
  const [openvinoDevice, setOpenvinoDevice] = useState("GPU");
  const [openvinoNumBeams, setOpenvinoNumBeams] = useState("1");
  const [openvinoMaxNewTokens, setOpenvinoMaxNewTokens] = useState("448");
  const [openvinoVadEnabled, setOpenvinoVadEnabled] = useState(true);
  const [openvinoVadThreshold, setOpenvinoVadThreshold] = useState("0.5");
  const [modelDownloadProxy, setModelDownloadProxy] = useState("");
  const [proxyTestBusy, setProxyTestBusy] = useState(false);
  const [proxyTestResult, setProxyTestResult] = useState<ModelProxyTestResponse | null>(null);
  const [externalWhisperBaseUrl, setExternalWhisperBaseUrl] = useState("");
  const [externalWhisperModel, setExternalWhisperModel] = useState("whisper-1");
  const [externalWhisperApiKey, setExternalWhisperApiKey] = useState("");
  const [externalWhisperTestBusy, setExternalWhisperTestBusy] = useState(false);
  const [externalWhisperTestResult, setExternalWhisperTestResult] = useState<ExternalWhisperTestResponse | null>(null);
  const [groqWhisperModel, setGroqWhisperModel] = useState("whisper-large-v3-turbo");
  const [groqWhisperApiKey, setGroqWhisperApiKey] = useState("");
  const [groqWhisperTestBusy, setGroqWhisperTestBusy] = useState(false);
  const [groqWhisperTestResult, setGroqWhisperTestResult] = useState<GroqWhisperTestResponse | null>(null);
  const [cloudflareAccountId, setCloudflareAccountId] = useState("");
  const [cloudflareModel, setCloudflareModel] = useState("@cf/openai/whisper-large-v3-turbo");
  const [cloudflareApiKey, setCloudflareApiKey] = useState("");
  const [cloudflareTestBusy, setCloudflareTestBusy] = useState(false);
  const [cloudflareTestResult, setCloudflareTestResult] = useState<CloudflareWorkersAITestResponse | null>(null);

  async function refresh() {
    setError(null);
    try {
      const [s, m, a] = await Promise.all([
        fetchJson<WhisperSettings>(`${ORCHESTRATOR_URL}/subtitle/settings`),
        fetchJson<WhisperModelInfo[]>(`${ORCHESTRATOR_URL}/subtitle/models`),
        fetchJson<ASRDefaults>(`${ORCHESTRATOR_URL}/subtitle/asr/settings`),
      ]);
      setSettings(s);
      setModels(m);
      setAsrDefaults(a);
      if (a.default_engine) setDefaultEngine(a.default_engine);
      if (a.default_language) setDefaultLanguage(a.default_language);
      if (typeof a.default_model === "string") setDefaultModel(a.default_model);
      if (typeof a.openvino_device === "string" && a.openvino_device.trim()) setOpenvinoDevice(a.openvino_device);
      if (typeof a.openvino_num_beams === "number" && a.openvino_num_beams > 0) setOpenvinoNumBeams(String(a.openvino_num_beams));
      if (typeof a.openvino_max_new_tokens === "number" && a.openvino_max_new_tokens > 0) setOpenvinoMaxNewTokens(String(a.openvino_max_new_tokens));
      if (typeof a.openvino_vad_enabled === "boolean") setOpenvinoVadEnabled(a.openvino_vad_enabled);
      if (typeof a.openvino_vad_threshold === "number") setOpenvinoVadThreshold(String(a.openvino_vad_threshold));
      if (typeof a.model_download_proxy === "string") setModelDownloadProxy(a.model_download_proxy);
      if (typeof a.external_whisper_base_url === "string") setExternalWhisperBaseUrl(a.external_whisper_base_url);
      if (typeof a.external_whisper_model === "string" && a.external_whisper_model.trim()) setExternalWhisperModel(a.external_whisper_model);
      if (typeof a.groq_whisper_model === "string" && a.groq_whisper_model.trim()) setGroqWhisperModel(a.groq_whisper_model);
      if (typeof a.cloudflare_workers_ai_account_id === "string") setCloudflareAccountId(a.cloudflare_workers_ai_account_id);
      if (typeof a.cloudflare_workers_ai_model === "string" && a.cloudflare_workers_ai_model.trim()) setCloudflareModel(a.cloudflare_workers_ai_model);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const knownSizes = useMemo(() => ["tiny", "base", "small", "medium", "large-v3"], []);

  return (
    <div className="space-y-4">
      <div className="rounded border bg-white p-4">
        <div className="text-lg font-semibold">Settings · ASR / Whisper / OpenVINO</div>
        <div className="mt-1 text-sm text-slate-600">管理默认 ASR 引擎、模型路径与 OpenVINO 参数；本地模型目录也可用于上传 OpenVINO Whisper 导出模型。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">当前配置（来自后端环境变量）</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_ASR_ENGINE</div>
              <div className="mt-1 font-mono text-sm">{settings.asr_engine}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">faster-whisper</div>
              <div className="mt-1 text-sm">{settings.faster_whisper_installed ? "installed" : "not installed"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">openvino-genai</div>
              <div className="mt-1 text-sm">{settings.openvino_installed ? "installed" : "not installed"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_WHISPER_MODEL</div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_model}</div>
            </div>
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">SUBTITLE_WHISPER_MODEL_DIR（ASR 模型目录）</div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_model_dir}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">device</div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_device}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">compute_type</div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_compute_type}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_WHISPER_CPU_THREADS</div>
              <div className="mt-1 font-mono text-sm">
                {settings.whisper_cpu_threads}{" "}
                {settings.whisper_cpu_threads !== settings.whisper_cpu_threads_effective ? `(effective: ${settings.whisper_cpu_threads_effective})` : null}
              </div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_WHISPER_NUM_WORKERS</div>
              <div className="mt-1 font-mono text-sm">
                {settings.whisper_num_workers}{" "}
                {settings.whisper_num_workers !== settings.whisper_num_workers_effective ? `(effective: ${settings.whisper_num_workers_effective})` : null}
              </div>
            </div>
            <div className="rounded border p-3 md:col-span-2">
              <div className="text-xs text-slate-500">SUBTITLE_OPENVINO_MODEL</div>
              <div className="mt-1 font-mono text-sm break-all">{settings.openvino_model || "(empty)"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_OPENVINO_DEVICE</div>
              <div className="mt-1 font-mono text-sm">{settings.openvino_device}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_OPENVINO_NUM_BEAMS</div>
              <div className="mt-1 font-mono text-sm">{settings.openvino_num_beams}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">SUBTITLE_OPENVINO_MAX_NEW_TOKENS</div>
              <div className="mt-1 font-mono text-sm">{settings.openvino_max_new_tokens}</div>
            </div>
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">
          提示：`faster-whisper` 和 `openvino-genai` 都依赖 `INSTALL_ASR=1` 构建。要启用方案 2，请把默认引擎切到 `openvino`，并提供一个已导出的 OpenVINO Whisper 模型目录。
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">默认 ASR 参数（存储在数据库）</div>
        <div className="mt-2 text-xs text-slate-500">
          当任务中选择 <span className="font-mono">engine=auto</span> / <span className="font-mono">language=auto</span> / 未指定 model 时，会使用这里的默认值。
        </div>

        {!asrDefaults ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}

        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_engine</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultEngine} onChange={(e) => setDefaultEngine(e.target.value)}>
              <option value="faster-whisper">faster-whisper</option>
              <option value="openvino">openvino（方案2 / Intel Arc）</option>
              <option value="external-whisper">external-whisper（外部 API）</option>
              <option value="groq-whisper">groq-whisper（GroqCloud，自动切片）</option>
              <option value="cloudflare-workers-ai">cloudflare-workers-ai（原生时间轴）</option>
              <option value="mock">mock</option>
            </select>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">default_language</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={defaultLanguage} onChange={(e) => setDefaultLanguage(e.target.value)} placeholder="auto / zh / en ..." />
          </label>

          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">default_model（size/repo id/本地路径）</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              value={defaultModel}
              onChange={(e) => setDefaultModel(e.target.value)}
              placeholder="例如 tiny / Systran/faster-whisper-small / /models/whisper/whisper-large-v3-ov"
            />
            <div className="mt-2 flex flex-wrap gap-2">
              {knownSizes.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                  onClick={() => setDefaultModel(s)}
                >
                  {s}
                </button>
              ))}
              <button type="button" className="rounded border px-2 py-1 text-xs hover:bg-slate-50" onClick={() => setDefaultModel("")}>
                (use env default)
              </button>
            </div>
            {models && models.length > 0 ? (
              <div className="mt-2">
                <select
                  className="w-full rounded border px-3 py-2 text-sm"
                  value=""
                  onChange={(e) => setDefaultModel(e.target.value)}
                >
                  <option value="">选择本地模型目录…</option>
                  {models.map((m) => (
                    <option key={m.name} value={m.path}>
                      {m.name} · {m.path}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
            <div className="mt-2 text-xs text-slate-500">
              `faster-whisper` 可用 size/repo id/本地路径；`openvino` 需要填写一个已导出的 OpenVINO Whisper 模型目录路径；外部 API 模式使用下方配置的模型。
            </div>
          </label>

          {defaultEngine === "external-whisper" ? (
            <div className="rounded border border-amber-100 bg-amber-50/60 p-3 md:col-span-2">
              <div className="text-sm font-medium text-slate-800">外部 Whisper API（OpenAI 兼容接口）</div>
              <div className="mt-1 text-xs text-slate-600">请求地址应为服务的 base URL，例如 `https://api.openai.com/v1`；后端会调用 `/audio/transcriptions`。</div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <label className="block md:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">external_whisper_base_url</div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={externalWhisperBaseUrl} onChange={(e) => setExternalWhisperBaseUrl(e.target.value)} placeholder="https://api.openai.com/v1" />
                </label>
                <label className="block md:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">external_whisper_api_key（仅保存，不回显）</div>
                  <input type="password" className="w-full rounded border px-3 py-2 text-sm" value={externalWhisperApiKey} onChange={(e) => setExternalWhisperApiKey(e.target.value)} placeholder={asrDefaults?.external_whisper_api_key_set ? "已设置（留空则不修改）" : "API key"} />
                </label>
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">external_whisper_model</div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={externalWhisperModel} onChange={(e) => setExternalWhisperModel(e.target.value)} placeholder="whisper-1" />
                </label>
                <div className="flex items-end">
                  <button
                    type="button"
                    disabled={externalWhisperTestBusy}
                    className="rounded border px-3 py-2 text-sm hover:bg-white disabled:opacity-50"
                    onClick={async () => {
                      setExternalWhisperTestBusy(true);
                      setExternalWhisperTestResult(null);
                      setError(null);
                      try {
                        const result = await fetchJson<ExternalWhisperTestResponse>(`${ORCHESTRATOR_URL}/subtitle/asr/external/test`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ base_url: externalWhisperBaseUrl, api_key: externalWhisperApiKey, model: externalWhisperModel }),
                        });
                        setExternalWhisperTestResult(result);
                      } catch (e: unknown) {
                        setExternalWhisperTestResult({ ok: false, elapsed_ms: 0, text: "", error: e instanceof Error ? e.message : String(e) });
                      } finally {
                        setExternalWhisperTestBusy(false);
                      }
                    }}
                  >
                    {externalWhisperTestBusy ? "测试中…" : "测试外部 Whisper"}
                  </button>
                </div>
              </div>
              {externalWhisperTestResult ? (
                <div className="mt-3 rounded border bg-white p-3 text-xs">
                  <div className={externalWhisperTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{externalWhisperTestResult.ok ? "连接成功" : "测试失败"} · {externalWhisperTestResult.elapsed_ms}ms</div>
                  {externalWhisperTestResult.text ? <div className="mt-1 break-all text-slate-700">返回：{externalWhisperTestResult.text}</div> : null}
                  {externalWhisperTestResult.error ? <div className="mt-1 break-all text-rose-700">{externalWhisperTestResult.error}</div> : null}
                </div>
              ) : null}
            </div>
          ) : null}

          {defaultEngine === "cloudflare-workers-ai" ? (
            <div className="rounded border border-sky-100 bg-sky-50/60 p-3 md:col-span-2">
              <div className="text-sm font-medium text-slate-800">Cloudflare Workers AI（原生 ASR）</div>
              <div className="mt-1 text-xs text-slate-600">
                直接调用 Cloudflare `/ai/run`，解析模型返回的 segments；推荐使用 `@cf/openai/whisper-large-v3-turbo` 以获得时间轴。
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">cloudflare_workers_ai_account_id</div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={cloudflareAccountId} onChange={(e) => setCloudflareAccountId(e.target.value)} placeholder="Cloudflare Account ID" />
                </label>
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">cloudflare_workers_ai_model</div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={cloudflareModel} onChange={(e) => setCloudflareModel(e.target.value)} placeholder="@cf/openai/whisper-large-v3-turbo" />
                </label>
                <label className="block md:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">cloudflare_workers_ai_api_key（仅保存，不回显）</div>
                  <input type="password" className="w-full rounded border px-3 py-2 text-sm" value={cloudflareApiKey} onChange={(e) => setCloudflareApiKey(e.target.value)} placeholder={asrDefaults?.cloudflare_workers_ai_api_key_set ? "已设置（留空则不修改）" : "Cloudflare API Token"} />
                </label>
                <div className="flex items-end">
                  <button
                    type="button"
                    disabled={cloudflareTestBusy}
                    className="rounded border px-3 py-2 text-sm hover:bg-white disabled:opacity-50"
                    onClick={async () => {
                      setCloudflareTestBusy(true);
                      setCloudflareTestResult(null);
                      setError(null);
                      try {
                        const result = await fetchJson<CloudflareWorkersAITestResponse>(`${ORCHESTRATOR_URL}/subtitle/asr/cloudflare/test`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ account_id: cloudflareAccountId, api_key: cloudflareApiKey, model: cloudflareModel }),
                        });
                        setCloudflareTestResult(result);
                      } catch (e: unknown) {
                        setCloudflareTestResult({ ok: false, elapsed_ms: 0, text: "", segments: 0, error: e instanceof Error ? e.message : String(e) });
                      } finally {
                        setCloudflareTestBusy(false);
                      }
                    }}
                  >
                    {cloudflareTestBusy ? "测试中…" : "测试 Cloudflare ASR"}
                  </button>
                </div>
              </div>
              {cloudflareTestResult ? (
                <div className="mt-3 rounded border bg-white p-3 text-xs">
                  <div className={cloudflareTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{cloudflareTestResult.ok ? "连接成功" : "测试失败"} · {cloudflareTestResult.elapsed_ms}ms · segments={cloudflareTestResult.segments}</div>
                  {cloudflareTestResult.text ? <div className="mt-1 break-all text-slate-700">返回：{cloudflareTestResult.text}</div> : null}
                  {cloudflareTestResult.error ? <div className="mt-1 break-all text-rose-700">{cloudflareTestResult.error}</div> : null}
                </div>
              ) : null}
            </div>
          ) : null}

          {defaultEngine === "groq-whisper" ? (
            <div className="rounded border border-orange-100 bg-orange-50/60 p-3 md:col-span-2">
              <div className="text-sm font-medium text-slate-800">GroqCloud Whisper（专用接入）</div>
              <div className="mt-1 text-xs text-slate-600">
                调用 Groq 的 `/openai/v1/audio/transcriptions`。音频会按固定 45 秒转为无损 FLAC 分片，并保留 5 秒重叠；网络断开或上游 5xx/524 时每片最多重试 5 次。成功分片会保存检查点，点击“继续字幕”可从失败分片继续，并合并原始时间轴。
              </div>
              <div className="mt-3 grid gap-3 md:grid-cols-2">
                <label className="block md:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">groq_whisper_api_key（仅保存，不回显）</div>
                  <input type="password" className="w-full rounded border px-3 py-2 text-sm" value={groqWhisperApiKey} onChange={(e) => setGroqWhisperApiKey(e.target.value)} placeholder={asrDefaults?.groq_whisper_api_key_set ? "已设置（留空则不修改）" : "gsk_..."} />
                </label>
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">groq_whisper_model</div>
                  <select className="w-full rounded border px-3 py-2 text-sm" value={groqWhisperModel} onChange={(e) => setGroqWhisperModel(e.target.value)}>
                    <option value="whisper-large-v3-turbo">whisper-large-v3-turbo（推荐）</option>
                    <option value="whisper-large-v3">whisper-large-v3（高准确率）</option>
                  </select>
                </label>
                <div className="flex items-end">
                  <button
                    type="button"
                    disabled={groqWhisperTestBusy}
                    className="rounded border px-3 py-2 text-sm hover:bg-white disabled:opacity-50"
                    onClick={async () => {
                      setGroqWhisperTestBusy(true);
                      setGroqWhisperTestResult(null);
                      setError(null);
                      try {
                        const result = await fetchJson<GroqWhisperTestResponse>(`${ORCHESTRATOR_URL}/subtitle/asr/groq/test`, {
                          method: "POST",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ api_key: groqWhisperApiKey, model: groqWhisperModel }),
                        });
                        setGroqWhisperTestResult(result);
                      } catch (e: unknown) {
                        setGroqWhisperTestResult({ ok: false, elapsed_ms: 0, text: "", segments: 0, error: e instanceof Error ? e.message : String(e) });
                      } finally {
                        setGroqWhisperTestBusy(false);
                      }
                    }}
                  >
                    {groqWhisperTestBusy ? "测试中…" : "测试 Groq ASR"}
                  </button>
                </div>
              </div>
              {groqWhisperTestResult ? (
                <div className="mt-3 rounded border bg-white p-3 text-xs">
                  <div className={groqWhisperTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{groqWhisperTestResult.ok ? "连接成功" : "测试失败"} · {groqWhisperTestResult.elapsed_ms}ms · segments={groqWhisperTestResult.segments}</div>
                  {groqWhisperTestResult.text ? <div className="mt-1 break-all text-slate-700">返回：{groqWhisperTestResult.text}</div> : null}
                  {groqWhisperTestResult.error ? <div className="mt-1 break-all text-rose-700">{groqWhisperTestResult.error}</div> : null}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="rounded border border-sky-100 bg-sky-50/50 p-3 md:col-span-2">
            <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-slate-800">
              <input
                type="checkbox"
                checked={openvinoVadEnabled}
                onChange={(e) => setOpenvinoVadEnabled(e.target.checked)}
              />
              OpenVINO / Groq 启用人声检测（推荐）
            </label>
            <div className="mt-1 text-xs text-slate-600">
              OpenVINO 会只处理 Silero VAD 找到的人声片段；Groq 会在每个 45 秒切片上传前检测，无人声切片保存为空检查点并跳过上传，可减少请求和无声幻觉字幕。
            </div>
            <label className="mt-3 block max-w-xs">
              <div className="mb-1 text-xs text-slate-600">人声检测阈值（0.5 平衡；0.6 更严格）</div>
              <input
                type="number"
                min={0.1}
                max={0.95}
                step={0.05}
                disabled={!openvinoVadEnabled}
                className="w-full rounded border px-3 py-2 text-sm disabled:bg-slate-100"
                value={openvinoVadThreshold}
                onChange={(e) => setOpenvinoVadThreshold(e.target.value)}
              />
            </label>
            <div className="mt-1 text-xs text-slate-500">阈值越高越不容易把背景音误认成人声，但极轻、较远的人声更可能漏掉。</div>
          </div>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openvino_device</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              value={openvinoDevice}
              onChange={(e) => setOpenvinoDevice(e.target.value)}
              placeholder="GPU / GPU.0 / CPU"
            />
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">openvino_num_beams</div>
            <input
              type="number"
              min={1}
              max={16}
              className="w-full rounded border px-3 py-2 text-sm"
              value={openvinoNumBeams}
              onChange={(e) => setOpenvinoNumBeams(e.target.value)}
            />
          </label>

          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">openvino_max_new_tokens</div>
            <input
              type="number"
              min={1}
              max={4096}
              className="w-full rounded border px-3 py-2 text-sm"
              value={openvinoMaxNewTokens}
              onChange={(e) => setOpenvinoMaxNewTokens(e.target.value)}
            />
            <div className="mt-1 text-xs text-slate-500">
              这些参数仅在 `default_engine=openvino` 或任务里显式选择 `openvino` 时生效。
            </div>
          </label>

          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">model_download_proxy（仅用于模型下载）</div>
            <div className="flex items-center gap-2">
              <input
                className="w-full flex-1 rounded border px-3 py-2 text-sm"
                value={modelDownloadProxy}
                onChange={(e) => setModelDownloadProxy(e.target.value)}
                placeholder="http://127.0.0.1:7890 / socks5://127.0.0.1:1080"
              />
              <button
                type="button"
                disabled={proxyTestBusy}
                className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
                onClick={async () => {
                  setProxyTestBusy(true);
                  setProxyTestResult(null);
                  setError(null);
                  try {
                    const res = await fetchJson<ModelProxyTestResponse>(`${ORCHESTRATOR_URL}/subtitle/models/proxy/test`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        proxy: modelDownloadProxy.trim() ? modelDownloadProxy.trim() : null,
                        url: "https://huggingface.co/robots.txt",
                      }),
                    });
                    setProxyTestResult(res);
                  } catch (e: unknown) {
                    setError(e instanceof Error ? e.message : String(e));
                  } finally {
                    setProxyTestBusy(false);
                  }
                }}
              >
                {proxyTestBusy ? "检测中…" : "检测"}
              </button>
            </div>
            <div className="mt-1 text-xs text-slate-500">
              用于 Settings · ASR 的模型下载/任务自动下载模型。留空=不使用代理。
            </div>
            {proxyTestResult ? (
              <div className="mt-2 rounded border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <div className={proxyTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{proxyTestResult.ok ? "OK" : "FAILED"}</div>
                  <div className="text-slate-600">status={proxyTestResult.status_code ?? "-"}</div>
                  <div className="text-slate-600">elapsed={proxyTestResult.elapsed_ms}ms</div>
                </div>
                <div className="mt-2 text-xs text-slate-600 break-all">url: {proxyTestResult.url}</div>
                <div className="mt-1 text-xs text-slate-600 break-all">proxy: {proxyTestResult.used_proxy ?? "(none)"}</div>
                {proxyTestResult.error ? <div className="mt-2 text-xs text-rose-700 break-all">{proxyTestResult.error}</div> : null}
              </div>
            ) : null}
          </label>
        </div>

        <div className="mt-3">
          <button
            disabled={busy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const openvinoNumBeamsValue = Math.max(1, Number.parseInt(openvinoNumBeams || "1", 10) || 1);
                const openvinoMaxNewTokensValue = Math.max(1, Number.parseInt(openvinoMaxNewTokens || "448", 10) || 448);
                const parsedOpenvinoVadThreshold = Number.parseFloat(openvinoVadThreshold || "0.5");
                const openvinoVadThresholdValue = Math.min(0.95, Math.max(0.1, Number.isFinite(parsedOpenvinoVadThreshold) ? parsedOpenvinoVadThreshold : 0.5));
                await fetchJson(`${ORCHESTRATOR_URL}/subtitle/asr/settings`, {
                  method: "PUT",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    default_engine: defaultEngine,
                    default_language: defaultLanguage,
                    default_model: defaultEngine === "external-whisper" ? externalWhisperModel : defaultEngine === "groq-whisper" ? groqWhisperModel : defaultEngine === "cloudflare-workers-ai" ? cloudflareModel : defaultModel,
                    openvino_device: openvinoDevice,
                    openvino_num_beams: openvinoNumBeamsValue,
                    openvino_max_new_tokens: openvinoMaxNewTokensValue,
                    openvino_vad_enabled: openvinoVadEnabled,
                    openvino_vad_threshold: openvinoVadThresholdValue,
                    model_download_proxy: modelDownloadProxy,
                    external_whisper_base_url: externalWhisperBaseUrl,
                    external_whisper_model: externalWhisperModel,
                    ...(externalWhisperApiKey.trim() ? { external_whisper_api_key: externalWhisperApiKey.trim() } : {}),
                    groq_whisper_model: groqWhisperModel,
                    ...(groqWhisperApiKey.trim() ? { groq_whisper_api_key: groqWhisperApiKey.trim() } : {}),
                    cloudflare_workers_ai_account_id: cloudflareAccountId,
                    cloudflare_workers_ai_model: cloudflareModel,
                    ...(cloudflareApiKey.trim() ? { cloudflare_workers_ai_api_key: cloudflareApiKey.trim() } : {}),
                  }),
                });
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "保存中…" : "保存默认 ASR"}
          </button>
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">本地模型（后端目录）</div>
        {!models ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}
        {models && models.length === 0 ? <div className="mt-2 text-sm text-slate-500">暂无</div> : null}
        {models && models.length > 0 ? (
          <div className="mt-2 overflow-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr>
                  <th className="py-2 pr-3">Name</th>
                  <th className="py-2 pr-3">Size</th>
                  <th className="py-2 pr-3">Path</th>
                  <th className="py-2 pr-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.name} className="border-t">
                    <td className="py-2 pr-3 font-mono text-xs">{m.name}</td>
                    <td className="py-2 pr-3 text-xs">{formatBytes(m.size_bytes)}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{m.path}</td>
                    <td className="py-2 pr-3">
                      <button
                        disabled={busy}
                        className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                        onClick={async () => {
                          const ok = await confirm({
                            title: "删除模型",
                            message: `确定删除模型：${m.name} ?`,
                            confirmLabel: "删除",
                            tone: "danger",
                          });
                          if (!ok) return;
                          setBusy(true);
                          setError(null);
                          try {
                            await fetchJson(`${ORCHESTRATOR_URL}/subtitle/models/${encodeURIComponent(m.name)}`, { method: "DELETE" });
                            await refresh();
                          } catch (e: unknown) {
                            setError(e instanceof Error ? e.message : String(e));
                          } finally {
                            setBusy(false);
                          }
                        }}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">下载模型（从 Hugging Face）</div>
        <div className="mt-2 text-xs text-slate-500">
          支持 `faster-whisper` 和 `openvino`。选择 `openvino` 时，`tiny/base/small/medium/large-v3` 会映射到 OpenVINO 官方预转换 Whisper 仓库。
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">engine</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={downloadEngine} onChange={(e) => setDownloadEngine(e.target.value)}>
              <option value="faster-whisper">faster-whisper</option>
              <option value="openvino">openvino（OpenVINO 官方源）</option>
            </select>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">model</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={downloadModel} onChange={(e) => setDownloadModel(e.target.value)} />
            <div className="mt-2 flex flex-wrap gap-2">
              {knownSizes.map((s) => (
                <button
                  key={s}
                  type="button"
                  className="rounded border px-2 py-1 text-xs hover:bg-slate-50"
                  onClick={() => setDownloadModel(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">name（本地目录名，可选）</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              placeholder={downloadEngine === "openvino" ? "例如 whisper-small-fp16-ov" : "例如 tiny"}
              value={downloadName}
              onChange={(e) => setDownloadName(e.target.value)}
            />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">revision（可选）</div>
            <input className="w-full rounded border px-3 py-2 text-sm" placeholder="main / commit sha" value={downloadRevision} onChange={(e) => setDownloadRevision(e.target.value)} />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={downloadForce} onChange={(e) => setDownloadForce(e.target.checked)} />
            force（覆盖同名目录）
          </label>
        </div>
        <div className="mt-3">
          <button
            disabled={busy}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await fetchJson(`${ORCHESTRATOR_URL}/subtitle/models/download`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    engine: downloadEngine,
                    model: downloadModel.trim(),
                    name: downloadName.trim() ? downloadName.trim() : null,
                    revision: downloadRevision.trim() ? downloadRevision.trim() : null,
                    force: downloadForce,
                  }),
                });
                setDownloadName("");
                setDownloadRevision("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "下载中…" : "开始下载"}
          </button>
        </div>
      </div>

      <div className="rounded border bg-white p-4">
        <div className="text-sm font-semibold">上传模型（zip）</div>
        <div className="mt-2 text-xs text-slate-500">
          上传一个 zip 包。解压后可以是 `faster-whisper/ctranslate2` 模型目录，也可以是已导出的 OpenVINO Whisper 模型目录。目录名仅允许字母数字与 `._-`。
        </div>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">name</div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={uploadName} onChange={(e) => setUploadName(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">file.zip</div>
            <input type="file" accept=".zip" onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)} />
          </label>
        </div>
        <div className="mt-3">
          <button
            disabled={busy || !uploadFile || !uploadName.trim()}
            className="rounded bg-slate-900 px-3 py-2 text-sm text-white hover:bg-slate-800 disabled:opacity-50"
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                const fd = new FormData();
                if (!uploadFile) throw new Error("no file selected");
                fd.append("file", uploadFile, uploadFile.name);
                await fetchJson(`${ORCHESTRATOR_URL}/subtitle/models/upload?name=${encodeURIComponent(uploadName.trim())}`, {
                  method: "POST",
                  body: fd,
                });
                setUploadFile(null);
                setUploadName("");
                await refresh();
              } catch (e: unknown) {
                setError(e instanceof Error ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "上传中…" : "上传"}
          </button>
        </div>
      </div>

      <div className="rounded border bg-white p-4 text-xs text-slate-600">
        <div className="font-semibold text-slate-700">如何在任务里使用本地模型？</div>
        <div className="mt-2">
          在任务详情页生成字幕时，可将 `asr_engine` 设为 `faster-whisper` 或 `openvino`；`asr_model` 支持模型目录路径（例如：`/models/whisper/tiny` 或 `/models/whisper/whisper-large-v3-ov`）。
        </div>
      </div>
    </div>
  );
}
