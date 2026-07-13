export class HttpError extends Error {
  readonly status: number;
  readonly statusText: string;
  readonly detail: string;
  readonly body: unknown;

  constructor(response: Response, detail: string, body: unknown) {
    super(`${response.status} ${response.statusText}${detail ? ` - ${detail}` : ""}`);
    this.name = "HttpError";
    this.status = response.status;
    this.statusText = response.statusText;
    this.detail = detail;
    this.body = body;
  }
}

export async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, { credentials: "include", ...init });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    let detail = text;
    let body: unknown = text || null;
    if (text) {
      try {
        const parsed = JSON.parse(text) as { detail?: unknown; message?: unknown };
        body = parsed;
        if (typeof parsed.detail === "string" && parsed.detail.trim()) {
          detail = parsed.detail.trim();
        } else if (typeof parsed.message === "string" && parsed.message.trim()) {
          detail = parsed.message.trim();
        }
      } catch {}
    }
    throw new HttpError(resp, detail, body);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}
