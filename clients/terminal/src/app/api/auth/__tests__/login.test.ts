import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** Cookie jar the mocked next/headers writes into, so the test can assert what login set. */
let setCookies: Array<{ name: string; value: string; opts?: unknown }> = [];

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: () => undefined,
    set: (name: string, value: string, opts?: unknown) => setCookies.push({ name, value, opts }),
    delete: () => {},
  }),
}));

import { POST as login } from "../login/route";

function makeReq(body: unknown): import("next/server").NextRequest {
  return { json: async () => body } as unknown as import("next/server").NextRequest;
}

beforeEach(() => {
  setCookies = [];
  process.env.VEXA_ADMIN_API_URL = "http://admin.test";
  process.env.VEXA_ADMIN_API_KEY = "admin-secret";
  // dev login is opt-in (default 404 — see criticalAuth.test.ts); these tests exercise it ON with an
  // exact-match allowlist. vitest runs with NODE_ENV=test, i.e. not production.
  process.env.VEXA_TERMINAL_DEV_LOGIN = "1";
  process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS = "test-a@b.com, Test-New@B.com";
});

afterEach(() => {
  delete process.env.VEXA_TERMINAL_DEV_LOGIN;
  delete process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS;
  delete process.env.VEXA_ALLOW_DIRECT_LOGIN;
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("/api/auth/login — direct email login against a mocked admin-api", () => {
  it("finds an existing user, mints a token, and sets both cookies", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(`${init?.method || "GET"} ${url}`);
        if (url.includes("/admin/users/email/")) {
          return new Response(JSON.stringify({ id: 42, email: "test-a@b.com", name: "A" }), { status: 200 });
        }
        if (url.includes("/tokens")) {
          return new Response(JSON.stringify({ token: "minted-tok" }), { status: 200 });
        }
        return new Response("nope", { status: 500 });
      }),
    );

    const res = await login(makeReq({ email: "test-a@b.com" }));
    expect(res.status).toBe(200);

    // No create call — user already existed.
    expect(calls.some((c) => c.includes("/admin/users/email/"))).toBe(true);
    expect(calls.some((c) => c.startsWith("POST") && c.endsWith("/admin/users"))).toBe(false);
    expect(calls.some((c) => c.includes("/tokens"))).toBe(true);
    // an EXISTING user is not re-provisioned (eager provisioning fires only on account creation)
    expect(calls.some((c) => c.includes("/agent/workspace/init"))).toBe(false);

    const tok = setCookies.find((c) => c.name === "vexa-token");
    const info = setCookies.find((c) => c.name === "vexa-user-info");
    expect(tok?.value).toBe("minted-tok");
    expect(JSON.parse(info!.value)).toEqual({ email: "test-a@b.com", name: "A" });
    expect((tok?.opts as { httpOnly?: boolean })?.httpOnly).toBe(true);
  });

  it("rejects a non-allowlisted email without calling admin-api", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email: "real@company.com" }));
    expect(res.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  // C6 regression: "contains test" used to be the whole check.
  it.each([
    "victim-test@company.com",
    "test@company.com",
    "admin@test.com",
    "xtest-a@b.com",        // superstring of an allowlisted entry
    "test-a@b.com.evil.io", // allowlisted entry as a prefix
    "test-a@b.co",          // allowlisted entry minus a char
  ])("rejects %s (no substring / prefix / domain matching) without calling admin-api", async (email) => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email }));
    expect(res.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(setCookies).toHaveLength(0);
  });

  it("matches the allowlist case- and whitespace-insensitively (exact address)", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/admin/users/email/")) return new Response(JSON.stringify({ id: 42, email: "test-a@b.com" }), { status: 200 });
      if (url.includes("/tokens")) return new Response(JSON.stringify({ token: "t" }), { status: 200 });
      return new Response("nope", { status: 500 });
    }));
    const res = await login(makeReq({ email: "  TEST-A@B.COM " }));
    expect(res.status).toBe(200);
  });

  it("C6: an email merely containing \"test\" gets 403 and never reaches admin-api, even with the flag on", async () => {
    process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS = "qa-test@example.com";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    for (const email of ["attacker-test@evil.com", "test@bank.com", "contest@company.com", "qa-test@example.com.evil"]) {
      const res = await login(makeReq({ email }));
      expect(res.status).toBe(403);
    }
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(setCookies).toHaveLength(0);
  });

  it("an empty allowlist allows nobody, even with the flag on", async () => {
    process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS = " , ";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email: "test-a@b.com" }));
    expect(res.status).toBe(403);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("is 404 when NODE_ENV=production even with the flag + allowlist set", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email: "test-a@b.com" }));
    expect(res.status).toBe(404);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(setCookies).toHaveLength(0);
  });

  it.each(["true", "yes", "0", ""])("is 404 unless the flag is exactly \"1\" (got %j)", async (v) => {
    process.env.VEXA_TERMINAL_DEV_LOGIN = v;
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email: "test-a@b.com" }));
    expect(res.status).toBe(404);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("the legacy VEXA_ALLOW_DIRECT_LOGIN=true no longer enables it", async () => {
    delete process.env.VEXA_TERMINAL_DEV_LOGIN;
    process.env.VEXA_ALLOW_DIRECT_LOGIN = "true";
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email: "test-a@b.com" }));
    expect(res.status).toBe(404);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("creates the user when admin-api returns 404, then mints a token", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(`${init?.method || "GET"} ${url}`);
        if (url.includes("/admin/users/email/")) return new Response("not found", { status: 404 });
        if (init?.method === "POST" && url.endsWith("/admin/users")) {
          return new Response(JSON.stringify({ id: 7, email: "test-new@b.com" }), { status: 201 });
        }
        if (url.includes("/tokens")) return new Response(JSON.stringify({ token: "tok-7" }), { status: 200 });
        return new Response("nope", { status: 500 });
      }),
    );

    const res = await login(makeReq({ email: "test-new@b.com" }));
    expect(res.status).toBe(200);
    expect(calls.some((c) => c.startsWith("POST") && c.endsWith("/admin/users"))).toBe(true);
    expect(setCookies.find((c) => c.name === "vexa-token")?.value).toBe("tok-7");
    // a NEW account eagerly provisions the agent workspace over the gateway (best-effort — a 500 here
    // is swallowed, so sign-in still succeeds above); it authenticates with the freshly minted token
    const provision = calls.find((c) => c.includes("/agent/workspace/init"));
    expect(provision).toBeTruthy();
    expect(provision!.startsWith("POST")).toBe(true);
  });

  it("rejects a malformed email without calling admin-api", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const res = await login(makeReq({ email: "not-an-email" }));
    expect(res.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
