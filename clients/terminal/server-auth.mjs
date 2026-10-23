// Upstream-key resolution for the custom server's `/ws` relay (server.mjs). Kept dependency-free so
// it is unit-testable with `node --test` (server-auth.test.mjs) without booting Next.
//
// The key MUST mirror the REST side (src/app/api/proxyAuth.ts): the logged-in user's APIToken from
// the httpOnly `vexa-token` cookie. With no cookie the caller is ANONYMOUS — the upgrade is refused
// (401) instead of being relayed on a server-held key. The shared VEXA_API_KEY / VEXA_BOT_API_KEY
// fallback (every visitor acts as that key's user) is opt-in: VEXA_ALLOW_SHARED_API_KEY=true.

/** Pull a single cookie value out of a raw `Cookie` header. Returns undefined if absent. */
export function readCookie(header, name) {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq < 0) continue;
    if (part.slice(0, eq).trim() === name) {
      try {
        return decodeURIComponent(part.slice(eq + 1).trim());
      } catch {
        return undefined;
      }
    }
  }
  return undefined;
}

export function sharedApiKeyAllowed(env = process.env) {
  return env.VEXA_ALLOW_SHARED_API_KEY === "true";
}

/** cookie token → (opt-in only) VEXA_API_KEY → VEXA_BOT_API_KEY → "" (anonymous). */
export function resolveUpstreamKey(req, env = process.env) {
  const cookieName = env.VEXA_AUTH_COOKIE_NAME || "vexa-token";
  const cookieToken = readCookie(req.headers?.cookie, cookieName);
  if (cookieToken) return cookieToken;
  if (!sharedApiKeyAllowed(env)) return "";
  return env.VEXA_API_KEY || env.VEXA_BOT_API_KEY || "";
}

/** Refuse an HTTP upgrade before any upstream socket is opened. */
export function rejectUpgrade(socket, status = 401, reason = "Unauthorized") {
  try {
    socket.write(`HTTP/1.1 ${status} ${reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`);
  } catch {
    /* socket already gone */
  }
  socket.destroy();
}
