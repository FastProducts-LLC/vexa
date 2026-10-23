/** Per-user API key for the upstream proxies.
 *
 *  The X-API-Key the terminal forwards to the gateway / agent-api is the logged-in user's APIToken,
 *  read from the httpOnly `vexa-token` cookie (set by /api/auth/login). With no cookie the caller is
 *  ANONYMOUS and gets "" (the gateway answers 401). The server-side VEXA_API_KEY / VEXA_BOT_API_KEY
 *  fallback — which makes EVERY visitor act as that key's user — is opt-in for single-user self-host
 *  deploys only: VEXA_ALLOW_SHARED_API_KEY=true. */
import { cookies } from "next/headers";
import { AUTH_COOKIE } from "./auth/adminApi";

/** true when the operator explicitly opted into the shared-key (zero-login, single-user) mode. */
export function sharedApiKeyAllowed(): boolean {
  return process.env.VEXA_ALLOW_SHARED_API_KEY === "true";
}

/** Resolve the API key to send upstream: cookie token → (opt-in only) VEXA_API_KEY → VEXA_BOT_API_KEY → "".
 *  cookies() can throw outside a request scope (e.g. in tests / edge cases); treat that as no cookie. */
export async function resolveApiKey(): Promise<string> {
  let cookieToken: string | undefined;
  try {
    cookieToken = (await cookies()).get(AUTH_COOKIE)?.value;
  } catch {
    cookieToken = undefined;
  }
  if (cookieToken) return cookieToken;
  if (!sharedApiKeyAllowed()) return "";
  return process.env.VEXA_API_KEY || process.env.VEXA_BOT_API_KEY || "";
}
