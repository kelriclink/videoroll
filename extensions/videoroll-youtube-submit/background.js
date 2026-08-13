import {
  buildRemotePayload,
  DEFAULT_EXTENSION_SETTINGS,
  endpointPermissionPattern,
  normalizeRemoteEndpoint,
  resolveYouTubeVideoUrl,
  responseErrorMessage,
} from "./shared.js";

const MENU_ID = "videoroll-submit-auto";
const PENDING_STORAGE_KEY = "pendingSubmissions";
const PENDING_TTL_MS = 23 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 120_000;
const inFlight = new Set();

function installContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: "提交到 VideoRoll 自动模式",
      contexts: ["page", "link", "video"],
      documentUrlPatterns: ["https://*.youtube.com/*", "https://youtu.be/*"],
    });
  });
}

chrome.runtime.onInstalled.addListener(installContextMenu);
chrome.runtime.onStartup.addListener(installContextMenu);

chrome.action.onClicked.addListener(() => {
  chrome.action.setBadgeText({ text: "" });
  chrome.runtime.openOptionsPage();
});

async function showStatus(tabId, kind, message) {
  const badge = kind === "working" ? "…" : kind === "success" ? "✓" : "!";
  const color = kind === "working" ? "#0369a1" : kind === "success" ? "#15803d" : "#be123c";
  await chrome.action.setBadgeBackgroundColor({ color }).catch(() => {});
  await chrome.action.setBadgeText({ text: badge }).catch(() => {});
  if (typeof tabId === "number") {
    await chrome.tabs.sendMessage(tabId, { type: "videoroll-submit-status", kind, message }).catch(() => {});
  }
}

async function readPendingSubmissions() {
  const stored = await chrome.storage.local.get(PENDING_STORAGE_KEY);
  const raw = stored[PENDING_STORAGE_KEY];
  const now = Date.now();
  const pending = raw && typeof raw === "object" ? raw : {};
  let changed = false;
  for (const [url, record] of Object.entries(pending)) {
    if (!record || typeof record !== "object" || now - Number(record.createdAt || 0) >= PENDING_TTL_MS) {
      delete pending[url];
      changed = true;
    }
  }
  if (changed) await chrome.storage.local.set({ [PENDING_STORAGE_KEY]: pending });
  return pending;
}

async function reserveIdempotencyKey(videoUrl, endpoint, payload) {
  const pending = await readPendingSubmissions();
  const payloadJson = JSON.stringify(payload);
  const existing = pending[videoUrl];
  if (existing && existing.endpoint === endpoint && existing.payloadJson === payloadJson && existing.key) {
    return existing.key;
  }

  const key = `videoroll-extension-${crypto.randomUUID()}`;
  pending[videoUrl] = { key, endpoint, payloadJson, createdAt: Date.now() };
  await chrome.storage.local.set({ [PENDING_STORAGE_KEY]: pending });
  return key;
}

async function clearIdempotencyKey(videoUrl, key) {
  const pending = await readPendingSubmissions();
  if (!pending[videoUrl] || pending[videoUrl].key !== key) return;
  delete pending[videoUrl];
  await chrome.storage.local.set({ [PENDING_STORAGE_KEY]: pending });
}

function shouldKeepIdempotencyKey(status, message) {
  if (status >= 500) return true;
  if (status !== 409) return false;
  return !message.toLowerCase().includes("previously failed");
}

async function submitVideo(videoUrl, tabId) {
  const settings = await chrome.storage.local.get(DEFAULT_EXTENSION_SETTINGS);
  if (!settings.endpoint || !settings.token) {
    await showStatus(tabId, "error", "请先点击扩展图标，配置 VideoRoll 地址和 Remote API Token。 扩展设置页已打开。");
    await chrome.runtime.openOptionsPage();
    return;
  }

  let endpoint;
  try {
    endpoint = normalizeRemoteEndpoint(settings.endpoint);
  } catch (error) {
    await showStatus(tabId, "error", error instanceof Error ? error.message : String(error));
    await chrome.runtime.openOptionsPage();
    return;
  }

  const permission = endpointPermissionPattern(endpoint);
  const allowed = await chrome.permissions.contains({ origins: [permission] });
  if (!allowed) {
    await showStatus(tabId, "error", "VideoRoll 地址权限尚未授权，请在扩展设置页重新保存配置。 设置页已打开。");
    await chrome.runtime.openOptionsPage();
    return;
  }

  const payload = buildRemotePayload(videoUrl, settings);
  const idempotencyKey = await reserveIdempotencyKey(videoUrl, endpoint, payload);
  await showStatus(tabId, "working", "正在提交到 VideoRoll 自动模式…");

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${settings.token}`,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok) {
      const message = await responseErrorMessage(response);
      if (!shouldKeepIdempotencyKey(response.status, message)) {
        await clearIdempotencyKey(videoUrl, idempotencyKey);
      }
      throw new Error(message);
    }

    const result = await response.json();
    await clearIdempotencyKey(videoUrl, idempotencyKey);
    const shortId = String(result.task_id || "").slice(0, 8);
    const detail = result.deduped ? "该视频已存在，未重复启动流水线" : `任务 ${shortId || "已创建"} 已进入自动模式`;
    await showStatus(tabId, "success", `提交成功：${detail}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    const readable = error instanceof DOMException && error.name === "AbortError" ? "请求超时；下次提交同一视频时会安全重试，不会重复派发。" : message;
    await showStatus(tabId, "error", `提交失败：${readable}`);
  } finally {
    clearTimeout(timeout);
  }
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID) return;
  const videoUrl = resolveYouTubeVideoUrl(info, tab && tab.url ? tab.url : "");
  if (!videoUrl) {
    void showStatus(tab && tab.id, "error", "没有识别到 YouTube 视频链接。请在视频页面空白处，或视频缩略图链接上右键。 ");
    return;
  }
  if (inFlight.has(videoUrl)) {
    void showStatus(tab && tab.id, "working", "这个视频正在提交，请稍候。 ");
    return;
  }

  inFlight.add(videoUrl);
  void submitVideo(videoUrl, tab && tab.id).finally(() => inFlight.delete(videoUrl));
});
