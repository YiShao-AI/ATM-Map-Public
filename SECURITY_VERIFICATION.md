# Security verification record

## Recorded result

| Check | Version | 2026-09-04 result |
|---|---:|---|
| Gitleaks working-tree scan | 8.30.1 | no secrets detected; no project-specific finding allowlist is used |
| Trivy secret/configuration scan | 0.74.0 | 0 HIGH/CRITICAL findings |
| Workflow static validation | actionlint 1.7.12 | both GitHub Actions workflows passed |
| HTTP boundary suite | project suite | 83 tests passed with the service active; protected files and traversal forms were refused |
| Search History browser flow | Playwright 1.62.0 | create, list, filter, mouse/keyboard row opening, reopen, export, partial-result recovery, rerun, cache reuse, and direct-link recovery passed |
| Protected-host health smoke check | curl 8.5.0 | unauthenticated `/healthz` returned only `{"ok": true}` while the application root remained `401` |

Deployment examples reference shell environment variables instead of embedding
credentials in command lines. Default Gitleaks rules remain enabled for every
value and file. Real environment files, access logs, usage state, and
candidate-level benchmark exports remain excluded from publication.

## Continuous checks

`.github/workflows/security.yml` runs CodeQL for Python and JavaScript, a
full-history Gitleaks scan, and Trivy secret/configuration scanning on pushes,
pull requests, manual dispatch, and a weekly schedule. Third-party actions are
pinned to immutable commit SHAs, and Dependabot checks GitHub Actions monthly.

`.github/workflows/verify.yml` runs both stopped- and active-service suites with
placeholder keys, verifies the completed-run source fingerprints, then exercises
Search History and formula-safe export in Chromium against an isolated temporary
database and stubbed provider. It also relaunches the service with authentication
enabled to verify the public health response, protected application root, and
authenticated application access. Those checks make no paid API calls and do
not mutate a production database.

`/healthz` is the sole intentional unauthenticated application endpoint. It
returns a fixed, data-free response for external availability monitoring; it
does not expose configuration, usage, search, pipeline, or credential state.
All user and operational API routes remain inside the authentication boundary.

## Boundary

Repository scanning and application regressions do not replace cloud key
restrictions, billing alerts, tunnel authentication, host patching, or access-log
review. Those deployment controls remain in `DEPLOY.md`.
