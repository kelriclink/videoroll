import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useConfirm } from "../components/feedbackContext";
import { Button, DataTable, EmptyState, PageHeader, Section } from "../components/ui";
import { fetchJson } from "../lib/http";
import { SUBTITLE_SERVICE_URL } from "../lib/urls";

type KnowledgeItem = {
  id: string;
  item_type: string;
  term: string;
  translation: string;
  target_lang: string;
  domain: string;
  aliases: unknown[];
  title: string;
  content: string;
  description: string;
  sources: unknown[];
  confidence: number;
  status: string;
  created_by: string;
  usage_count: number;
  embedding_model: string;
  updated_at: string;
};

function formatDate(value: string) {
  return value ? new Date(value).toLocaleString() : "-";
}

function statusClass(status: string) {
  if (status === "auto_approved" || status === "approved") return "border-emerald-200 bg-emerald-50 text-emerald-800";
  if (status === "pending") return "border-amber-200 bg-amber-50 text-amber-800";
  if (status === "archived") return "border-slate-200 bg-slate-50 text-slate-600";
  return "border-slate-200 bg-slate-50 text-slate-700";
}

export default function KnowledgeBasePage() {
  const confirm = useConfirm();
  const [items, setItems] = useState<KnowledgeItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [itemTypeFilter, setItemTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [searchText, setSearchText] = useState("");
  const [domainFilter, setDomainFilter] = useState("");
  const [page, setPage] = useState(0);
  const pageSize = 50;

  const [itemType, setItemType] = useState<"term" | "document">("term");
  const [targetLang, setTargetLang] = useState("zh");
  const [term, setTerm] = useState("");
  const [translation, setTranslation] = useState("");
  const [domain, setDomain] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [status, setStatus] = useState("approved");
  const [confidence, setConfidence] = useState(1);
  const [formError, setFormError] = useState<string | null>(null);

  const query = useMemo(() => {
    const params = new URLSearchParams();
    params.set("limit", String(pageSize));
    params.set("offset", String(page * pageSize));
    if (itemTypeFilter) params.set("item_type", itemTypeFilter);
    if (statusFilter) params.set("status", statusFilter);
    if (searchText.trim()) params.set("q", searchText.trim());
    if (domainFilter.trim()) params.set("domain", domainFilter.trim());
    return params.toString();
  }, [domainFilter, itemTypeFilter, page, statusFilter, searchText]);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const rows = await fetchJson<KnowledgeItem[]>(`${SUBTITLE_SERVICE_URL}/subtitle/knowledge/items?${query}`);
      setItems(rows);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [query]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    setPage(0);
  }, [domainFilter, itemTypeFilter, statusFilter, searchText]);

  function validateForm(): string | null {
    if (!targetLang.trim()) return "target_lang 不能为空";
    if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) return "confidence 必须在 0 到 1 之间";
    if (itemType === "term") {
      if (!term.trim()) return "term 不能为空";
      if (!translation.trim()) return "translation 不能为空";
    }
    if (itemType === "document" && !title.trim() && !content.trim()) return "document 至少需要 title 或 content";
    return null;
  }

  async function saveItem() {
    const validationError = validateForm();
    if (validationError) {
      setFormError(validationError);
      return;
    }
    setBusy(true);
    setError(null);
    setFormError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/knowledge/items`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          item_type: itemType,
          target_lang: targetLang,
          term,
          translation,
          domain,
          title,
          content,
          description,
          confidence,
          status,
          created_by: "manual",
        }),
      });
      setTerm("");
      setTranslation("");
      setTitle("");
      setDescription("");
      setContent("");
      setConfidence(1);
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteItem(item: KnowledgeItem) {
    const ok = await confirm({
      title: "删除知识条目",
      message: `确定删除「${item.term || item.title || item.id}」吗？此操作不会删除任务，只会移除知识库条目。`,
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/knowledge/items/${item.id}`, { method: "DELETE" });
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Knowledge Base"
        description="管理翻译 RAG 使用的术语和文档。基础词、局部变量和一次性表达建议删除或保持未批准。"
        actions={
          <>
            <Link to="/settings/translate" className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-800 hover:bg-slate-50">
              RAG 设置
            </Link>
            <Button onClick={refresh}>刷新</Button>
          </>
        }
      />
      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}
      {formError ? <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">{formError}</div> : null}

      <Section>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-slate-900">新增条目</div>
            <div className="mt-1 text-xs text-slate-500">手动添加的条目会直接生成向量；如果 embedding 配置不可用，术语仍可保存但不会参与向量检索。</div>
          </div>
          <div className="flex flex-wrap gap-2">
            <select className="vr-input" value={itemType} onChange={(e) => setItemType(e.target.value as "term" | "document")}>
              <option value="term">term</option>
              <option value="document">document</option>
            </select>
            <select className="vr-input" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="approved">approved</option>
              <option value="pending">pending</option>
              <option value="auto_approved">auto_approved</option>
              <option value="archived">archived</option>
            </select>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">target_lang</div>
            <input className="vr-input w-full" value={targetLang} onChange={(e) => setTargetLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">domain</div>
            <input className="vr-input w-full" value={domain} onChange={(e) => setDomain(e.target.value)} />
          </label>
          {itemType === "term" ? (
            <>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">term</div>
                <input className="vr-input w-full" value={term} onChange={(e) => setTerm(e.target.value)} />
              </label>
              <label className="block">
                <div className="mb-1 text-xs text-slate-600">translation</div>
                <input className="vr-input w-full" value={translation} onChange={(e) => setTranslation(e.target.value)} />
              </label>
            </>
          ) : (
            <label className="block md:col-span-2">
              <div className="mb-1 text-xs text-slate-600">title</div>
              <input className="vr-input w-full" value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
          )}
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">confidence</div>
            <input type="number" min={0} max={1} step="0.01" className="vr-input w-full" value={confidence} onChange={(e) => setConfidence(parseFloat(e.target.value || "1"))} />
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">description</div>
            <textarea className="vr-input h-20 w-full" value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">content</div>
            <textarea className="vr-input h-28 w-full" value={content} onChange={(e) => setContent(e.target.value)} />
          </label>
        </div>
        <div className="mt-3">
          <Button tone="primary" disabled={busy} onClick={saveItem}>{busy ? "保存中..." : "添加到知识库"}</Button>
        </div>
      </Section>

      <Section>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-slate-900">知识条目</div>
            <div className="mt-1 text-xs text-slate-500">第 {page + 1} 页，每页 {pageSize} 条，当前 {items.length} 条。</div>
          </div>
          <div className="flex flex-wrap gap-2">
            <input className="vr-input" placeholder="搜索术语、译文、说明" value={searchText} onChange={(e) => setSearchText(e.target.value)} />
            <input className="vr-input" placeholder="domain" value={domainFilter} onChange={(e) => setDomainFilter(e.target.value)} />
            <select className="vr-input" value={itemTypeFilter} onChange={(e) => setItemTypeFilter(e.target.value)}>
              <option value="">全部类型</option>
              <option value="term">term</option>
              <option value="document">document</option>
            </select>
            <select className="vr-input" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">全部状态</option>
              <option value="approved">approved</option>
              <option value="auto_approved">auto_approved</option>
              <option value="pending">pending</option>
              <option value="archived">archived</option>
            </select>
            <Button
              disabled={!searchText && !domainFilter && !itemTypeFilter && !statusFilter}
              onClick={() => {
                setSearchText("");
                setDomainFilter("");
                setItemTypeFilter("");
                setStatusFilter("");
              }}
            >
              清空筛选
            </Button>
          </div>
        </div>

        {items.length === 0 ? (
          <EmptyState>暂无知识条目</EmptyState>
        ) : (
          <DataTable wrapClassName="max-h-[34rem]">
            <thead>
              <tr>
                <th className="py-2 pr-3 text-left">Type</th>
                <th className="py-2 pr-3 text-left">Term / Title</th>
                <th className="py-2 pr-3 text-left">Translation</th>
                <th className="py-2 pr-3 text-left">Domain</th>
                <th className="py-2 pr-3 text-left">Status</th>
                <th className="py-2 pr-3 text-left">Used</th>
                <th className="py-2 pr-3 text-left">Updated</th>
                <th className="py-2 pr-3 text-left">Action</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td className="py-2 pr-3 align-top">{item.item_type}</td>
                  <td className="max-w-56 py-2 pr-3 align-top">
                    <div className="font-medium text-slate-900">{item.term || item.title || "-"}</div>
                    {item.description ? <div className="mt-1 line-clamp-2 text-xs text-slate-500">{item.description}</div> : null}
                  </td>
                  <td className="max-w-48 py-2 pr-3 align-top">{item.translation || "-"}</td>
                  <td className="py-2 pr-3 align-top">{item.domain || "-"}</td>
                  <td className="py-2 pr-3 align-top">
                    <span className={`rounded border px-2 py-0.5 text-xs ${statusClass(item.status)}`}>{item.status}</span>
                  </td>
                  <td className="py-2 pr-3 align-top">{item.usage_count}</td>
                  <td className="py-2 pr-3 align-top text-xs text-slate-500">{formatDate(item.updated_at)}</td>
                  <td className="py-2 pr-3 align-top">
                    <Button size="xs" tone="danger" disabled={busy} onClick={() => deleteItem(item)}>删除</Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
        <div className="mt-3 flex items-center justify-between gap-3">
          <Button disabled={page === 0 || busy} onClick={() => setPage((value) => Math.max(0, value - 1))}>上一页</Button>
          <div className="text-xs text-slate-500">offset {page * pageSize}</div>
          <Button disabled={items.length < pageSize || busy} onClick={() => setPage((value) => value + 1)}>下一页</Button>
        </div>
      </Section>
    </div>
  );
}
