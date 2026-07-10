# Alerting must not share a failure mode with what it monitors

(from AGENTS.md, verbatim; see git history for context)

**Alerting must not share a failure mode with what it monitors.** The Gmail
refresh token was revoked on 04-07-2026; the orchestrator crash-looped 1136
times over 3+ days and NO alert could reach the user because alert email
used that same dead token. Fixes: `notify.send_alert` pushes (web push)
BEFORE emailing; the orchestrator's watch loop catches Gmail failures
in-process (alert + `WATCH_RETRY_SECONDS` backoff, no systemd crash loop);
the healthcheck checks unit liveness and supports a dead-man ping. NB: a
Google OAuth app in *Testing* status expires refresh tokens every 7 days —
publish it to Production or this recurs weekly.
