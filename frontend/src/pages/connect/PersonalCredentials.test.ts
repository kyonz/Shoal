import { describe, expect, it } from "vitest";
import { pickPersonal, userFields } from "./PersonalCredentials";

describe("pickPersonal", () => {
  it("leaves out defaults", () => {
    expect(pickPersonal({ credentials: "shared", user_fields: [] })).toEqual({});
    expect(pickPersonal({ credentials: "per_user", user_fields: ["token"] })).toEqual({ credentials: "per_user", user_fields: ["token"] });
  });
});

describe("userFields", () => {
  it("follows the engine: user_fields, else the secret fields, plus the OAuth token field", () => {
    expect(userFields({ secrets: { token: "t", app_key: "k" } })).toEqual(["token", "app_key"]);
    expect(userFields({ user_fields: ["token"], secrets: { token: "t", app_key: "k" } })).toEqual(["token"]);
    expect(userFields({ oauth: { authorize_url: "a", token_url: "b", client_id: "c", token_field: "access_token" } })).toEqual(["access_token"]);
  });
});
