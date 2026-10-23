import { afterEach, describe, expect, it, vi } from "vitest";

/** Server routes that act with a server-side credential (admin key / STT token) must never do so for
 *  an anonymous or unverified caller. */
let jar: Record<string, string> = {};
vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (jar[name] !== undefined ? { name, value: jar[name] } : undefined),
    set: (name: string, value: string) => { jar[name] = value; },
    delete: () => {},
  }),
}));

import { POST as loginRoute } from "../auth/login/route";
import { POST as sttRoute } from "../stt/route";

const ENV_KEYS = ["VEXA_ALLOW_DIRECT_LOGIN", "VEXA_ADMIN_API_URL", "VEXA_ADMIN_API_KEY", "VEXA_INTERNAL_API_SECRET",
  "TRANSCRIPTION_SERVICE_URL", "TRANSCRIPTION_SERVICE_TOKEN", "VEXA_API_KEY", "VEXA_BOT_API_KEY",
  "VEXA_ALLOW_SHARED_API_KEY", "VEXA_ADMIN_EMAILS"];

afterEach(() => {
  jar = {};
  for (const k of ENV_KEYS) delete process.env[k];
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function jsonReq(body: unknown) {
  return { json: async () => body } as unknown as import("next/server").NextRequest;
}

function wavReq() {
  return new Request("http://terminal/api/stt", { method: "POST", body: new Uint8Array(4096) });
}

describe("POST /api/auth/login (direct email login)", () => {
  it("is 404 by default and never touches admin-api with the admin key", async () => {
    process.env.VEXA_ADMIN_API_URL = "http://admin-api";
    process.env.VEXA_ADMIN_API_KEY = "admin-secret";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const r = await loginRoute(jsonReq({ email: "victim-test@example.com" }));
    expect(r.status).toBe(404);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(jar["vexa-token"]).toBeUndefined();
  });

  it("works only with the explicit VEXA_ALLOW_DIRECT_LOGIN=true opt-in", async () => {
    process.env.VEXA_ALLOW_DIRECT_LOGIN = "true";
    process.env.VEXA_ADMIN_API_URL = "http://admin-api";
    process.env.VEXA_ADMIN_API_KEY = "admin-secret";
    process.env.VEXA_ADMIN_EMAILS = "ops@example.com"; // allowlist → no bootstrap-admin claim
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/admin/users/email/")) return new Response(JSON.stringify({ id: 5, email: "qa-test@example.com" }), { status: 200 });
      if (url.includes("/tokens")) return new Response(JSON.stringify({ token: "minted" }), { status: 201 });
      return new Response("{}", { status: 200 });
    }));
    const r = await loginRoute(jsonReq({ email: "qa-test@example.com" }));
    expect(r.status).toBe(200);
    expect(jar["vexa-token"]).toBe("minted");
  });
});

describe("POST /api/stt (credentialed STT proxy)", () => {
  it("401s an anonymous caller even when shared keys are configured", async () => {
    process.env.TRANSCRIPTION_SERVICE_URL = "http://stt";
    process.env.TRANSCRIPTION_SERVICE_TOKEN = "stt-secret";
    process.env.VEXA_BOT_API_KEY = "bot-key";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const r = await sttRoute(wavReq());
    expect(r.status).toBe(401);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("401s a forged cookie (validated against admin-api, STT never called)", async () => {
    jar["vexa-token"] = "forged";
    process.env.TRANSCRIPTION_SERVICE_URL = "http://stt";
    process.env.TRANSCRIPTION_SERVICE_TOKEN = "stt-secret";
    process.env.VEXA_ADMIN_API_URL = "http://admin-api";
    process.env.VEXA_INTERNAL_API_SECRET = "isecret";
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(url);
      if (url.includes("/internal/validate")) return new Response("no", { status: 401 });
      return new Response(JSON.stringify({ text: "x" }), { status: 200 });
    }));
    const r = await sttRoute(wavReq());
    expect(r.status).toBe(401);
    expect(calls.some((u) => u.startsWith("http://stt"))).toBe(false);
  });

  it("forwards for a validated user", async () => {
    jar["vexa-token"] = "alice-tok";
    process.env.TRANSCRIPTION_SERVICE_URL = "http://stt";
    process.env.TRANSCRIPTION_SERVICE_TOKEN = "stt-secret";
    process.env.VEXA_ADMIN_API_URL = "http://admin-api";
    process.env.VEXA_INTERNAL_API_SECRET = "isecret";
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/internal/validate")) return new Response(JSON.stringify({ user_id: 1, email: "a@x.io" }), { status: 200 });
      return new Response(JSON.stringify({ text: "hello", segments: [] }), { status: 200 });
    }));
    const r = await sttRoute(wavReq());
    expect(r.status).toBe(200);
    expect((await r.json()).text).toBe("hello");
  });
});
