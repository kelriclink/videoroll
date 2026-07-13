export const ADMIN_BOOTSTRAP_HEADER = "X-Videoroll-Admin-Bootstrap";

export function buildSetupAuthRequest(password: string, bootstrapSecret: string): RequestInit {
  return {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [ADMIN_BOOTSTRAP_HEADER]: bootstrapSecret,
    },
    body: JSON.stringify({ password }),
  };
}
