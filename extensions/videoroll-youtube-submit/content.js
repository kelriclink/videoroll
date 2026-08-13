chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "videoroll-submit-status") return;

  const previous = document.getElementById("videoroll-submit-toast-host");
  if (previous) previous.remove();

  const host = document.createElement("div");
  host.id = "videoroll-submit-toast-host";
  host.style.position = "fixed";
  host.style.right = "24px";
  host.style.top = "24px";
  host.style.zIndex = "2147483647";
  const shadow = host.attachShadow({ mode: "closed" });

  const toast = document.createElement("div");
  const colors = {
    working: { border: "#38bdf8", background: "#f0f9ff", foreground: "#0c4a6e" },
    success: { border: "#4ade80", background: "#f0fdf4", foreground: "#14532d" },
    error: { border: "#fb7185", background: "#fff1f2", foreground: "#881337" },
  };
  const color = colors[message.kind] || colors.working;
  toast.style.cssText = [
    "box-sizing:border-box",
    "display:flex",
    "align-items:flex-start",
    "gap:12px",
    "max-width:440px",
    "padding:14px 16px",
    "border-radius:10px",
    `border:1px solid ${color.border}`,
    `background:${color.background}`,
    `color:${color.foreground}`,
    "box-shadow:0 12px 30px rgba(15,23,42,.2)",
    "font:14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",
  ].join(";");

  const text = document.createElement("div");
  text.style.flex = "1";
  text.textContent = String(message.message || "");
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "×";
  close.setAttribute("aria-label", "关闭");
  close.style.cssText = "border:0;background:transparent;color:inherit;cursor:pointer;font:20px/1 sans-serif;padding:0";
  close.addEventListener("click", () => host.remove());

  toast.append(text, close);
  shadow.append(toast);
  document.documentElement.append(host);
  const timeoutMs = message.kind === "error" ? 10_000 : message.kind === "success" ? 7_000 : 30_000;
  setTimeout(() => host.remove(), timeoutMs);
});
