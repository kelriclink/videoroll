export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, { credentials: "include", ...init });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    let detail = text;
    if (text) {
      try {
        const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown };
        if (typeof parsed.detail === "string" && parsed.detail.trim()) {
          detail = parsed.detail.trim();
        } else if (typeof parsed.message === "string" && parsed.message.trim()) {
          detail = parsed.message.trim();
        }
      } catch {}
    }
    throw new Error(`${resp.status} ${resp.statusText}${detail ? ` - ${detail}` : ""}`);
  }
  return (await resp.json()) as T;
}
