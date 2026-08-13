import {
  DEFAULT_EXTENSION_SETTINGS,
  endpointPermissionPattern,
  normalizeRemoteEndpoint,
} from "./shared.js";

const form = document.getElementById("settings-form");
const endpointInput = document.getElementById("endpoint");
const tokenInput = document.getElementById("token");
const licenseInput = document.getElementById("license");
const autoPublishInput = document.getElementById("auto-publish-mode");
const clearTokenButton = document.getElementById("clear-token");
const status = document.getElementById("status");

function showStatus(message, kind) {
  status.textContent = message;
  status.className = kind;
}

async function loadSettings() {
  const settings = await chrome.storage.local.get(DEFAULT_EXTENSION_SETTINGS);
  endpointInput.value = settings.endpoint || "";
  licenseInput.value = settings.license || DEFAULT_EXTENSION_SETTINGS.license;
  autoPublishInput.value = settings.autoPublishMode || DEFAULT_EXTENSION_SETTINGS.autoPublishMode;
  tokenInput.value = "";
  tokenInput.placeholder = settings.token ? "Token 已保存；留空表示保持不变" : "输入 Remote API Token";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showStatus("", "");

  try {
    const endpoint = normalizeRemoteEndpoint(endpointInput.value);
    const permission = endpointPermissionPattern(endpoint);
    const granted = await chrome.permissions.request({ origins: [permission] });
    if (!granted) throw new Error("未授予 VideoRoll 地址访问权限，配置尚未保存");

    const current = await chrome.storage.local.get(DEFAULT_EXTENSION_SETTINGS);
    const token = tokenInput.value.trim() || current.token;
    if (!token) throw new Error("请填写 Remote API Token");

    await chrome.storage.local.set({
      endpoint,
      token,
      license: licenseInput.value,
      autoPublishMode: autoPublishInput.value,
    });
    endpointInput.value = endpoint;
    tokenInput.value = "";
    tokenInput.placeholder = "Token 已保存；留空表示保持不变";
    showStatus("配置已保存。现在可以在 YouTube 上右键提交视频。", "success");
  } catch (error) {
    showStatus(error instanceof Error ? error.message : String(error), "error");
  }
});

clearTokenButton.addEventListener("click", async () => {
  await chrome.storage.local.set({ token: "" });
  tokenInput.value = "";
  tokenInput.placeholder = "输入 Remote API Token";
  showStatus("本机浏览器中保存的 Token 已清除。", "success");
});

void loadSettings();
