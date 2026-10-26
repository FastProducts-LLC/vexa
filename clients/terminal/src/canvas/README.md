# canvas

The Meeting Canvas: the terminal's live/processed transcript surface for one meeting.

Public surface:
- `MeetingCanvasView.tsx` renders the meeting through ONE built-in engine (`LiveTranscriptEngine.tsx`) in
  raw or processed mode; `useMeeting.ts` / `notes.ts` / `actions.ts` expose the live meeting feed and
  sanctioned side effects.
- `kit.tsx` exports the theme-locked `ui` component vocabulary.

## Security boundary (finding C3)

The terminal does NOT execute agent-authored view code. Earlier versions loaded `views/meeting.tsx` from the
user's workspace, transpiled it and ran it through `new Function(...)` in the app origin, guarded only by an
identifier blocklist. That path (`runtime.tsx`, `canvasSource.ts`, `validator.ts`) was already disconnected
from the UI and has now been deleted, together with the `sucrase` / `acorn` dependencies, so there is no
code left that can turn workspace text into JavaScript. The Meeting Canvas renders ONLY from data (the
copilot's `meeting.schema.json` envelope + the transcript stream) through the built-in engine.

The same rule holds for workspace markdown: `src/ui-kit/MdxDoc.tsx` parses MDX to a syntax tree, rejects
anything executable (ESM, `{expressions}`, expression attributes) and walks the declarative tree straight
into React elements — nothing is compiled to JavaScript.

Both are enforced at the browser level by the terminal's Content-Security-Policy (`csp.mjs`): production
responses carry `script-src` WITHOUT `'unsafe-eval'`, so `eval` / `new Function` throw in this origin. If a
generative-UI feature is ever reintroduced it must run model-authored code in an opaque-origin sandboxed
iframe (`sandbox="allow-scripts"` without `allow-same-origin`, data in / actions out over `postMessage`),
never in the app origin. `src/__tests__/noRuntimeCodeExecution.test.ts` fails the suite if an eval path or
one of the removed dependencies comes back.
