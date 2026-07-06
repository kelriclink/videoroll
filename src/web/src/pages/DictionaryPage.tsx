import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useConfirm } from "../components/feedbackContext";
import { Button, DataTable, EmptyState, PageHeader, Section } from "../components/ui";
import { fetchJson } from "../lib/http";
import { SUBTITLE_SERVICE_URL } from "../lib/urls";

type DictionarySource = {
  id: string;
  name: string;
  slug: string;
  source_lang: string;
  target_lang: string;
  format: string;
  license: string;
  source_url: string;
  version: string;
  domain: string;
  priority: number;
  enabled: boolean;
  entry_count: number;
  updated_at: string;
};

type DictionaryEntry = {
  id: string;
  source_id: string;
  source_name: string;
  source_lang: string;
  target_lang: string;
  term: string;
  translations: string[];
  translation: string;
  pos: string;
  definition: string;
  domain: string;
  quality: number;
  enabled: boolean;
  usage_count: number;
  license: string;
  source_url: string;
  updated_at: string;
};

type DictionaryImportResponse = {
  source_id: string;
  batch_id: string;
  status: string;
  parsed: number;
  upserted: number;
  skipped: number;
  max_entries: number;
  full_import: boolean;
  sha256: string;
};

type DictionaryImportPreset = {
  key: string;
  label: string;
  name: string;
  slug: string;
  description: string;
  source_lang: string;
  target_lang: string;
  format_name: string;
  license: string;
  license_url: string;
  source_url: string;
  version: string;
  domain: string;
  priority: number;
  recommended_full_import: boolean;
  recommended_max_entries: number;
};

function formatDate(value: string) {
  return value ? new Date(value).toLocaleString() : "-";
}

function formatTranslations(entry: DictionaryEntry) {
  return (entry.translations || []).slice(0, 4).join(" / ") || entry.translation || "-";
}

export default function DictionaryPage() {
  const confirm = useConfirm();
  const [sources, setSources] = useState<DictionarySource[]>([]);
  const [entries, setEntries] = useState<DictionaryEntry[]>([]);
  const [importPresets, setImportPresets] = useState<DictionaryImportPreset[]>([]);
  const [lookupResults, setLookupResults] = useState<DictionaryEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [dictionaryPreset, setDictionaryPreset] = useState("");
  const [name, setName] = useState("");
  const [formatName, setFormatName] = useState("auto");
  const [sourceLang, setSourceLang] = useState("en");
  const [targetLang, setTargetLang] = useState("zh");
  const [domain, setDomain] = useState("");
  const [license, setLicense] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [version, setVersion] = useState("");
  const [priority, setPriority] = useState(0);
  const [importMode, setImportMode] = useState("upsert");
  const [maxEntries, setMaxEntries] = useState(250000);
  const [fullImport, setFullImport] = useState(false);

  const [sourceFilter, setSourceFilter] = useState("");
  const [entrySearch, setEntrySearch] = useState("");
  const [entryTargetLang, setEntryTargetLang] = useState("zh");
  const [entryDomain, setEntryDomain] = useState("");
  const [entryPage, setEntryPage] = useState(0);
  const pageSize = 50;

  const [lookupTerm, setLookupTerm] = useState("");
  const [lookupExact, setLookupExact] = useState(true);

  const entryQuery = useMemo(() => {
    const params = new URLSearchParams();
    params.set("limit", String(pageSize));
    params.set("offset", String(entryPage * pageSize));
    if (sourceFilter) params.set("source_id", sourceFilter);
    if (entrySearch.trim()) params.set("q", entrySearch.trim());
    if (entryTargetLang.trim()) params.set("target_lang", entryTargetLang.trim());
    if (entryDomain.trim()) params.set("domain", entryDomain.trim());
    return params.toString();
  }, [entryDomain, entryPage, entrySearch, entryTargetLang, sourceFilter]);

  const refreshSources = useCallback(async () => {
    const rows = await fetchJson<DictionarySource[]>(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/sources`);
    setSources(rows);
  }, []);

  const refreshEntries = useCallback(async () => {
    const rows = await fetchJson<DictionaryEntry[]>(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/entries?${entryQuery}`);
    setEntries(rows);
  }, [entryQuery]);

  const refreshAll = useCallback(async () => {
    setError(null);
    try {
      await Promise.all([refreshSources(), refreshEntries()]);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshEntries, refreshSources]);

  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  useEffect(() => {
    fetchJson<DictionaryImportPreset[]>(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/import-presets`)
      .then(setImportPresets)
      .catch(() => setImportPresets([]));
  }, []);

  useEffect(() => {
    setEntryPage(0);
  }, [entryDomain, entrySearch, entryTargetLang, sourceFilter]);

  function applyImportPreset(value: string) {
    setDictionaryPreset(value);
    const preset = importPresets.find((item) => item.key === value);
    if (!preset) return;
    setName(preset.name || "");
    setFormatName(preset.format_name || "auto");
    setSourceLang(preset.source_lang || "");
    setTargetLang(preset.target_lang || "zh");
    setDomain(preset.domain || "");
    setLicense(preset.license || "");
    setSourceUrl(preset.source_url || "");
    setVersion(preset.version || "");
    setPriority(preset.priority || 0);
    setFullImport(Boolean(preset.recommended_full_import));
    setMaxEntries(preset.recommended_max_entries || 0);
  }

  async function importDictionary() {
    if (!file) {
      setError("请选择词典文件");
      return;
    }
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("dictionary_preset", dictionaryPreset);
      form.append("name", name.trim() || file.name.replace(/\.[^.]+$/, ""));
      form.append("format_name", formatName);
      form.append("source_lang", sourceLang.trim());
      form.append("target_lang", targetLang.trim() || "zh");
      form.append("domain", domain.trim());
      form.append("license", license.trim());
      form.append("source_url", sourceUrl.trim());
      form.append("version", version.trim());
      form.append("priority", String(priority));
      form.append("import_mode", importMode);
      form.append("full_import", String(fullImport));
      form.append("max_entries", String(fullImport ? 0 : maxEntries));
      const result = await fetchJson<DictionaryImportResponse>(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/import`, {
        method: "POST",
        body: form,
      });
      setMessage(`imported ${result.upserted}/${result.parsed}, skipped ${result.skipped}${result.full_import ? " (full import)" : ""}`);
      setFile(null);
      await refreshAll();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function updateSource(source: DictionarySource, patch: Partial<DictionarySource>) {
    setBusy(true);
    setError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/sources/${source.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      await refreshSources();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteSource(source: DictionarySource) {
    const ok = await confirm({
      title: "删除词典来源",
      message: `确定删除「${source.name}」及其 ${source.entry_count} 个条目吗？`,
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/sources/${source.id}`, { method: "DELETE" });
      if (sourceFilter === source.id) setSourceFilter("");
      await refreshAll();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setEntryEnabled(entry: DictionaryEntry, enabled: boolean) {
    setBusy(true);
    setError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/entries/${entry.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      await refreshEntries();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function deleteEntry(entry: DictionaryEntry) {
    const ok = await confirm({
      title: "删除词典条目",
      message: `确定删除「${entry.term}」吗？`,
      confirmLabel: "删除",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await fetchJson(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/entries/${entry.id}`, { method: "DELETE" });
      await refreshEntries();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function promoteEntry(entry: DictionaryEntry) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const resp = await fetchJson<{ knowledge_item_id: string }>(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/promote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entry_id: entry.id, status: "approved", confidence: Math.max(0.85, entry.quality || 0) }),
      });
      setMessage(`promoted ${resp.knowledge_item_id}`);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function runLookup() {
    if (!lookupTerm.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const resp = await fetchJson<{ count: number; results: DictionaryEntry[] }>(`${SUBTITLE_SERVICE_URL}/subtitle/dictionaries/lookup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          term: lookupTerm.trim(),
          source_lang: sourceLang.trim(),
          target_lang: targetLang.trim() || "zh",
          domain: domain.trim(),
          exact: lookupExact,
          limit: 20,
        }),
      });
      setLookupResults(resp.results);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dictionaries"
        description="导入词典和术语表，供翻译 RAG 查找。"
        actions={
          <>
            <Link to="/knowledge" className="rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-800 hover:bg-slate-50">
              Knowledge Base
            </Link>
            <Button onClick={refreshAll}>刷新</Button>
          </>
        }
      />
      {error ? <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div> : null}
      {message ? <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-800">{message}</div> : null}

      <Section>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-slate-900">导入词典</div>
            <div className="mt-1 text-xs text-slate-500">支持 CSV、TSV、ECDICT、CC-CEDICT、TMX、TBX、Wiktextract JSONL。</div>
          </div>
          <div className="flex flex-wrap gap-2">
            <select className="vr-input" value={dictionaryPreset} onChange={(e) => applyImportPreset(e.target.value)}>
              <option value="">特定字典导入</option>
              {importPresets.map((preset) => (
                <option key={preset.key} value={preset.key}>{preset.label}</option>
              ))}
            </select>
            <select className="vr-input" value={formatName} onChange={(e) => setFormatName(e.target.value)}>
              <option value="auto">auto</option>
              <option value="ecdict">ecdict</option>
              <option value="csv">csv</option>
              <option value="tsv">tsv</option>
              <option value="cc-cedict">cc-cedict</option>
              <option value="tmx">tmx</option>
              <option value="tbx">tbx</option>
              <option value="wiktextract">wiktextract</option>
            </select>
            <select className="vr-input" value={importMode} onChange={(e) => setImportMode(e.target.value)}>
              <option value="upsert">upsert</option>
              <option value="replace">replace</option>
            </select>
          </div>
        </div>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="block md:col-span-2">
            <div className="mb-1 text-xs text-slate-600">file</div>
            <input className="vr-input w-full" type="file" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">name</div>
            <input className="vr-input w-full" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">domain</div>
            <input className="vr-input w-full" value={domain} onChange={(e) => setDomain(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">source_lang</div>
            <input className="vr-input w-full" value={sourceLang} onChange={(e) => setSourceLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">target_lang</div>
            <input className="vr-input w-full" value={targetLang} onChange={(e) => setTargetLang(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">license</div>
            <input className="vr-input w-full" value={license} onChange={(e) => setLicense(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">source_url</div>
            <input className="vr-input w-full" value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">version</div>
            <input className="vr-input w-full" value={version} onChange={(e) => setVersion(e.target.value)} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">priority</div>
            <input type="number" className="vr-input w-full" value={priority} onChange={(e) => setPriority(parseInt(e.target.value || "0", 10))} />
          </label>
          <label className="block">
            <div className="mb-1 text-xs text-slate-600">max_entries</div>
            <input
              type="number"
              min={0}
              className="vr-input w-full"
              value={maxEntries}
              disabled={fullImport}
              onChange={(e) => setMaxEntries(parseInt(e.target.value || "0", 10))}
            />
          </label>
        </div>
        <div className="mt-3 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <label className="inline-flex items-center gap-2 text-sm text-slate-700">
            <input type="checkbox" checked={fullImport} onChange={(e) => setFullImport(e.target.checked)} />
            <span>全量导入</span>
          </label>
          <Button tone="primary" disabled={busy || !file} onClick={importDictionary}>{busy ? "导入中..." : "导入"}</Button>
        </div>
      </Section>

      <Section>
        <div className="mb-3 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-slate-900">词典来源</div>
            <div className="mt-1 text-xs text-slate-500">{sources.length} sources</div>
          </div>
        </div>
        {sources.length === 0 ? (
          <EmptyState>暂无词典来源</EmptyState>
        ) : (
          <DataTable>
            <thead>
              <tr>
                <th className="py-2 pr-3 text-left">Name</th>
                <th className="py-2 pr-3 text-left">Lang</th>
                <th className="py-2 pr-3 text-left">Entries</th>
                <th className="py-2 pr-3 text-left">License</th>
                <th className="py-2 pr-3 text-left">Priority</th>
                <th className="py-2 pr-3 text-left">Updated</th>
                <th className="py-2 pr-3 text-left">Action</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => (
                <tr key={source.id}>
                  <td className="py-2 pr-3">
                    <div className="font-medium text-slate-900">{source.name}</div>
                    <div className="mt-1 font-mono text-xs text-slate-500">{source.slug}</div>
                  </td>
                  <td className="py-2 pr-3 text-xs">{source.source_lang || "*"} {"->"} {source.target_lang}</td>
                  <td className="py-2 pr-3">{source.entry_count}</td>
                  <td className="py-2 pr-3 text-xs">{source.license || "-"}</td>
                  <td className="py-2 pr-3">
                    <input
                      type="number"
                      className="vr-input w-20"
                      value={source.priority}
                      onChange={(e) => updateSource(source, { priority: parseInt(e.target.value || "0", 10) })}
                    />
                  </td>
                  <td className="py-2 pr-3 text-xs">{formatDate(source.updated_at)}</td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-2">
                      <Button size="xs" onClick={() => setSourceFilter(source.id)}>筛选</Button>
                      <Button size="xs" onClick={() => updateSource(source, { enabled: !source.enabled })}>{source.enabled ? "禁用" : "启用"}</Button>
                      <Button size="xs" tone="danger" onClick={() => deleteSource(source)}>删除</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
      </Section>

      <Section>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-slate-900">词典条目</div>
            <div className="mt-1 text-xs text-slate-500">第 {entryPage + 1} 页，当前 {entries.length} 条。</div>
          </div>
          <div className="flex flex-wrap gap-2">
            <input className="vr-input" placeholder="搜索" value={entrySearch} onChange={(e) => setEntrySearch(e.target.value)} />
            <input className="vr-input w-24" placeholder="target" value={entryTargetLang} onChange={(e) => setEntryTargetLang(e.target.value)} />
            <input className="vr-input w-32" placeholder="domain" value={entryDomain} onChange={(e) => setEntryDomain(e.target.value)} />
            <select className="vr-input" value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
              <option value="">全部来源</option>
              {sources.map((source) => (
                <option key={source.id} value={source.id}>{source.name}</option>
              ))}
            </select>
          </div>
        </div>
        {entries.length === 0 ? (
          <EmptyState>暂无条目</EmptyState>
        ) : (
          <DataTable>
            <thead>
              <tr>
                <th className="py-2 pr-3 text-left">Term</th>
                <th className="py-2 pr-3 text-left">Translation</th>
                <th className="py-2 pr-3 text-left">Source</th>
                <th className="py-2 pr-3 text-left">Quality</th>
                <th className="py-2 pr-3 text-left">Usage</th>
                <th className="py-2 pr-3 text-left">Action</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td className="py-2 pr-3">
                    <div className="font-medium text-slate-900">{entry.term}</div>
                    <div className="mt-1 text-xs text-slate-500">{entry.pos || entry.domain || "-"}</div>
                  </td>
                  <td className="py-2 pr-3">
                    <div className="text-sm">{formatTranslations(entry)}</div>
                    <div className="mt-1 max-w-md truncate text-xs text-slate-500">{entry.definition || "-"}</div>
                  </td>
                  <td className="py-2 pr-3 text-xs">{entry.source_name}</td>
                  <td className="py-2 pr-3 text-xs">{entry.quality.toFixed(2)}</td>
                  <td className="py-2 pr-3 text-xs">{entry.usage_count}</td>
                  <td className="py-2 pr-3">
                    <div className="flex flex-wrap gap-2">
                      <Button size="xs" onClick={() => promoteEntry(entry)}>入库</Button>
                      <Button size="xs" onClick={() => setEntryEnabled(entry, !entry.enabled)}>{entry.enabled ? "禁用" : "启用"}</Button>
                      <Button size="xs" tone="danger" onClick={() => deleteEntry(entry)}>删除</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </DataTable>
        )}
        <div className="mt-3 flex gap-2">
          <Button disabled={entryPage <= 0} onClick={() => setEntryPage((value) => Math.max(0, value - 1))}>上一页</Button>
          <Button disabled={entries.length < pageSize} onClick={() => setEntryPage((value) => value + 1)}>下一页</Button>
        </div>
      </Section>

      <Section>
        <div className="text-sm font-semibold text-slate-900">Lookup</div>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input className="vr-input min-w-64" placeholder="term" value={lookupTerm} onChange={(e) => setLookupTerm(e.target.value)} />
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={lookupExact} onChange={(e) => setLookupExact(e.target.checked)} />
            exact
          </label>
          <Button disabled={busy || !lookupTerm.trim()} onClick={runLookup}>查找</Button>
        </div>
        {lookupResults.length ? (
          <div className="mt-3">
            <DataTable>
              <thead>
                <tr>
                  <th className="py-2 pr-3 text-left">Term</th>
                  <th className="py-2 pr-3 text-left">Translation</th>
                  <th className="py-2 pr-3 text-left">Source</th>
                  <th className="py-2 pr-3 text-left">Quality</th>
                </tr>
              </thead>
              <tbody>
                {lookupResults.map((entry) => (
                  <tr key={entry.id}>
                    <td className="py-2 pr-3">{entry.term}</td>
                    <td className="py-2 pr-3">{formatTranslations(entry)}</td>
                    <td className="py-2 pr-3">{entry.source_name}</td>
                    <td className="py-2 pr-3">{entry.quality.toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </DataTable>
          </div>
        ) : null}
      </Section>
    </div>
  );
}
