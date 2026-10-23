// node --test server-auth.test.mjs — the `/ws` relay must never act on a server-held key for an
// anonymous caller, and must refuse the upgrade before any upstream socket is opened.
import { test } from "node:test";
import assert from "node:assert/strict";
import { rejectUpgrade, resolveUpstreamKey } from "./server-auth.mjs";

const req = (cookie) => ({ headers: cookie ? { cookie } : {} });
const SHARED = { VEXA_API_KEY: "shared-key", VEXA_BOT_API_KEY: "bot-key" };

test("anonymous caller gets no key even when shared keys are configured (fail closed)", () => {
  assert.equal(resolveUpstreamKey(req(), { ...SHARED }), "");
  assert.equal(resolveUpstreamKey(req("other=1"), { ...SHARED }), "");
});

test("the caller's own cookie token is used, never the shared key", () => {
  assert.equal(resolveUpstreamKey(req("a=1; vexa-token=user-tok"), { ...SHARED }), "user-tok");
});

test("shared-key fallback only with explicit opt-in", () => {
  assert.equal(resolveUpstreamKey(req(), { ...SHARED, VEXA_ALLOW_SHARED_API_KEY: "true" }), "shared-key");
  assert.equal(resolveUpstreamKey(req(), { VEXA_BOT_API_KEY: "bot-key", VEXA_ALLOW_SHARED_API_KEY: "true" }), "bot-key");
  assert.equal(resolveUpstreamKey(req(), { ...SHARED, VEXA_ALLOW_SHARED_API_KEY: "1" }), "");
});

test("malformed cookie encoding is treated as anonymous, not a crash", () => {
  assert.equal(resolveUpstreamKey(req("vexa-token=%E0%A4%A"), { ...SHARED }), "");
});

test("rejectUpgrade writes a 401 and destroys the socket", () => {
  const sock = { out: "", destroyed: false, write(s) { this.out += s; }, destroy() { this.destroyed = true; } };
  rejectUpgrade(sock);
  assert.match(sock.out, /^HTTP\/1\.1 401 Unauthorized\r\n/);
  assert.equal(sock.destroyed, true);
});
