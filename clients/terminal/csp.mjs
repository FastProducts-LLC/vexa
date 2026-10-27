// Content-Security-Policy for the terminal (security finding C3).
//
// The terminal renders agent-authored content (workspace markdown, meeting notes, canvas data) in the
// user's authenticated origin. The policy below is the browser-level guarantee that none of that content
// can ever become code: there is NO 'unsafe-eval' in production, so `eval`, `new Function` and friends
// throw even if a renderer that compiles model-authored text to JavaScript were ever wired back in
// (the old Meeting Canvas `new Function` runtime and the @mdx-js `evaluate` path were both removed —
// see src/ui-kit/MdxDoc.tsx and src/canvas/README.md).
//
// Plain ESM (no TypeScript) so the SAME function is imported by next.config.ts at build/boot time and
// by the vitest suite — one policy, one source of truth.
//
// What stays open, and why:
//   - script-src 'unsafe-inline': Next's hydration bootstrap and the theme snippet in app/layout.tsx are
//     inline scripts. Nonce-based CSP needs a middleware + fully dynamic rendering; out of scope here.
//     'unsafe-inline' does NOT re-enable eval — that is a separate keyword this policy never emits.
//   - style-src 'unsafe-inline': SSR'd `style=""` attributes, next/font and the toast library.
//   - img-src https: — workspace docs embed remote images (<img src="https://…">) and OAuth avatars.
//   - connect-src/img-src/script-src Google Analytics hosts only when a measurement id is baked in.
//   - 'unsafe-eval' in DEVELOPMENT only: `next dev` evaluates source maps / HMR chunks through eval.

/** Google Analytics (gtag.js) origins — added only when NEXT_PUBLIC_GA_MEASUREMENT_ID is configured. */
const GA_SCRIPT = ["https://www.googletagmanager.com"];
const GA_CONNECT = ["https://www.googletagmanager.com", "https://*.google-analytics.com", "https://*.analytics.google.com"];
const GA_IMG = ["https://www.googletagmanager.com", "https://*.google-analytics.com"];

/**
 * Build the policy string.
 * @param {{ dev?: boolean, gaMeasurementId?: string }} [opts]
 * @returns {string}
 */
export function buildContentSecurityPolicy(opts = {}) {
  const dev = Boolean(opts.dev);
  const ga = Boolean((opts.gaMeasurementId || "").trim());
  const script = ["'self'", "'unsafe-inline'"];
  if (dev) script.push("'unsafe-eval'");
  if (ga) script.push(...GA_SCRIPT);
  const connect = ["'self'"];
  if (ga) connect.push(...GA_CONNECT);
  const img = ["'self'", "data:", "blob:", "https:"];
  if (ga) img.push(...GA_IMG);
  const directives = [
    ["default-src", ["'self'"]],
    ["script-src", script],
    ["style-src", ["'self'", "'unsafe-inline'"]],
    ["img-src", img],
    ["font-src", ["'self'", "data:"]],
    ["connect-src", connect],
    ["media-src", ["'self'", "blob:", "data:"]],
    ["worker-src", ["'self'", "blob:"]],
    ["frame-src", ["'self'"]],
    ["object-src", ["'none'"]],
    ["base-uri", ["'self'"]],
    ["frame-ancestors", ["'self'"]],
  ];
  return directives.map(([name, values]) => `${name} ${values.join(" ")}`).join("; ");
}

/** The headers Next attaches to every response (next.config.ts → headers()). */
export function securityHeaders(opts = {}) {
  return [
    { key: "Content-Security-Policy", value: buildContentSecurityPolicy(opts) },
    { key: "X-Content-Type-Options", value: "nosniff" },
  ];
}
