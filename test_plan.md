# Patchline browser smoke plan (runtime_test_mode: playwright)

Runtime: `python3 -m uvicorn patchline.app:app --host 127.0.0.1 --port {port}`
with `DEMO_MODE=true`, `ENGINE_MODE=mock`, a temp `DATABASE_URL`, and any `SESSION_SECRET`.

## Flow under test

1. **Health** — `GET /healthz?deep=1` returns 200 with `"database":"connected"`,
   `"engine":"mock"` (or healthy), `"worker"` key present.
2. **Demo auto-login** — `GET /` redirects to `/login`; demo mode auto-creates the
   demo session and lands on the coverage board. Banner "Demo Mode — no real GitHub
   data" is visible on every surface.
3. **Connections setup (P2)** — open `/connections/setup`; the install link from
   `GITHUB_APP_SLUG` renders with a copy control; mock installations list from
   `tests/fixtures/github/installations.json`.
4. **Repository inventory (P3)** — open `/repositories`; repositories from mock
   fixtures render grouped by installation account with collapsible sections.
5. **Entry composer (P4)** — open `/entries/new`; title / prior_api_shape /
   target_contract / language fields render; submit with a too-long title shows an
   inline error naming only `title`.
6. **Scan console (P5)** — create an entry, trigger "scan connected repositories";
   repository cards render with `aria-live="polite"` polling regions; a note states
   remediation operates on the default branch only.
7. **Diff review (P6)** — on a repository with hits, generate remediation; diff
   renders with per-file collapsible headers; the syntax-only warning is visible;
   in demo mode "approve and open PR" shows a simulated success state.
8. **Coverage board (P8)** — home roll-up rows link to `/repositories/{rid}/coverage`;
   statuses render as icon + text (never color alone); the best-effort verification
   note is present.
9. **Keyboard operability** — tab order reaches the approve action; all controls are
   operable without a pointer.

## Expected terminal state

No console errors; all GET surfaces 200; no POST succeeds without a CSRF token
(403); the demo session cannot dispatch real PRs.
