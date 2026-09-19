import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, DataTable, PageHeader, Section } from "../components/ui";
import { fetchJson } from "../lib/http";
import { ORCHESTRATOR_URL } from "../lib/urls";

type AIModelStat = {
  provider: string;
  model: string;
  requests: number;
  failures: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd?: number | null;
};

type AIStatusStat = { status_code?: number | null; count: number };

type AIUsageSummary = {
  hours: number;
  requests: number;
  successes: number;
  failures: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  priced_requests: number;
  estimated_cost_usd?: number | null;
  by_model: AIModelStat[];
  by_status: AIStatusStat[];
};

type Pricing = {
  models: Record<string, { input_per_million_usd: number; output_per_million_usd: number }>;
};

type AlertItem = {
  id: string;
  fingerprint: string;
  source: string;
  severity: string;
  status: string;
  title: string;
  message: string;
  details: Record<string, unknown>;
  occurrence_count: number;
  first_seen_at: string;
  last_seen_at: string;
  acknowledged_at?: string | null;
  resolved_at?: string | null;
};

function formatTokens(value: number) {
  return new Intl.NumberFormat().format(value || 0);
}

function severityClass(value: string) {
  if (value === "critical") return "border-rose-200 bg-rose-50 text-rose-800";
  if (value === "warning") return "border-amber-200 bg-amber-50 text-amber-800";
  return "border-sky-200 bg-sky-50 text-sky-800";
}

export default function OperationsPage() {
  const [hours, setHours] = useState(24);
  const [usage, setUsage] = useState<AIUsageSummary | null>(null);
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [pricingText, setPricingText] = useState("{}");
  const [pricingDirty, setPricingDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [nextUsage, activeAlerts, resolvedAlerts, pricing] = await Promise.all([
        fetchJson<AIUsageSummary>(`${ORCHESTRATOR_URL}/operations/ai-usage?hours=${hours}&recent_limit=25`),
        fetchJson<AlertItem[]>(`${ORCHESTRATOR_URL}/operations/alerts?status=active&limit=1000`),
        fetchJson<AlertItem[]>(`${ORCHESTRATOR_URL}/operations/alerts?status=resolved&limit=20`),
        fetchJson<Pricing>(`${ORCHESTRATOR_URL}/operations/ai-usage/pricing`),
      ]);
      setUsage(nextUsage);
      setAlerts([...activeAlerts, ...resolvedAlerts]);
      if (!pricingDirty) setPricingText(JSON.stringify(pricing.models ?? {}, null, 2));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [hours, pricingDirty]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function scanAlerts() {
    setBusy("scan");
    setError(null);
    try {
      await fetchJson(`${ORCHESTRATOR_URL}/operations/alerts/scan`, { method: "POST" });
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function setAlertStatus(id: string, action: "ack" | "resolve") {
    setBusy(id);
    setError(null);
    try {
      await fetchJson(`${ORCHESTRATOR_URL}/operations/alerts/${id}/${action}`, { method: "POST" });
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  async function savePricing() {
    setBusy("pricing");
    setError(null);
    try {
      const parsed = JSON.parse(pricingText || "{}") as Pricing["models"];
      await fetchJson<Pricing>(`${ORCHESTRATOR_URL}/operations/ai-usage/pricing`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: parsed }),
      });
      setPricingDirty(false);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const openAlerts = useMemo(() => alerts.filter((item) => item.status !== "resolved"), [alerts]);
  const resolvedAlerts = useMemo(() => alerts.filter((item) => item.status === "resolved").slice(0, 20), [alerts]);

  return (
    <div className="space-y-4">
      <PageHeader
        title="运维中心"
        description="统一查看 AI 用量与错误、直播/队列/存储/账号告警，并维护按模型 Token 价格。后台每 60 秒自动扫描一次告警。"
        actions={
          <>
            <select className="vr-input" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              <option value={1}>最近 1 小时</option>
              <option value={24}>最近 24 小时</option>
              <option value={168}>最近 7 天</option>
              <option value={720}>最近 30 天</option>
            </select>
            <Button disabled={busy === "scan"} onClick={scanAlerts}>{busy === "scan" ? "扫描中..." : "立即扫描"}</Button>
            <Button onClick={() => refresh()}>刷新</Button>
          </>
        }
      />

      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        {[
          ["AI 请求", usage?.requests ?? 0],
          ["失败", usage?.failures ?? 0],
          ["输入 Token", formatTokens(usage?.input_tokens ?? 0)],
          ["输出 Token", formatTokens(usage?.output_tokens ?? 0)],
          ["估算成本", usage?.estimated_cost_usd == null ? "未配置价格" : `$${usage.estimated_cost_usd.toFixed(4)}`],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-xl border border-slate-200 bg-white p-4">
            <div className="text-xs text-slate-500">{label}</div>
            <div className="mt-1 text-xl font-semibold text-slate-950">{value}</div>
          </div>
        ))}
      </div>

      <Section>
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">当前告警</div>
            <div className="mt-1 text-xs text-slate-500">自动信号：磁盘、AI 402/429/5xx、Worker heartbeat、账号检查；ffplayout 的 RTMP/RTMPS 输出失败由输出链路直接上报并在恢复后自动解决。</div>
          </div>
          <div className="text-sm text-slate-500">{openAlerts.length} 条未解决</div>
        </div>
        {openAlerts.length === 0 ? <div className="mt-4 text-sm text-emerald-700">当前没有未解决告警。</div> : (
          <div className="mt-4 space-y-2">
            {openAlerts.map((alert) => (
              <div key={alert.id} className="rounded-lg border border-slate-200 p-3">
                <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded border px-2 py-0.5 text-xs ${severityClass(alert.severity)}`}>{alert.severity}</span>
                      <span className="text-xs text-slate-500">{alert.source}</span>
                      <span className="text-xs text-slate-400">×{alert.occurrence_count}</span>
                    </div>
                    <div className="mt-2 font-medium text-slate-950">{alert.title}</div>
                    <div className="mt-1 text-sm text-slate-600">{alert.message || "-"}</div>
                    <div className="mt-1 text-xs text-slate-400">最后出现：{new Date(alert.last_seen_at).toLocaleString()}</div>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {alert.status === "open" ? (
                      <Button size="xs" disabled={busy === alert.id} onClick={() => setAlertStatus(alert.id, "ack")}>确认</Button>
                    ) : null}
                    <Button size="xs" tone="primary" disabled={busy === alert.id} onClick={() => setAlertStatus(alert.id, "resolve")}>解决</Button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section>
        <div className="text-sm font-semibold">AI 模型用量</div>
        <DataTable wrapClassName="mt-3">
          <thead>
            <tr>
              <th>Provider / Model</th>
              <th>请求</th>
              <th>失败</th>
              <th>Input</th>
              <th>Output</th>
              <th>成本</th>
            </tr>
          </thead>
          <tbody>
            {(usage?.by_model ?? []).map((item) => (
              <tr key={`${item.provider}:${item.model}`}>
                <td><div className="font-medium">{item.model || "-"}</div><div className="text-xs text-slate-500">{item.provider}</div></td>
                <td>{item.requests}</td>
                <td>{item.failures}</td>
                <td>{formatTokens(item.input_tokens)}</td>
                <td>{formatTokens(item.output_tokens)}</td>
                <td>{item.estimated_cost_usd == null ? "-" : `$${item.estimated_cost_usd.toFixed(4)}`}</td>
              </tr>
            ))}
            {(usage?.by_model ?? []).length === 0 ? <tr><td colSpan={6} className="text-slate-500">暂无新版本采集的 AI 请求。</td></tr> : null}
          </tbody>
        </DataTable>
        {(usage?.by_status ?? []).length > 0 ? (
          <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-600">
            {(usage?.by_status ?? []).map((item) => (
              <span key={String(item.status_code)} className="rounded border border-slate-200 px-2 py-1">
                HTTP {item.status_code ?? "transport"}: {item.count}
              </span>
            ))}
          </div>
        ) : null}
      </Section>

      <Section>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="text-sm font-semibold">Token 价格表</div>
            <div className="mt-1 text-xs text-slate-500">
              单位为 USD / 1M tokens。Key 可写 model 或 provider:model；未配置价格的请求仍统计 Token，但成本显示为空。
            </div>
          </div>
          <Button tone="primary" disabled={busy === "pricing"} onClick={savePricing}>{busy === "pricing" ? "保存中..." : "保存价格"}</Button>
        </div>
        <textarea
          className="vr-input mt-3 min-h-44 w-full font-mono text-xs"
          value={pricingText}
          onChange={(e) => {
            setPricingText(e.target.value);
            setPricingDirty(true);
          }}
          placeholder={'{"gpt-4o-mini":{"input_per_million_usd":0.15,"output_per_million_usd":0.60}}'}
        />
      </Section>

      {resolvedAlerts.length > 0 ? (
        <Section>
          <div className="text-sm font-semibold">最近已解决告警</div>
          <div className="mt-3 space-y-1 text-sm">
            {resolvedAlerts.map((alert) => (
              <div key={alert.id} className="flex flex-col justify-between gap-1 border-t border-slate-100 py-2 first:border-0 sm:flex-row">
                <span>{alert.title}</span>
                <span className="text-xs text-slate-400">{new Date(alert.last_seen_at).toLocaleString()}</span>
              </div>
            ))}
          </div>
        </Section>
      ) : null}
    </div>
  );
}
