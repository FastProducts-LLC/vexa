# app tests

App-level unit tests for the terminal client's `src/app/` concern (e.g.
`analytics.test.ts`). Run via the terminal package's test script (`pnpm test`,
Vitest); part of `gate:node` / `pnpm test` in CI.

- `noRuntimeCodeExecution.test.ts` — security finding C3: no source file may turn agent-authored text into
  executable JavaScript (`eval`, `new Function`, a runtime JS compiler such as sucrase or `@mdx-js/mdx`
  `evaluate`), the removed dependencies stay removed, the old Meeting Canvas runtime stays deleted, and the
  production Content-Security-Policy (`csp.mjs`, wired in `next.config.ts`) carries no `'unsafe-eval'`.
