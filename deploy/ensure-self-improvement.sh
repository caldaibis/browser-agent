#!/usr/bin/env bash
# Idempotent: ensure the self-improvement agent's runtime deps (the `claude`
# CLI that claude-agent-sdk shells out to, and litellm-proxy.service) are
# installed and up to date on this VPS. Safe to re-run on every deploy.
#
# Called from two places so a VPS never gets stuck a step behind the repo:
#   - deploy/setup.sh          (fresh VPS provisioning)
#   - `just deploy` / deploy.yml (every ongoing deploy to an existing VPS)
# Without this, an already-provisioned VPS would need a one-off manual SSH
# fix every time this list grows -- which is exactly what happened the first
# time litellm-proxy.service was added.
#
# Run as root (systemctl + a global npm install both need it -- matches how
# deploy.yml and deploy/setup.sh already SSH in as root).
set -euo pipefail

APP_USER="${APP_USER:-deploy}"
APP_HOME="/home/${APP_USER}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> claude CLI (self-improvement agent, via claude-agent-sdk)"
command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code >/dev/null

# The apply agent uses the native Rust binary and attaches it to browser-host's
# existing CDP profile. Install an exact version: this boundary submits real
# rental applications, so an unreviewed `latest` upgrade is not acceptable.
AGENT_BROWSER_VERSION="$(tr -d '[:space:]' < "${APP_DIR}/deploy/agent-browser.version")"
CURRENT_AGENT_BROWSER="$(agent-browser --version 2>/dev/null | awk '{print $2}' || true)"
if [ "${CURRENT_AGENT_BROWSER}" != "${AGENT_BROWSER_VERSION}" ]; then
  echo "==> agent-browser ${AGENT_BROWSER_VERSION} (apply agent)"
  npm install -g "agent-browser@${AGENT_BROWSER_VERSION}" >/dev/null
else
  echo "==> agent-browser ${AGENT_BROWSER_VERSION} already installed"
fi

echo "==> litellm-proxy.service"
sed "s|__APP_USER__|${APP_USER}|g; s|__APP_DIR__|${APP_DIR}|g; s|__APP_HOME__|${APP_HOME}|g" \
  "${APP_DIR}/deploy/systemd/litellm-proxy.service" > /etc/systemd/system/litellm-proxy.service
systemctl daemon-reload
systemctl enable --now litellm-proxy.service

# Dashboard safe-action sudoers drop-in. Re-synced on every deploy (not just
# fresh setup.sh installs) so an existing VPS picks up newly whitelisted
# commands -- e.g. poller start/stop, added for the dashboard's poller
# pause/resume buttons, which silently failed before this line existed.
echo "==> dashboard sudoers drop-in"
sed "s|__APP_USER__|${APP_USER}|g" "${APP_DIR}/deploy/stekkies-dashboard.sudoers" \
  > /etc/sudoers.d/stekkies-dashboard
chmod 0440 /etc/sudoers.d/stekkies-dashboard
