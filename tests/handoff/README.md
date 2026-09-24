# tests/handoff/

Evaluator-supplied build-phase tests land here (`test_build_first.py` …
`test_build_fifth.py` per the B13 handoff contract). This directory is
intentionally empty of implementer-authored tests so the evaluator's files
collect cleanly on their own.

Contracts the handoff tests rely on (already implemented):

- `patchline.models` — full §5 schema incl. `tenant_id`, `deleted_at`,
  `priority`, `next_attempt_at`, `idx_coverage_repo_status`.
- `patchline.adapters.engine.MockEngineAdapter().get_version()` — non-empty.
- `patchline.adapters.scm.github.create_app_jwt()` — decodable JWT with
  `iss`/`exp` (requires PyJWT + a configured RSA key).
- `patchline.adapters.languages.python.PythonPack().parse_syntax` —
  `parse_syntax("x=1")` valid; `parse_syntax("if True::")` fails with
  line/offset populated.
- Mock OAuth: `GET /demo` in `DEMO_MODE=true` creates a vendor + session.
