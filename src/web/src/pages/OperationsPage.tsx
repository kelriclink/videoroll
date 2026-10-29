import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Button, DataTable, InlineNotice, PageHeader, Section, TechnicalDetails } from "../components/ui";
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
        title="运维"
        description="处理系统告警并查看 AI 用量。工作流、HTTP 状态和原始运行数据集中在高级诊断中。"
        actions={
          <>
            <Link
              to="/workflow"
              className="rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-700 hover:bg-white dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-900"
            >
              工作流诊断
            </Link>
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

      {error ? (
        <InlineNotice title="运维数据未能完整刷新" tone="danger">
          <TechnicalDetails className="mt-2">
            <pre className="whitespace-pre-wrap break-words font-mono">{error}</pre>
          </TechnicalDetails>
        </InlineNotice>
      ) : null}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        {[
          ["AI 请求", usage?.requests ?? 0],
          ["失败", usage?.failures ?? 0],
          ["输入 Token", formatTokens(usage?.input_tokens ?? 0)],
          ["输出 Token", formatTokens(usage?.output_tokens ?? 0)],
          ["估算成本", usage?.estimated_cost_usd == null ? "未配置价格" : `$${usage.estimated_cost_usd.toFixed(4)}`],
        ].map(([label, value]) => (
          <div key={String(label)} className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-950">
            <div className="text-xs text-slate-500">{label}</div>
            <div className="mt-1 text-xl font-semibold text-slate-950 dark:text-white">{value}</div>
          </div>
        ))}
      </div>

      <Section>
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold text-slate-950 dark:text-white">当前告警</div>
            <div className="mt-1 text-xs text-slate-500">磁盘、AI、渲染节点、账号和生产流程异常会汇总到这里；后台每 60 秒自动扫描。</div>
          </div>
          <div className="text-sm text-slate-500">{openAlerts.length} 条未解决</div>
        </div>
        {openAlerts.length === 0 ? <div className="mt-4 text-sm text-emerald-700">当前没有未解决告警。</div> : (
          <div className="mt-4 space-y-2">
            {openAlerts.map((alert) => (
              <div key={alert.id} className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`rounded border px-2 py-0.5 text-xs ${severityClass(alert.severity)}`}>{alert.severity}</span>
                      <span className="text-xs text-slate-500">{alert.source}</span>
                      <span className="text-xs text-slate-400">×{alert.occurrence_count}</span>
                    </div>
                    <div className="mt-2 font-medium text-slate-950 dark:text-white">{alert.title}</div>
                    <div className="mt-1 text-sm text-slate-600 dark:text-slate-300">{alert.message || "-"}</div>
                    {(typeof alert.details?.reason === "string" && alert.details.reason)
                      || (typeof alert.details?.pipeline === "string" && alert.details.pipeline) ? (
                      <TechnicalDetails className="mt-2" summary="诊断详情">
                        {typeof alert.details?.reason === "string" && alert.details.reason ? (
                          <div className="break-words"><span className="text-slate-500">原因：</span>{alert.details.reason}</div>
                        ) : null}
                        {typeof alert.details?.pipeline === "string" && alert.details.pipeline ? (
                          <div className="mt-1 break-words"><span className="text-slate-500">执行路径：</span>{alert.details.pipeline}</div>
                        ) : null}
                      </TechnicalDetails>
                    ) : null}
                    {typeof alert.details?.task_id === "string" && alert.details.task_id ? (
                      <Link className="mt-1 inline-block text-xs text-sky-700 underline" to={`/tasks/${alert.details.task_id}`}>查看关联任务</Link>
                    ) : null}
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
              <th>模型服务 / 模型</th>
              <th>请求</th>
              <th>失败</th>
              <th>输入 Token</th>
              <th>输出 Token</th>
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
          <TechnicalDetails className="mt-3" summary="HTTP / 传输状态">
            <div className="flex flex-wrap gap-2">
              {(usage?.by_status ?? []).map((item) => (
                <span key={String(item.status_code)} className="rounded border border-slate-200 px-2 py-1 dark:border-slate-700">
                  {item.status_code ?? "传输失败"}：{item.count}
                </span>
              ))}
            </div>
          </TechnicalDetails>
        ) : null}
      </Section>

      <TechnicalDetails summary="高级：模型价格配置">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="text-xs text-slate-500">
            单位为 USD / 1M tokens。键可以写 model 或 provider:model；只影响成本估算，不影响模型调用。
          </div>
          <Button tone="primary" size="xs" disabled={busy === "pricing" || !pricingDirty} onClick={savePricing}>
            {busy === "pricing" ? "保存中..." : "保存价格"}
          </Button>
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
      </TechnicalDetails>

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
