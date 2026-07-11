export type SocialPlatform = "douyin" | "xiaohongshu" | "kuaishou";
export type PublishPlatform = "bilibili" | SocialPlatform;
export type PublishPlatformSettings = Record<PublishPlatform, boolean>;

export type SocialAccount = {
  id: string;
  platform: SocialPlatform;
  name: string;
  is_active: boolean;
  check_state: "unchecked" | "queued" | "checking" | "valid" | "invalid" | "error";
  last_checked_at?: string | null;
  last_check_message?: string | null;
  rotated_at?: string | null;
};

export type SocialLoginSession = {
  id: string;
  platform: SocialPlatform;
  account_name: string;
  state: "starting" | "running" | "canceling" | "canceled" | "succeeded" | "failed";
  message?: string | null;
  browser_url: string;
  created_at: string;
  finished_at?: string | null;
};

export function storageStateGuidance(platform: SocialPlatform, accountName: string) {
  const name = accountName.trim() || "creator";
  return {
    command: `sau ${platform} login --account ${name}`,
    path: `cookies/${platform}_${name}.json`,
  };
}

export function activeAccountsForPlatform(accounts: SocialAccount[], platform: SocialPlatform) {
  return accounts.filter((account) => account.platform === platform && account.is_active);
}

export function enabledPublishPlatforms(settings: PublishPlatformSettings): PublishPlatform[] {
  return (["bilibili", "douyin", "xiaohongshu", "kuaishou"] as PublishPlatform[]).filter(
    (platform) => settings[platform] === true,
  );
}

export function loginSessionLabel(session: Pick<SocialLoginSession, "state" | "message">) {
  const stateLabels: Record<SocialLoginSession["state"], string> = {
    starting: "正在准备登录窗口",
    running: "登录窗口已打开，可扫码并完成安全校验",
    canceling: "正在关闭登录窗口",
    canceled: "登录已取消",
    succeeded: "登录成功，账号状态已加密保存",
    failed: "登录失败",
  };
  const base = stateLabels[session.state];
  return session.message ? `${base} · ${session.message}` : base;
}
