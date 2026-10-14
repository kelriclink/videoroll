import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

export type ModelState = {
  model: string;
  installed: boolean;
  runtime_ready?: boolean;
  status: string;
  bytes: number;
  source: string;
  message: string;
};

export type DubbingInventory = {
  models: ModelState[];
  selection: { model: string; voice: string; device: string };
  model_root: string;
  voices: string[];
  devices: string[];
  sources: string[];
  tokenizer_installed: boolean;
  voice_profiles?: Array<{ id: string; name: string; duration_seconds: number }>;
  engines?: string[];
};

const api = orchestratorUrl("/settings/dubbing");
const labels: Record<string, string> = {
  not_downloaded: "未下载",
  queued: "排队中",
  downloading: "正在下载",
  interrupted: "中断，可继续",
  failed: "下载失败，可重试",
  installed: "已安装",
};

function sizeLabel(bytes: number): string {
  if (!bytes) return "0 MB";
  return (bytes / 1024 ** 2).toFixed(1) + " MB";
}

export default function SettingsDubbingPage() {
  const [inventory, setInventory] = useState<DubbingInventory | null>(null);
  const [model, setModel] = useState("");
  const [voice, setVoice] = useState("Vivian");
  const [device, setDevice] = useState("cuda:0");
  const [source, setSource] = useState("huggingface");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (syncSelection = false) => {
    try {
      const result = await fetchJson<DubbingInventory>(api);
      setInventory(result);
      if (syncSelection) {
        setModel(result.selection.model);
        setVoice(result.selection.voice);
        setDevice(result.selection.device);
      }
      setError(null);
    } catch (reason) {
      setError(String(reason));
    }
  }, []);

  useEffect(() => {
    void refresh(true);
    const interval = window.setInterval(() => void refresh(false), 3500);
    return () => window.clearInterval(interval);
  }, [refresh]);

  async function install(repo: string) {
    setBusy(repo);
    setError(null);
    setMessage(null);
    try {
      await fetchJson(orchestratorUrl("/settings/dubbing/models/download"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: repo, source }),
      });
      setMessage("已提交下载任务，页面会自动更新状态。中途断开可重新点击继续下载。");
      await refresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    setBusy("save");
    setError(null);
    setMessage(null);
    try {
      const result = await fetchJson<DubbingInventory>(api, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, voice, device }),
      });
      setInventory(result);
      setMessage("默认推理配置已保存，新建的手动配音任务将使用此配置。");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(null);
    }
  }

  const selectedInstalled = inventory?.models.some((item) => item.model === model && item.installed) ?? false;

  return (
    <section className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold">中文配音 · 模型管理</h1>
        <p className="mt-1 text-sm text-slate-600">
          下载和管理 Qwen3-TTS 或 CosyVoice 3 Base/RL。CosyVoice 需先安装独立 Python 3.10 推理环境，并从视频提取授权参考声音。
        </p>
      </div>
      <div className="rounded border bg-white p-4">
        <h2 className="font-semibold">模型下载安装</h2>
        <div className="mt-3 flex flex-wrap gap-3 items-center">
          <label className="text-sm">
            下载源
            <select className="ml-2 rounded border px-3 py-2" value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="huggingface">Hugging Face</option>
              <option value="modelscope">ModelScope（国内网络推荐）</option>
            </select>
          </label>
          <button type="button" onClick={() => void refresh()} className="rounded border px-3 py-2 text-sm">刷新状态</button>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          目录：{inventory?.model_root ?? "读取中…"}。Qwen 首次下载包含 Tokenizer；CosyVoice 3 的 Base/RL 共用一份官方模型目录。
          已完成的文件将在中断后复用。
        </p>
        <div className="mt-4 space-y-3">
          {(inventory?.models ?? []).map((item) => {
            const downloading = item.status === "downloading" || item.status === "queued";
            return (
              <div key={item.model} className="rounded border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="font-medium">{item.model === "cosyvoice3:base" ? "CosyVoice 3 0.5B · Base · 声音克隆" : item.model === "cosyvoice3:rl" ? "CosyVoice 3 0.5B · RL · 发音准确性优先" : item.model.includes("0.6B") ? "Qwen3-TTS CustomVoice 0.6B" : "Qwen3-TTS CustomVoice 1.7B"}</div>
                    <div className="text-xs text-slate-500">{item.model}</div>
                  </div>
                  <span className="text-sm">{labels[item.status] ?? item.status}</span>
                </div>
                <div className="mt-2 text-xs text-slate-600">
                  已存储 {sizeLabel(item.bytes)} · {item.message || "尚无下载记录"}{item.runtime_ready === false ? " · 推理环境尚未就绪" : ""}
                </div>
                <button type="button" disabled={item.installed || downloading || busy !== null}
                  onClick={() => void install(item.model)}
                  className="mt-3 rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50">
                  {item.installed ? "已安装" : downloading ? "下载中…" : item.status === "failed" || item.status === "interrupted" ? "继续下载" : "下载模型"}
                </button>
              </div>
            );
          })}
        </div>
        {inventory?.tokenizer_installed ? <p className="mt-2 text-xs text-green-700">TTS Tokenizer 已安装</p> : null}
      </div>
      <div className="rounded border bg-white p-4">
        <h2 className="font-semibold">默认推理配置</h2>
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <label className="text-sm">模型
            <select className="mt-1 block w-full rounded border p-2" value={model} onChange={(e) => {
              setModel(e.target.value);
              setVoice(e.target.value.startsWith("cosyvoice3:") ? "reference" : "Vivian");
              if (e.target.value.startsWith("cosyvoice3:")) setDevice("cuda:0");
            }}>
              {(inventory?.models ?? []).map((item) => (
                <option key={item.model} value={item.model}>
                  {item.model === "cosyvoice3:base" ? "CosyVoice 3 Base" : item.model === "cosyvoice3:rl" ? "CosyVoice 3 RL" : item.model.includes("0.6B") ? "Qwen 0.6B" : "Qwen 1.7B"} {item.installed ? "（已安装）" : "（未下载）"}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">中文音色
            <select className="mt-1 block w-full rounded border p-2" value={voice} onChange={(e) => setVoice(e.target.value)}>
              {model.startsWith("cosyvoice3:") ? <option value="reference">从任务视频提取参考音色（在任务详情中选择）</option> : (inventory?.voices ?? []).map((v) => <option key={v}>{v}</option>)}
            </select>
          </label>
          <label className="text-sm">推理设备
            <select className="mt-1 block w-full rounded border p-2" value={device} onChange={(e) => setDevice(e.target.value)}>
              {(inventory?.devices ?? []).map((d) => <option key={d}>{d}</option>)}
            </select>
          </label>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          Qwen V100 使用 FP16 + SDPA；CosyVoice 3 首次测试优先 PyTorch FP32，确认稳定后再测试 FP16。
          保存配置不会安装推理依赖、下载参考声音或自动执行任何配音任务。
        </p>
        <div className="mt-3 flex items-center gap-3">
          <button type="button" disabled={!selectedInstalled || busy !== null || (model.startsWith("cosyvoice3:") && !inventory?.models.find((v) => v.model === model)?.runtime_ready)}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-4 py-2 text-sm text-white disabled:opacity-50">
            保存默认推理配置
          </button>
          <Link to="/tasks" className="text-sm text-sky-700 underline">前往任务管理</Link>
        </div>
        {!selectedInstalled && inventory ? <p className="mt-2 text-xs text-amber-700">请先下载所选模型再保存。</p> : null}
      </div>
      {message ? <p className="text-sm text-green-700">{message}</p> : null}
      {error ? <p role="alert" className="break-words text-sm text-red-700">{error}</p> : null}
    </section>
  );
}
