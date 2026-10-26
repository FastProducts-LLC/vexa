/** Gate for the direct (password-less) email login — a LOCAL DEV / E2E convenience, never a sign-in path.
 *
 *  It mints a session for an email with no proof of mailbox ownership, so it is OFF unless ALL hold:
 *    1. VEXA_TERMINAL_DEV_LOGIN=1 (explicit operator opt-in; the legacy VEXA_ALLOW_DIRECT_LOGIN no longer
 *       enables anything),
 *    2. NODE_ENV is not "production" (refused even when the flag is set — prod builds can't turn it on),
 *    3. the email EXACTLY matches an entry of VEXA_TERMINAL_DEV_LOGIN_EMAILS (comma-separated, compared
 *       lowercased + trimmed). No substrings, no wildcards, no domain matches; an empty list allows nobody.
 *  It also never claims the first-run admin role (see login/route.ts) — admin bootstrap needs real OAuth.
 */

export function devLoginEnabled(): boolean {
  return process.env.VEXA_TERMINAL_DEV_LOGIN === "1" && process.env.NODE_ENV !== "production";
}

export function devLoginEmails(): Set<string> {
  return new Set(
    (process.env.VEXA_TERMINAL_DEV_LOGIN_EMAILS || "")
      .split(",")
      .map((e) => e.trim().toLowerCase())
      .filter(Boolean),
  );
}

/** true only when the gate is open AND `email` (already normalized) is an exact allowlist entry. */
export function devLoginAllowed(email: string): boolean {
  return devLoginEnabled() && devLoginEmails().has(email);
}
