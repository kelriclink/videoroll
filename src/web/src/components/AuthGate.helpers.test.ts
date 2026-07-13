import { describe, expect, it } from "vitest";
import { ADMIN_BOOTSTRAP_HEADER, buildSetupAuthRequest } from "./AuthGate.helpers";

describe("buildSetupAuthRequest", () => {
  it("sends the bootstrap secret only in the explicit header", () => {
    const request = buildSetupAuthRequest("password-123", "bootstrap-secret");

    expect(request.headers).toEqual({
      "Content-Type": "application/json",
      [ADMIN_BOOTSTRAP_HEADER]: "bootstrap-secret",
    });
    expect(JSON.parse(String(request.body))).toEqual({ password: "password-123" });
    expect(String(request.body)).not.toContain("bootstrap-secret");
  });
});
