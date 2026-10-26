import type { NextConfig } from "next";
import path from "path";
import { securityHeaders } from "./csp.mjs";

/**
 * Terminal composition root.
 *
 * Same-origin fallback for `/b/` (per-bot VNC/CDP), targeting the deploy SSOT `VEXA_API_URL`.
 * During early scaffolding (no backend) the rewrite is simply omitted so `npm run dev` works
 * against the prototype with no env required.
 *
 * Security headers (C3): every response carries a Content-Security-Policy WITHOUT 'unsafe-eval' in
 * production (csp.mjs) — the browser-level guarantee that agent-authored content rendered in this
 * origin can never be turned into executable code.
 *
 * NOTE: `/ws` is intentionally NOT rewritten here — Next.js rewrites proxy HTTP only and do not
 * carry the WebSocket upgrade. The custom server (server.mjs) handles the `/ws` upgrade directly,
 * proxying it to the gateway with the server-side api_key. A rewrite here would shadow that path.
 */
const VEXA_API_URL = process.env.VEXA_API_URL;

const nextConfig: NextConfig = {
  ...(process.env.BUILD_STANDALONE === "1" ? { output: "standalone" } : {}),
  turbopack: { root: path.resolve(__dirname) },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: securityHeaders({
          dev: process.env.NODE_ENV !== "production",
          gaMeasurementId: process.env.NEXT_PUBLIC_GA_MEASUREMENT_ID || "",
        }),
      },
    ];
  },
  async rewrites() {
    return VEXA_API_URL
      ? [
          { source: "/b/:path*", destination: `${VEXA_API_URL}/b/:path*` },
        ]
      : [];
  },
};

export default nextConfig;
