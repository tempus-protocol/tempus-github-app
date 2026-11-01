# Changelog

All notable changes to `tempus-github-app` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 0.2.0 "Guard"

Tempus Guard for GitHub: every agent change arrives with evidence.

### Added

- **"Verified by Tempus" check run.** With `--gate-keyfile` and `--gate-db` the executor runs the
  Guard flow: executes the permit, commits the signed outcome to the gate, exports the evidence
  bundle (optionally after `--create-checkpoint`), verifies it against the pinned gate key and
  the executor's own key, and publishes a check run on the commit the action produced
  (pushed tip, merge commit or PR head). `external_id` is the `action_id`; the conclusion is
  `success` only for a verified bundle of a succeeded action; the details link is the bundle's
  `verify_url` plus the gate key hint; the text embeds the bundle when it fits GitHub's limit
  and lists what the bundle cannot prove. New modules `check_run` (`CheckRunPublisher`,
  `Evidence`, `build_check_run`) and `guard` (`GuardFlow`, `GuardError`, `read_public_key`);
  new CLI options `--gate-keyfile`, `--check-name`, `--no-check-run`, `--bundle-out`,
  `--create-checkpoint`, `--max-stream-events` and their `TEMPUS_*` variables; exit code `3`
  when only the check run could not be published. Check tokens use a dedicated
  `checks: write` scope that no action can request.
- **`diff_sha256`** in `github.push_branch`: optional sha256 of the raw
  `git diff-tree -r -z --no-renames --no-commit-id --raw --abbrev=40 <base> <tip>` listing.
  The executor recomputes it from the unbundled commits and refuses a mismatch before the
  push; the accepted digest is echoed in the outcome.
- `github.create_pull_request` outcomes include the PR `head_sha`, so the check run can attach
  to the head commit.
- `github.push_branch`: publish an agent's git bundle to one branch under an exact-SHA permit. The permit binds the bundle sha256, `tip_sha`, `base_sha` and every changed path; the executor re-derives them, then creates or fast-forwards only branches under a namespace (`--push-branch-prefix`, default `agent/`) with a lease-guarded push. Git runs isolated from operator configuration, HTTPS-only, with the token in a repository-scoped header. New CLI options `--bundle` / `TEMPUS_BUNDLE` and `--push-branch-prefix` / `TEMPUS_PUSH_BRANCH_PREFIX`; the Docker image now includes `git`.
- Thread comments, issue/PR labels, and review requests with strict input validation and sanitized outcomes.
- Pull request merges with mandatory 40-character HEAD SHA, explicit merge method, repository-scoped `contents: write` tokens, and confirmation of boolean `merged: true`.
- Per-execution permit context and bounded GitHub rate-limit retries with read-only Gate revocation checks, complete server-required waits, and no retries after ambiguous results.
- Merge setup guidance for accepting new installation permissions and configuring tenant approval policy.
- Lightweight multi-purpose production `Dockerfile` (<150 MB, base `python:3.12-slim`) with unprivileged `tempus` user.
- In-memory and base64 private key loading via `GITHUB_APP_PRIVATE_KEY_BASE64` and `--app-private-key-base64` CLI flag.
- Hardened `.gitignore` enforcing absolute exclusion of secrets, keys, local SQLite databases, and runtime state.

### Changed

- The App is **Tempus Guard for GitHub** (manifest name and description, package description,
  webhook server title); the manifest requests `checks: write`. Package version 0.2.0.
- Core dependency is `tempus-ddb>=0.6,<0.7` (the Guard flow needs 0.6's `export_bundle`
  `verify_url`, `verify_bundle` trust report and `bundle_digest`). CI installs the core from its
  `main` branch until 0.6.0 is published on PyPI.
- Updated `pyproject.toml` dependency to consume official `tempus-ddb>=0.5.2` package.

### Fixed

- Classify unmergeable PRs (405) and changed HEAD commits (409) as deterministic failures.
- Suppress arbitrary transport error messages from signed outcomes.

## [0.1.0] - 2026-09-15

### Added
- **Credential-Isolated Mediated Execution**: Decouple privileged GitHub operations from AI agents. Agents sign Ed25519 intents without credentials; the executor holds the GitHub App RSA private key and performs authorized writes.
- **Short-Lived Installation Tokens**: Generates 1-hour, repository- and permission-scoped tokens (`issues: write`, `pull_requests: write`) on demand via RS256 JWT exchange, refreshed 60 seconds before expiration.
- **Atomic Single-Use Consumption**: Permits issued by the Tempus Gate are consumed atomically in SQLite; duplicate or replayed permit executions fail closed immediately.
- **Protocol Conformance Suite**: Certified against the official `tempus_ddb.testing.AdapterConformanceHarness` ensuring full compliance with Tempus zero-trust security invariants.
- **End-to-End B2A Lifecycle Integration**: Tested against real `TempusDDB` instances for intent signing, permit issuance, execution, outcome signing, trace verification, and incomplete execution recovery (`recover_incomplete`).
- **Standardized CLI (`tempus-github-app-executor`)**:
  - Full CLI parameter and environment variable fallback support (`.env` file compatible).
  - Optional `--gate-db` parameter for direct revocation and replay cross-verification against Gate databases.
  - `--version` flag and structured exit codes (0 for success, 1 for errors, 2 for ambiguous `UNKNOWN` outcomes).
  - Stdin support (`--permit -`) for direct piping in automated agent pipelines.
- **FastAPI Webhook Server (`tempus-github-app-server`)**:
  - Optional server entrypoint for receiving and dispatching GitHub App webhook events.
  - Strict HMAC-SHA256 signature verification (`X-Hub-Signature-256`).
  - GitHub `ping` event handling and `/healthz` health check endpoint for orchestrators.
- **Multi-OS GitHub Actions CI**: Automated test matrix running on Ubuntu, Windows, and macOS across Python 3.10, 3.11, and 3.12 with `ruff` and `pytest`.

### Fixed
- **Pytest Root Resolution**: Included repository root (`.`) in `tool.pytest.ini_options.pythonpath` so `pytest tests/ -v` can import `tests.conftest` fixtures without requiring `python -m pytest`.
- **Webhook Error Sanitization**: Replaced unhandled exception reflection in FastAPI webhook server (`/webhook`) with a generic error message (`Error handling event`) while capturing full traces in server logs, preventing internal information disclosure.
