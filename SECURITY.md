# Security Policy

## Reporting a vulnerability

Email: security@patchline.example

Please do not open public issues for security reports. Include a description,
reproduction steps, and the affected version (`app_version` from `/settings`
or `CHANGELOG.md`).

## Responsible disclosure timeline

- Acknowledgement within 3 business days.
- Triage and severity assessment within 10 business days.
- Fix or mitigation published within 90 days of acknowledgement, coordinated
  with the reporter where possible.

## Scope notes

- Customer source code is never persisted by Patchline; temporary archives are
  deleted within 10 minutes of job completion or failure.
- Installation tokens are held in memory only (50-minute TTL) and never written
  to disk.
- The SQLite database file is not encrypted at rest; rely on platform volume
  encryption.
- Webhook secret rotation requires a coordinated deploy (known limitation).
