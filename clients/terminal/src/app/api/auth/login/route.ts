/** Direct email login — no SMTP, no magic link. POST {email} → find-or-create the user at admin-api,
 *  mint an APIToken (scopes bot,tx,browser), set the httpOnly `vexa-token` + `vexa-user-info` cookies.
 *
 *  Mirrors the dashboard's VEXA_ALLOW_DIRECT_LOGIN branch (without importing it). No email is ever sent.
 *  OFF unless VEXA_ALLOW_DIRECT_LOGIN=true: it mints a token for ANY matching email with the server's
 *  admin key (no proof of mailbox ownership) — and on a fresh instance the first login claims admin.
 *  Must never be cached — a cached response would pin one identity for every subsequent login.
 */
import { NextResponse, type NextRequest } from "next/server";
import { cookies } from "next/headers";
import { AUTH_COOKIE, USER_INFO_COOKIE, findOrCreateUserToken } from "../adminApi";

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
  if (process.env.VEXA_ALLOW_DIRECT_LOGIN !== "true") {
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
  // Direct email login is a DEBUG path only — real sign-in goes through Google/Microsoft OAuth
  // (api/auth/[...nextauth]). Restrict it to test accounts so it can't be used as a password-less bypass.
  if (!normalized.includes("test")) {
    return NextResponse.json(
      { error: "Direct email login is for test accounts only — use Google or Microsoft sign-in." },
      { status: 403, headers: NO_STORE },
    );
  }

  const result = await findOrCreateUserToken(normalized);
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
