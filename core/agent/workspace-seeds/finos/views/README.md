# workspace-seed views

Legacy default view source copied into a new workspace for reference. **The terminal does not load or
execute these files** (security finding C3: model-authored code no longer runs in the app origin). The
Meeting Canvas renders from data — the `agents/meeting.schema.json` envelope and the transcript stream —
through the terminal's built-in engine. Agents should not author or edit `views/meeting.tsx`.
