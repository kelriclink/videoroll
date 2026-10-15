import { useCallback, useEffect, useState } from "react";
import { fetchJson } from "../lib/http";
import { orchestratorUrl } from "../lib/urls";

type NativeStatus = {
  engine: string; installed: boolean; enabled: boolean; accepting_jobs: boolean;
  active_jobs: number; busy: boolean; mode: string; resident: boolean;
  gpu_loaded: boolean | null; message: string;
};
type Summary = { native: NativeStatus; worker: { state: string } };

export default function SettingsDubbingPage() {
  const [state, setState] = useState<Summary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const value = await fetchJson<Summary>(orchestratorUrl("/settings/dubbing"));
      setState(value);
    } catch (reason) { setError(String(reason)); }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function switchEngine(enabled: boolean) {
    setBusy(true); setError(null); setMessage(null);
    try {
      await fetchJson(orchestratorUrl("/settings/dubbing/native"), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      setMessage(enabled ?
        "已启用 YouDub：任务开始时加载 VoxCPM2，完成后自动释放显存。" :
        "已停用 YouDub：不再接受新的配音任务。");
      await refresh();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  async function switchResidency(resident: boolean) {
    setBusy(true); setError(null); setMessage(null);
    try {
      await fetchJson(orchestratorUrl("/settings/dubbing/native"), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: true, resident }),
      });
      setMessage(resident ?
        "已请求驻留：正在独立进程预热 VoxCPM2；加载完成后显示显存已驻留。" :
        "已关闭驻留，模型使用完后不保留在 GPU。 ");
      await refresh();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  const native = state?.native;
  return <section className="space-y-5">
    <div>
      <h1 className="text-xl font-semibold">中文配音 · YouDub 引擎管理</h1>
      <p className="mt-1 text-sm text-slate-600">
        仅使用 YouDub 原生 Demucs、VoxCPM2、语音对齐、背景音乐混音与 MP4 合成。
        已下线 Qwen/CosyVoice 旧配音管线。
      </p>
    </div>
    <div className="rounded border bg-white p-4 space-y-4">
      <h2 className="font-semibold">YouDub · VoxCPM2</h2>
      <div className="grid gap-3 text-sm md:grid-cols-3">
        <span>模型：{native?.installed ? "已安装" : "尚未就绪"}</span>
        <span>控制：{native?.enabled ? "已启用（按需加载）" : "已停用"}</span>
        <span>任务：{native?.active_jobs ?? 0} 项正在执行或排队</span>
        <span>Hatchet Worker：{state?.worker.state === "active" ? "运行中" : "未运行"}</span>
        <span>显存：{native?.gpu_loaded === true ? "已驻留" : native?.resident ? "正在预热 / 执行中" : native?.busy ? "任务使用中" : "按需释放"}</span>
        <span>引擎：YouDub 原生独立环境</span>
      </div>
      <p className="text-xs text-slate-600">{native?.message ?? "正在检查引擎运行状态…"}</p>
      <div className="flex flex-wrap gap-3">
        <button type="button" onClick={() => void switchEngine(true)}
          disabled={busy || !native?.installed || native?.enabled}
          className="rounded bg-sky-800 px-4 py-2 text-sm text-white disabled:opacity-50">启用 YouDub 配音</button>
        <button type="button" onClick={() => void switchEngine(false)}
          disabled={busy || !native?.enabled || (native?.active_jobs ?? 0) > 0}
          className="rounded border px-4 py-2 text-sm disabled:opacity-50">停用 YouDub 配音</button>
        <button type="button" disabled={busy} onClick={() => void refresh()}
          className="rounded border px-4 py-2 text-sm">刷新</button>
      </div>
      <div className="rounded border border-slate-200 p-3 space-y-2">
        <div className="text-sm font-medium">VoxCPM2 显存驻留</div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={Boolean(native?.resident)}
            disabled={busy || !native?.enabled || (native?.active_jobs ?? 0) > 0}
            onChange={(e) => void switchResidency(e.target.checked)}/>
          任务结束后继续保留模型在显存中（加速下一条配音）
        </label>
        <p className="text-xs text-slate-500">
          默认关闭。开启后以独立进程真正驻留并跨任务复用 VoxCPM2；
          为避免与 Qwen/其他推理冲突，需要至少约 12 GiB 可用显存，否则会拒绝开启。
          有排队或运行任务时禁止切换；驻留进程异常退出后，新任务会尝试恢复。
        </p>
      </div>
      <p className="text-xs text-slate-500">
        配音服务“启用”与“显存驻留”独立控制。未开启驻留时任务结束释放 GPU；
        为保护批量处理，排队或运行中不能切换驻留状态或停用引擎。
      </p>
      {error && <p role="alert" className="break-all text-sm text-red-700">{error}</p>}
      {message && <p className="text-sm text-emerald-700">{message}</p>}
    </div>
  </section>;
}
