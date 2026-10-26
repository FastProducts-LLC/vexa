import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** First-run bootstrap: the unauthenticated /api/auth/instance probe + the sign-in claim call.
 *  Cookie jar mirrors login.test.ts (the login route sets cookies on success). */
let cookieJar: Record<string, string> = {};

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (cookieJar[name] !== undefined ? { name, value: cookieJar[name] } : undefined),
    set: (name: string, value: string) => { cookieJar[name] = value; },
    delete: (name: string) => { delete cookieJar[name]; },
  }),
}));

import { GET as instanceRoute } from "../instance/route";
import { POST as loginRoute } from "../login/route";
import { authOptions } from "../[...nextauth]/authOptions";

function req(body: unknown) {
  return new Request("http://local/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  }) as any;
}

/** admin-api stub: find-or-create + mint + the internal instance/bootstrap edges. */
function stubAdminApi(opts: { adminExists: boolean }) {
  const calls: { url: string; body?: string }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body as string });
      if (url.includes("/admin/users/email/")) {
        return new Response(JSON.stringify({ id: 7, email: "new-test@vexa.ai" }), { status: 200 });
      }
      if (url.includes("/tokens")) {
        return new Response(JSON.stringify({ token: "tok-7" }), { status: 201 });
      }
      if (url.includes("/internal/instance")) {
        return new Response(JSON.stringify({ admin_exists: opts.adminExists }), { status: 200 });
      }
      if (url.includes("/internal/bootstrap-admin")) {
        return new Response(JSON.stringify({ claimed: !opts.adminExists, admin_exists: true }), { status: 200 });
      }
      return new Response("nope", { status: 500 });
    }),
  );
  return calls;
}

beforeEach(() => {
  cookieJar = {};
  process.env.VEXA_ADMIN_API_URL = "http://admin.test";
  process.env.VEXA_ADMIN_API_KEY = "admin-key";
  process.env.VEXA_INTERNAL_API_SECRET = "internal-secret";
  delete process.env.VEXA_ADMIN_EMAILS;
  process.env.VEXA_TERMINAL_DEV_LOGIN = "1";
  process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS = "new-test@vexa.ai";
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete process.env.VEXA_ADMIN_EMAILS;
  delete process.env.VEXA_TERMINAL_DEV_LOGIN;
  delete process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS;
  vi.unstubAllEnvs();
});

describe("/api/auth/instance — the login surface's claim-screen switch", () => {
  it("no admin anywhere → admin_exists false (claim screen shows)", async () => {
    stubAdminApi({ adminExists: false });
    const res = await instanceRoute();
    expect(await res.json()).toEqual({ admin_exists: false, dev_login: true });
  });

  it("dev_login is false without the flag, and false in production even with it", async () => {
    stubAdminApi({ adminExists: true });
    delete process.env.VEXA_TERMINAL_DEV_LOGIN;
    expect((await (await instanceRoute()).json()).dev_login).toBe(false);
    process.env.VEXA_TERMINAL_DEV_LOGIN = "1";
    vi.stubEnv("NODE_ENV", "production");
    expect((await (await instanceRoute()).json()).dev_login).toBe(false);
  });

  it("a configured allowlist counts as an existing admin — internal probe not even called", async () => {
    process.env.VEXA_ADMIN_EMAILS = "dmitry@vexa.ai";
    const calls = stubAdminApi({ adminExists: false });
    const res = await instanceRoute();
    expect(await res.json()).toEqual({ admin_exists: true, dev_login: true });
    expect(calls.length).toBe(0);
  });

  it("probe unreachable → FAIL-SAFE true (plain sign-in, never a dangling claim screen)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("ECONNREFUSED"); }));
    const res = await instanceRoute();
    expect(await res.json()).toEqual({ admin_exists: true, dev_login: true });
  });
});

/** Drive the NextAuth signIn callback exactly as a verified Google sign-in would. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const oauthSignIn = (email: string) => (authOptions.callbacks!.signIn as any)({
  user: { id: "g-1", email },
  account: { provider: "google", type: "oauth", providerAccountId: "g-1" },
});

describe("first sign-in claims the admin role — via verified OAuth only", () => {
  it("OAuth sign-in on a fresh instance POSTs the bootstrap claim with the user's id", async () => {
    const calls = stubAdminApi({ adminExists: false });
    expect(await oauthSignIn("new-test@vexa.ai")).toBe(true);
    const claim = calls.find((c) => c.url.includes("/internal/bootstrap-admin"));
    expect(claim).toBeDefined();
    expect(JSON.parse(claim!.body || "{}")).toEqual({ user_id: 7 });
  });

  it("C6: dev login on a fresh instance NEVER claims admin (session only)", async () => {
    const calls = stubAdminApi({ adminExists: false });
    const res = await loginRoute(req({ email: "new-test@vexa.ai" }));
    expect(res.status).toBe(200);
    expect(calls.some((c) => c.url.includes("/internal/bootstrap-admin"))).toBe(false);
  });

  it("allowlist-run instance → claim machinery stays off, even for OAuth", async () => {
    process.env.VEXA_ADMIN_EMAILS = "dmitry@vexa.ai";
    const calls = stubAdminApi({ adminExists: true });
    expect(await oauthSignIn("new-test@vexa.ai")).toBe(true);
    expect(calls.some((c) => c.url.includes("/internal/bootstrap-admin"))).toBe(false);
  });
});
