/** One-shot idempotency key, supported on LAN HTTP as well as HTTPS. */
export function createYouDubOperationKey(): string {
  const prefix = `youdub-native:${Date.now().toString(36)}`;
  // crypto.getRandomValues is supported in non-secure contexts, unlike randomUUID.
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    return `${prefix}:${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
  }
  // This is an idempotency key, not an authentication token. Support older browsers.
  return `${prefix}:${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
}
