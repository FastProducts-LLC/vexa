/** Security regression gate (finding C3): the terminal must never turn agent-authored text — workspace
 *  markdown, canvas views, meeting notes — into executable JavaScript in the app origin. Two things pin
 *  that: (1) no source file reaches for eval / new Function / a runtime JS compiler, and (2) the production
 *  Content-Security-Policy carries no 'unsafe-eval', so a regression would throw in the browser anyway. */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";
import { buildContentSecurityPolicy, securityHeaders } from "../../csp.mjs";

const SRC = join(__dirname, "..");
const ROOT = join(SRC, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) { if (entry.name !== "__tests__") walk(p, out); }
    else if (/\.(ts|tsx|mjs|js)$/.test(entry.name)) out.push(p);
  }
  return out;
}

// Patterns that mean "text became code". Comments are stripped first so prose explaining the rule
// (like this file's own header) cannot trip it.
const EVAL_PATTERNS: [RegExp, string][] = [
  [/\bnew\s+Function\s*\(/, "new Function("],
  [/(^|[^.\w])eval\s*\(/, "eval("],
  [/\bAsyncFunction\b/, "AsyncFunction"],
  [/from\s+["']sucrase["']/, "sucrase import"],
  [/from\s+["']@mdx-js\/mdx["']/, "@mdx-js/mdx import (its evaluate/run compile MDX to JS)"],
  [/\bevaluate\s*\(\s*transformWikilinks/, "MDX evaluate()"],
];

function stripComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*$/gm, "$1");
}

describe("no runtime code execution from agent-authored content", () => {
  it("no source file evals, compiles-to-JS or imports a JS compiler", () => {
    const offenders: string[] = [];
    for (const file of walk(SRC)) {
      const code = stripComments(readFileSync(file, "utf8"));
      for (const [re, label] of EVAL_PATTERNS) if (re.test(code)) offenders.push(`${relative(ROOT, file)}: ${label}`);
    }
    expect(offenders).toEqual([]);
  });

  it("the removed compile-at-runtime dependencies stay removed", () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, "package.json"), "utf8")) as { dependencies?: Record<string, string>; devDependencies?: Record<string, string> };
    const declared = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
    for (const name of ["sucrase", "acorn", "acorn-walk", "@mdx-js/mdx"]) expect(declared, `${name} must not be a dependency`).not.toHaveProperty(name);
  });

  it("the Meeting Canvas new Function runtime is gone", () => {
    const canvas = readdirSync(join(SRC, "canvas"));
    expect(canvas).not.toContain("runtime.tsx");
    expect(canvas).not.toContain("canvasSource.ts");
    expect(canvas).not.toContain("validator.ts");
  });
});

describe("Content-Security-Policy (csp.mjs)", () => {
  it("production: no unsafe-eval anywhere, object-src none, frame-ancestors self", () => {
    const csp = buildContentSecurityPolicy({ dev: false });
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).toMatch(/(^|; )script-src 'self' 'unsafe-inline'(;|$)/);
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-ancestors 'self'");
    expect(csp).toContain("base-uri 'self'");
    expect(csp).toContain("connect-src 'self'");
  });

  it("development adds unsafe-eval for next dev only", () => {
    expect(buildContentSecurityPolicy({ dev: true })).toContain("script-src 'self' 'unsafe-inline' 'unsafe-eval'");
  });

  it("Google Analytics hosts appear only when a measurement id is configured", () => {
    expect(buildContentSecurityPolicy({})).not.toContain("googletagmanager");
    const withGa = buildContentSecurityPolicy({ gaMeasurementId: "G-TEST" });
    expect(withGa).toMatch(/script-src [^;]*https:\/\/www\.googletagmanager\.com/);
    expect(withGa).toMatch(/connect-src [^;]*https:\/\/\*\.google-analytics\.com/);
    expect(withGa).not.toContain("unsafe-eval");
  });

  it("securityHeaders emits the CSP plus nosniff, and the default (no opts) is the production policy", () => {
    const headers = securityHeaders();
    expect(headers.map((h) => h.key)).toEqual(["Content-Security-Policy", "X-Content-Type-Options"]);
    expect(headers[0].value).toBe(buildContentSecurityPolicy({ dev: false }));
    expect(headers[0].value).not.toContain("unsafe-eval");
  });
});
