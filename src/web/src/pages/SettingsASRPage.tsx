import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useConfirm } from "../components/feedbackContext";
import { SettingsSaveBar } from "../components/ui";
import { type ASRDefaults, useASRForm } from "../features/settings-asr/form";
import { useUnsavedChangesGuard } from "../hooks/useUnsavedChangesGuard";
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

type IntelHardwareProbe = {
  checked: boolean;
  available: boolean;
  render_device: string;
  model_name?: string | null;
  driver?: string | null;
  pci_slot?: string | null;
  pci_id?: string | null;
  detail?: string;
  openvino_devices?: string[];
  openvino_gpu_available?: boolean;
  openvino_error?: string;
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

function asrDefaultsSnapshot(defaults: ASRDefaults): string {
  return JSON.stringify({
    defaultEngine: defaults.default_engine || "faster-whisper",
    defaultLanguage: defaults.default_language || "auto",
    effectiveDefaultModel: defaults.default_model || "",
    openvinoDevice: defaults.openvino_device?.trim() || "GPU",
    openvinoNumBeams: String(Math.max(1, Number(defaults.openvino_num_beams || 1))),
    openvinoMaxNewTokens: String(Math.max(1, Number(defaults.openvino_max_new_tokens || 448))),
    openvinoVadEnabled: Boolean(defaults.openvino_vad_enabled),
    openvinoVadThreshold: String(defaults.openvino_vad_threshold ?? 0.5),
    modelDownloadProxy: defaults.model_download_proxy || "",
    externalWhisperBaseUrl: defaults.external_whisper_base_url || "",
    externalWhisperModel: defaults.external_whisper_model || "whisper-1",
    groqWhisperModel: defaults.groq_whisper_model || "whisper-large-v3-turbo",
    cloudflareAccountId: defaults.cloudflare_workers_ai_account_id || "",
    cloudflareModel: defaults.cloudflare_workers_ai_model || "@cf/openai/whisper-large-v3-turbo",
  });
}

export default function SettingsASRPage() {
  const confirm = useConfirm();
  const savedDefaultsSnapshotRef = useRef("");
  const {
    downloadModel, setDownloadModel,
    downloadEngine, setDownloadEngine,
    downloadName, setDownloadName,
    downloadRevision, setDownloadRevision,
    downloadForce, setDownloadForce,
    uploadName, setUploadName,
    defaultEngine, setDefaultEngine,
    defaultLanguage, setDefaultLanguage,
    defaultModel, setDefaultModel,
    openvinoDevice, setOpenvinoDevice,
    openvinoNumBeams, setOpenvinoNumBeams,
    openvinoMaxNewTokens, setOpenvinoMaxNewTokens,
    openvinoVadEnabled, setOpenvinoVadEnabled,
    openvinoVadThreshold, setOpenvinoVadThreshold,
    modelDownloadProxy, setModelDownloadProxy,
    externalWhisperBaseUrl, setExternalWhisperBaseUrl,
    externalWhisperModel, setExternalWhisperModel,
    externalWhisperApiKey, setExternalWhisperApiKey,
    groqWhisperModel, setGroqWhisperModel,
    groqWhisperApiKey, setGroqWhisperApiKey,
    cloudflareAccountId, setCloudflareAccountId,
    cloudflareModel, setCloudflareModel,
    cloudflareApiKey, setCloudflareApiKey,
    applyDefaults,
  } = useASRForm();
  const [settings, setSettings] = useState<WhisperSettings | null>(null);
  const [asrDefaults, setAsrDefaults] = useState<ASRDefaults | null>(null);
  const [models, setModels] = useState<WhisperModelInfo[] | null>(null);
  const [intelHardware, setIntelHardware] = useState<IntelHardwareProbe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [proxyTestBusy, setProxyTestBusy] = useState(false);
  const [proxyTestResult, setProxyTestResult] = useState<ModelProxyTestResponse | null>(null);
  const [externalWhisperTestBusy, setExternalWhisperTestBusy] = useState(false);
  const [externalWhisperTestResult, setExternalWhisperTestResult] = useState<ExternalWhisperTestResponse | null>(null);
  const [groqWhisperTestBusy, setGroqWhisperTestBusy] = useState(false);
  const [groqWhisperTestResult, setGroqWhisperTestResult] = useState<GroqWhisperTestResponse | null>(null);
  const [cloudflareTestBusy, setCloudflareTestBusy] = useState(false);
  const [cloudflareTestResult, setCloudflareTestResult] = useState<CloudflareWorkersAITestResponse | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [s, m, a, intel] = await Promise.all([
        fetchJson<WhisperSettings>(`${ORCHESTRATOR_URL}/subtitle/settings`),
        fetchJson<WhisperModelInfo[]>(`${ORCHESTRATOR_URL}/subtitle/models`),
        fetchJson<ASRDefaults>(`${ORCHESTRATOR_URL}/subtitle/asr/settings`),
        fetchJson<IntelHardwareProbe>(`${ORCHESTRATOR_URL}/subtitle/hardware/intel`).catch(() => null),
      ]);
      setSettings(s);
      setModels(m);
      setAsrDefaults(a);
      setIntelHardware(intel);
      savedDefaultsSnapshotRef.current = asrDefaultsSnapshot(a);
      applyDefaults(a);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [applyDefaults]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const knownSizes = useMemo(() => ["tiny", "base", "small", "medium", "large-v3"], []);

  const effectiveDefaultModel =
    defaultEngine === "external-whisper"
      ? externalWhisperModel
      : defaultEngine === "groq-whisper"
        ? groqWhisperModel
        : defaultEngine === "cloudflare-workers-ai"
          ? cloudflareModel
          : defaultModel;
  const currentDefaultsSnapshot = JSON.stringify({
    defaultEngine,
    defaultLanguage,
    effectiveDefaultModel,
    openvinoDevice,
    openvinoNumBeams: String(Math.max(1, Number.parseInt(openvinoNumBeams || "1", 10) || 1)),
    openvinoMaxNewTokens: String(Math.max(1, Number.parseInt(openvinoMaxNewTokens || "448", 10) || 448)),
    openvinoVadEnabled,
    openvinoVadThreshold: String(
      Math.min(0.95, Math.max(0.1, Number.isFinite(Number.parseFloat(openvinoVadThreshold || "0.5")) ? Number.parseFloat(openvinoVadThreshold || "0.5") : 0.5)),
    ),
    modelDownloadProxy,
    externalWhisperBaseUrl,
    externalWhisperModel,
    groqWhisperModel,
    cloudflareAccountId,
    cloudflareModel,
  });
  const isDefaultsDirty =
    Boolean(asrDefaults) &&
    (
      savedDefaultsSnapshotRef.current !== currentDefaultsSnapshot ||
      Boolean(externalWhisperApiKey.trim()) ||
      Boolean(groqWhisperApiKey.trim()) ||
      Boolean(cloudflareApiKey.trim())
    );
  useUnsavedChangesGuard(isDefaultsDirty, { message: "离开当前页面会丢失尚未保存的 ASR 默认配置。" });

  const discardDefaults = useCallback(() => {
    if (!asrDefaults) return;
    applyDefaults(asrDefaults);
    setExternalWhisperApiKey("");
    setGroqWhisperApiKey("");
    setCloudflareApiKey("");
    setError(null);
  }, [applyDefaults, asrDefaults, setCloudflareApiKey, setExternalWhisperApiKey, setGroqWhisperApiKey]);

  async function saveDefaults() {
    setBusy(true);
    setError(null);
    try {
      const openvinoNumBeamsValue = Math.max(1, Number.parseInt(openvinoNumBeams || "1", 10) || 1);
      const openvinoMaxNewTokensValue = Math.max(1, Number.parseInt(openvinoMaxNewTokens || "448", 10) || 448);
      const parsedOpenvinoVadThreshold = Number.parseFloat(openvinoVadThreshold || "0.5");
      const openvinoVadThresholdValue = Math.min(
        0.95,
        Math.max(0.1, Number.isFinite(parsedOpenvinoVadThreshold) ? parsedOpenvinoVadThreshold : 0.5),
      );
      await fetchJson(`${ORCHESTRATOR_URL}/subtitle/asr/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          default_engine: defaultEngine,
          default_language: defaultLanguage,
          default_model: effectiveDefaultModel,
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
      setExternalWhisperApiKey("");
      setGroqWhisperApiKey("");
      setCloudflareApiKey("");
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="px-1">
        <h2 className="text-xl font-semibold tracking-tight text-slate-950">语音识别（ASR）</h2>
        <div className="mt-1 text-sm text-slate-600">管理默认 ASR 引擎、模型路径与 OpenVINO 参数；本地模型目录也可用于上传 OpenVINO Whisper 导出模型。</div>
        {error ? <div className="mt-3 text-sm text-rose-700">{error}</div> : null}
      </div>

      <div className="vr-section">
        <div className="flex items-center justify-between">
          <div className="text-sm font-semibold">当前配置（来自后端环境变量）</div>
          <button onClick={() => refresh()} className="rounded border px-3 py-2 text-sm hover:bg-slate-50">
            刷新
          </button>
        </div>
        {!settings ? (
          <div className="mt-2 text-sm text-slate-500">加载中…</div>
        ) : (
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div className="rounded border border-slate-200 p-3 lg:col-span-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <div className="text-xs text-slate-500">Intel GPU 运行设备</div>
                  <div className="mt-1 text-sm font-medium">
                    {intelHardware?.available ? intelHardware.model_name || "Intel GPU detected" : "未检测到可用 Intel GPU"}
                  </div>
                </div>
                <span
                  className={[
                    "rounded-full px-2 py-1 text-xs",
                    intelHardware?.available && intelHardware?.openvino_gpu_available
                      ? "bg-emerald-50 text-emerald-700"
                      : intelHardware?.available
                        ? "bg-amber-50 text-amber-700"
                        : "bg-rose-50 text-rose-700",
                  ].join(" ")}
                >
                  {intelHardware?.available && intelHardware?.openvino_gpu_available
                    ? "GPU runtime ready"
                    : intelHardware?.available
                      ? "DRM only"
                      : "Unavailable"}
                </span>
              </div>
              <div className="mt-2 grid gap-1 text-xs text-slate-500 sm:grid-cols-2">
                <div className="break-all">DRM：{intelHardware?.render_device || "-"}</div>
                <div>Driver：{intelHardware?.driver || "-"}</div>
                {intelHardware?.pci_slot ? <div>PCI：{intelHardware.pci_slot}</div> : null}
                {intelHardware?.pci_id ? <div>PCI ID：{intelHardware.pci_id}</div> : null}
                <div>OpenVINO devices：{intelHardware?.openvino_devices?.join(", ") || "-"}</div>
                <div>
                  OpenVINO GPU：
                  <span className={intelHardware?.openvino_gpu_available ? "text-emerald-700" : "text-rose-700"}>
                    {intelHardware?.openvino_gpu_available ? "可用" : "不可用"}
                  </span>
                </div>
              </div>
              {!intelHardware?.available && intelHardware?.detail ? (
                <div className="mt-2 break-words text-xs text-rose-700">{intelHardware.detail}</div>
              ) : null}
              {intelHardware?.openvino_error ? (
                <div className="mt-2 break-words text-xs text-rose-700">OpenVINO：{intelHardware.openvino_error}</div>
              ) : null}
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">默认 ASR 引擎</div>
              <div className="mt-1 text-sm font-medium">{settings.asr_engine}</div>
              <div className="mt-1 font-mono text-[11px] text-slate-400">SUBTITLE_ASR_ENGINE</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">faster-whisper Runtime</div>
              <div className={["mt-1 text-sm font-medium", settings.faster_whisper_installed ? "text-emerald-700" : "text-rose-700"].join(" ")}>{settings.faster_whisper_installed ? "已安装" : "未安装"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">OpenVINO GenAI Runtime</div>
              <div className={["mt-1 text-sm font-medium", settings.openvino_installed ? "text-emerald-700" : "text-rose-700"].join(" ")}>{settings.openvino_installed ? "已安装" : "未安装"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">Whisper 默认模型 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_WHISPER_MODEL</span></div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_model}</div>
            </div>
            <div className="rounded border p-3 lg:col-span-2">
              <div className="text-xs text-slate-500">ASR 模型目录 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_WHISPER_MODEL_DIR</span></div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_model_dir}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">faster-whisper 设备</div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_device}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">计算精度</div>
              <div className="mt-1 font-mono text-sm">{settings.whisper_compute_type}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">CPU 线程 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_WHISPER_CPU_THREADS</span></div>
              <div className="mt-1 font-mono text-sm">
                {settings.whisper_cpu_threads}{" "}
                {settings.whisper_cpu_threads !== settings.whisper_cpu_threads_effective ? `(effective: ${settings.whisper_cpu_threads_effective})` : null}
              </div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">ASR Worker 数 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_WHISPER_NUM_WORKERS</span></div>
              <div className="mt-1 font-mono text-sm">
                {settings.whisper_num_workers}{" "}
                {settings.whisper_num_workers !== settings.whisper_num_workers_effective ? `(effective: ${settings.whisper_num_workers_effective})` : null}
              </div>
            </div>
            <div className="rounded border p-3 lg:col-span-2">
              <div className="text-xs text-slate-500">OpenVINO 模型 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_OPENVINO_MODEL</span></div>
              <div className="mt-1 font-mono text-sm break-all">{settings.openvino_model || "(empty)"}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">OpenVINO 设备 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_OPENVINO_DEVICE</span></div>
              <div className="mt-1 font-mono text-sm">{settings.openvino_device}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">Beam 数 <span className="font-mono text-[10px] text-slate-400">SUBTITLE_OPENVINO_NUM_BEAMS</span></div>
              <div className="mt-1 font-mono text-sm">{settings.openvino_num_beams}</div>
            </div>
            <div className="rounded border p-3">
              <div className="text-xs text-slate-500">最大生成 Tokens <span className="font-mono text-[10px] text-slate-400">SUBTITLE_OPENVINO_MAX_NEW_TOKENS</span></div>
              <div className="mt-1 font-mono text-sm">{settings.openvino_max_new_tokens}</div>
            </div>
          </div>
        )}
        <div className="mt-3 text-xs text-slate-500">
          提示：`faster-whisper` 和 `openvino-genai` 都依赖 `INSTALL_ASR=1` 构建。要启用方案 2，请把默认引擎切到 `openvino`，并提供一个已导出的 OpenVINO Whisper 模型目录。
        </div>
      </div>

      <div className="vr-section">
        <div className="text-sm font-semibold">默认 ASR 参数（存储在数据库）</div>
        <div className="mt-2 text-xs text-slate-500">
          当任务中选择 <span className="font-mono">engine=auto</span> / <span className="font-mono">language=auto</span> / 未指定 model 时，会使用这里的默认值。
        </div>

        {!asrDefaults ? <div className="mt-2 text-sm text-slate-500">加载中…</div> : null}

        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">默认引擎</div>
            <select className="w-full rounded border px-3 py-2 text-sm" value={defaultEngine} onChange={(e) => setDefaultEngine(e.target.value)}>
              <option value="faster-whisper">faster-whisper</option>
              <option value="openvino">openvino（方案2 / Intel Arc）</option>
              <option value="external-whisper">在线 Whisper（自建 faster-whisper / OpenAI 兼容）</option>
              <option value="groq-whisper">groq-whisper（GroqCloud，自动切片）</option>
              <option value="cloudflare-workers-ai">cloudflare-workers-ai（原生时间轴）</option>
              <option value="mock">mock</option>
            </select>
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">默认语言 <span className="font-mono text-[10px] text-slate-400">default_language</span></div>
            <input className="w-full rounded border px-3 py-2 text-sm" value={defaultLanguage} onChange={(e) => setDefaultLanguage(e.target.value)} placeholder="auto / zh / en ..." />
          </label>

          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">默认模型（size / repo id / 本地路径） <span className="font-mono text-[10px] text-slate-400">default_model</span></div>
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
              `faster-whisper` 可用 size/repo id/本地路径；`openvino` 需要填写一个已导出的 OpenVINO Whisper 模型目录路径；在线 Whisper 使用下方配置的远程服务地址与模型名。
            </div>
          </label>

          {defaultEngine === "external-whisper" ? (
            <div className="rounded border border-amber-100 bg-amber-50/60 p-3 lg:col-span-2">
              <div className="text-sm font-medium text-slate-800">在线 Whisper（自建 faster-whisper / OpenAI 兼容）</div>
              <div className="mt-1 text-xs text-slate-600">
                填写 VideoRoll 生产机能够访问的 Whisper 服务地址。支持 `http://192.168.1.10:8000`、`http://192.168.1.10:8000/v1`，也可直接填写完整的 `/v1/audio/transcriptions` 地址。
              </div>
              <div className="mt-3 grid gap-3 lg:grid-cols-2">
                <label className="block lg:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">服务地址 <span className="font-mono text-[10px] text-slate-400">external_whisper_base_url</span></div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={externalWhisperBaseUrl} onChange={(e) => setExternalWhisperBaseUrl(e.target.value)} placeholder="http://192.168.1.10:8000/v1" />
                  <div className="mt-1 text-xs text-slate-500">
                    请求由 subtitle-worker 发出；如果 faster-whisper 在另一台机器上，请填写生产机可达的 IP/域名，不要填写你浏览器电脑的 127.0.0.1。
                  </div>
                </label>
                <label className="block lg:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">API Key（可选，仅保存不回显） <span className="font-mono text-[10px] text-slate-400">external_whisper_api_key</span></div>
                  <input type="password" className="w-full rounded border px-3 py-2 text-sm" value={externalWhisperApiKey} onChange={(e) => setExternalWhisperApiKey(e.target.value)} placeholder={asrDefaults?.external_whisper_api_key_set ? "已设置（留空则继续使用已保存 Key）" : "本地服务无需鉴权可留空"} />
                </label>
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">模型名 <span className="font-mono text-[10px] text-slate-400">external_whisper_model</span></div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={externalWhisperModel} onChange={(e) => setExternalWhisperModel(e.target.value)} placeholder="whisper-1" />
                  <div className="mt-1 text-xs text-slate-500">服务端固定模型时保持默认 `whisper-1` 即可；如果你的服务要求指定模型 ID，可在这里填写。</div>
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
                    {externalWhisperTestBusy ? "测试中…" : "测试在线 Whisper"}
                  </button>
                </div>
              </div>
              {externalWhisperTestResult ? (
                <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
                  <div className={externalWhisperTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{externalWhisperTestResult.ok ? "连接成功" : "测试失败"} · {externalWhisperTestResult.elapsed_ms}ms</div>
                  {externalWhisperTestResult.text ? <div className="mt-1 break-all text-slate-700">返回：{externalWhisperTestResult.text}</div> : null}
                  {externalWhisperTestResult.error ? <div className="mt-1 break-all text-rose-700">{externalWhisperTestResult.error}</div> : null}
                </div>
              ) : null}
            </div>
          ) : null}

          {defaultEngine === "cloudflare-workers-ai" ? (
            <div className="rounded border border-sky-100 bg-sky-50/60 p-3 lg:col-span-2">
              <div className="text-sm font-medium text-slate-800">Cloudflare Workers AI（原生 ASR）</div>
              <div className="mt-1 text-xs text-slate-600">
                直接调用 Cloudflare `/ai/run`，解析模型返回的 segments；推荐使用 `@cf/openai/whisper-large-v3-turbo` 以获得时间轴。
              </div>
              <div className="mt-3 grid gap-3 lg:grid-cols-2">
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">Cloudflare Account ID</div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={cloudflareAccountId} onChange={(e) => setCloudflareAccountId(e.target.value)} placeholder="Cloudflare Account ID" />
                </label>
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">Cloudflare ASR 模型</div>
                  <input className="w-full rounded border px-3 py-2 text-sm" value={cloudflareModel} onChange={(e) => setCloudflareModel(e.target.value)} placeholder="@cf/openai/whisper-large-v3-turbo" />
                </label>
                <label className="block lg:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">Cloudflare API Token（仅保存，不回显）</div>
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
                <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
                  <div className={cloudflareTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{cloudflareTestResult.ok ? "连接成功" : "测试失败"} · {cloudflareTestResult.elapsed_ms}ms · segments={cloudflareTestResult.segments}</div>
                  {cloudflareTestResult.text ? <div className="mt-1 break-all text-slate-700">返回：{cloudflareTestResult.text}</div> : null}
                  {cloudflareTestResult.error ? <div className="mt-1 break-all text-rose-700">{cloudflareTestResult.error}</div> : null}
                </div>
              ) : null}
            </div>
          ) : null}

          {defaultEngine === "groq-whisper" ? (
            <div className="rounded border border-orange-100 bg-orange-50/60 p-3 lg:col-span-2">
              <div className="text-sm font-medium text-slate-800">GroqCloud Whisper（专用接入）</div>
              <div className="mt-1 text-xs text-slate-600">
                调用 Groq 的 `/openai/v1/audio/transcriptions`。音频会按固定 45 秒转为无损 FLAC 分片，并保留 5 秒重叠；网络断开或上游 5xx/524 时每片最多重试 5 次。成功分片会保存检查点，点击“继续字幕”可从失败分片继续，并合并原始时间轴。
              </div>
              <div className="mt-3 grid gap-3 lg:grid-cols-2">
                <label className="block lg:col-span-2">
                  <div className="mb-1 text-xs text-slate-600">Groq API Key（仅保存，不回显）</div>
                  <input type="password" className="w-full rounded border px-3 py-2 text-sm" value={groqWhisperApiKey} onChange={(e) => setGroqWhisperApiKey(e.target.value)} placeholder={asrDefaults?.groq_whisper_api_key_set ? "已设置（留空则不修改）" : "gsk_..."} />
                </label>
                <label className="block">
                  <div className="mb-1 text-xs text-slate-600">Groq Whisper 模型</div>
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
                <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
                  <div className={groqWhisperTestResult.ok ? "text-emerald-700" : "text-rose-700"}>{groqWhisperTestResult.ok ? "连接成功" : "测试失败"} · {groqWhisperTestResult.elapsed_ms}ms · segments={groqWhisperTestResult.segments}</div>
                  {groqWhisperTestResult.text ? <div className="mt-1 break-all text-slate-700">返回：{groqWhisperTestResult.text}</div> : null}
                  {groqWhisperTestResult.error ? <div className="mt-1 break-all text-rose-700">{groqWhisperTestResult.error}</div> : null}
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="rounded border border-sky-100 bg-sky-50/50 p-3 lg:col-span-2">
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
            <div className="mb-1 text-xs text-slate-600">OpenVINO 设备</div>
            <input
              className="w-full rounded border px-3 py-2 text-sm"
              value={openvinoDevice}
              onChange={(e) => setOpenvinoDevice(e.target.value)}
              placeholder="GPU / GPU.0 / CPU"
            />
          </label>

          <label className="block">
            <div className="mb-1 text-xs text-slate-600">OpenVINO Beam 数</div>
            <input
              type="number"
              min={1}
              max={16}
              className="w-full rounded border px-3 py-2 text-sm"
              value={openvinoNumBeams}
              onChange={(e) => setOpenvinoNumBeams(e.target.value)}
            />
          </label>

          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">OpenVINO 最大生成 Tokens</div>
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

          <label className="block lg:col-span-2">
            <div className="mb-1 text-xs text-slate-600">模型下载代理</div>
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
              用于 ASR 设置 的模型下载/任务自动下载模型。留空=不使用代理。
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

      </div>

      <div className="vr-section">
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

      <div className="vr-section">
        <div className="text-sm font-semibold">下载模型（从 Hugging Face）</div>
        <div className="mt-2 text-xs text-slate-500">
          支持 `faster-whisper` 和 `openvino`。选择 `openvino` 时，`tiny/base/small/medium/large-v3` 会映射到 OpenVINO 官方预转换 Whisper 仓库。
        </div>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
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

      <div className="vr-section">
        <div className="text-sm font-semibold">上传模型（zip）</div>
        <div className="mt-2 text-xs text-slate-500">
          上传一个 zip 包。解压后可以是 `faster-whisper/ctranslate2` 模型目录，也可以是已导出的 OpenVINO Whisper 模型目录。目录名仅允许字母数字与 `._-`。
        </div>
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
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

      <div className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-xs text-slate-600">
        <div className="font-semibold text-slate-700">如何在任务里使用本地模型？</div>
        <div className="mt-2">
          在任务详情页生成字幕时，可将 `asr_engine` 设为 `faster-whisper` 或 `openvino`；`asr_model` 支持模型目录路径（例如：`/models/whisper/tiny` 或 `/models/whisper/whisper-large-v3-ov`）。
        </div>
      </div>
      <SettingsSaveBar
        dirty={isDefaultsDirty}
        busy={busy}
        onSave={saveDefaults}
        onDiscard={discardDefaults}
        saveLabel="保存默认 ASR"
        dirtyLabel="有未保存的 ASR 默认配置"
      />
    </div>
  );
}
