# Security Policy

## Supported Versions

| Version | Branch | Supported |
|---|---|---|
| `2.5.x` | `release/prod-ready` | Yes |
| `0.1.0` (initial commit) | `main` (pre-merge) | No |

## Reporting a Vulnerability

Please report security issues privately rather than opening a public GitHub issue:
1. Email `[ADD: security-contact-email@yourdomain.com]` with a description of the vulnerability, reproduction steps, and affected endpoints.
2. You will receive an acknowledgement within 24 hours and a remediation timeline within 72 hours.

## Enforced Security Controls

- **Zero Hardcoded Secrets:** Pre-commit hooks (`.pre-commit-config.yaml`) and GitHub Actions CI (`.github/workflows/ci.yml`) reject commits containing tokens (`github_pat_`, `ghp_`, `AIzaSy`, `sk-`) or `.env` files.
- **Fail-Closed Startup:** `validate_production_readiness()` in `backend/app/config.py` halts startup if `APP_ENV=production` and any placeholder secret (`FORBIDDEN_SECRET_VALUES`) is present.
- **Webhook Authenticity:** `POST /webhooks/whatsapp` verifies `X-Hub-Signature-256` using constant-time `hmac.compare_digest` with `META_APP_SECRET`.
- **Tenant Isolation:** Every API query scopes `tenant_id` strictly from the verified HMAC-SHA256 JWT session (`backend/app/auth.py`).
- **CSV Formula Injection Protection:** All exported CSV cells are sanitized by `sanitize_csv_cell` (`backend/app/accounting_sync.py`).
