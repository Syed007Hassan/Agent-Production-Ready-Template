# Security Policy

## Supported versions

This is a template repository. Security fixes are applied to the `master` branch. Fork maintainers are responsible for keeping their forks up to date.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately via [GitHub Security Advisories](../../security/advisories/new) or email the maintainer directly. Include:

- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested mitigations

You can expect an acknowledgement within 48 hours and a fix or mitigation plan within 7 days for confirmed vulnerabilities.

## Security considerations when using this template

**Before deploying to production:**

- Set a strong, random `JWT_SECRET_KEY` (32+ characters)
- Rotate all secrets — never use the `.env.example` values
- Set `DEBUG=false`
- Restrict `ALLOWED_ORIGINS` to your actual frontend domain
- Set `LANGFUSE_TRACING_ENABLED=false` if you don't want conversation data sent to Langfuse
- Use environment-specific `.env` files — never commit secrets to git

**What the template does to protect you:**

- Passwords hashed with bcrypt (never stored plaintext)
- JWT tokens include a `jti` claim for uniqueness
- All user inputs sanitised before use
- Rate limiting on auth and chat endpoints
- Secret detection via `detect-secrets` pre-commit hook
- CORS configured (restrict `ALLOWED_ORIGINS` in production)
