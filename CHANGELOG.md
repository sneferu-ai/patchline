# Changelog

All notable changes to Patchline are documented here. Versions are tied to the
`app_version` reported by `/settings`, `/healthz?deep=1`, and pilot-evidence export.

## [0.1.0] — 2026-08-05

Initial MVP slice (B13 build-ready contract).

- GitHub-OAuth vendor sign-in with per-request allowlist validation, CSRF,
  single-use OAuth `state`, and step-up re-authentication for PR dispatch.
- Changelog entries (two declarative prose fields + per-entry language) with
  engine-derived pattern sets (mock + Sneferu adapter behind FR-043).
- Per-repository, per-branch scanning with priority queue, rate-limit
  re-enqueue, and short write transactions.
- Remediation generation with `unidiff` structural validation, syntax-only
  validation with reconstruction, canonical diff hashing, and a confirmation
  gate before any GitHub write.
- PR dispatch via the GitHub Git Data API with branch reuse on retry and a
  diff-export fallback. No merge path exists in the product.
- Coverage board with the 12-status state machine, cross-entry roll-up,
  per-repository detail, verification timestamps, and manual mark-migrated.
- Periodic background rescan with regression notifications; HMAC-signed
  outgoing notifications; demo mode; operator CLI; Docker packaging.
