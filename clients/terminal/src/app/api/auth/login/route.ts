/** Direct email login — DEV / E2E ONLY. POST {email} → find-or-create the user at admin-api, mint an
 *  APIToken (scopes bot,tx,browser), set the httpOnly `vexa-token` + `vexa-user-info` cookies.
 *
 *  There is no credential check, so it is 404 unless the dev-login gate is open (VEXA_TERMINAL_DEV_LOGIN=1,
 *  NODE_ENV != production) and the email EXACTLY matches VEXA_TERMINAL_DEV_LOGIN_EMAILS — see devLogin.ts.
 *  It NEVER claims the first-run admin role: admin bootstrap requires the real OAuth flow.
 *  Real sign-in is Google/Microsoft OAuth (api/auth/[...nextauth]).
 *  Must never be cached — a cached response would pin one identity for every subsequent login.
 */
import { NextResponse, type NextRequest } from "next/server";
import { cookies } from "next/headers";
import { AUTH_COOKIE, USER_INFO_COOKIE, findOrCreateUserToken } from "../adminApi";
import { devLoginAllowed, devLoginEnabled } from "./devLogin";

export const dynamic = "force-dynamic";
export const fetchCache = "force-no-store";

const NO_STORE = { "Cache-Control": "no-store, no-cache, must-revalidate" } as const;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function isSecureRequest(): boolean {
  return (
    (process.env.TERMINAL_URL || "").startsWith("https://") ||
    (process.env.NEXTAUTH_URL || "").startsWith("https://") ||
    false
  );
}

export async function POST(request: NextRequest) {
  if (!devLoginEnabled()) {
    return NextResponse.json({ error: "Not found" }, { status: 404, headers: NO_STORE });
  }
  let email: unknown;
  try {
    ({ email } = await request.json());
  } catch {
    return NextResponse.json({ error: "Invalid request body" }, { status: 400, headers: NO_STORE });
  }

  if (typeof email !== "string" || !email.trim()) {
    return NextResponse.json({ error: "Email is required" }, { status: 400, headers: NO_STORE });
  }
  const normalized = email.trim().toLowerCase();
  if (!EMAIL_RE.test(normalized)) {
    return NextResponse.json({ error: "Invalid email format" }, { status: 400, headers: NO_STORE });
  }
  // Exact allowlist match only — never a substring/domain test (C6: any email containing "test" used
  // to get a session, and on a fresh instance the admin role).
  if (!devLoginAllowed(normalized)) {
    return NextResponse.json(
      { error: "This email is not enabled for dev login — use Google or Microsoft sign-in." },
      { status: 403, headers: NO_STORE },
    );
  }

  // bootstrapAdmin: false — a credential-less login must never claim the first-run admin role.
  const result = await findOrCreateUserToken(normalized, { bootstrapAdmin: false });
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: result.status || 500, headers: NO_STORE });
  }

  const { user, token } = result;
  const secure = isSecureRequest();
  const cookieStore = await cookies();
  const opts = { httpOnly: true, secure, sameSite: "lax" as const, maxAge: 60 * 60 * 24 * 30, path: "/" };
  cookieStore.set(AUTH_COOKIE, token, opts);
  cookieStore.set(USER_INFO_COOKIE, JSON.stringify({ email: user.email, name: user.name || user.email.split("@")[0] }), opts);

  return NextResponse.json(
    { success: true, user: { id: user.id, email: user.email, name: user.name ?? user.email } },
    { headers: NO_STORE },
  );
}
