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

PLAYWRIGHT_MCP_VERSION="$(tr -d '[:space:]' < "${APP_DIR}/deploy/playwright-mcp.version")"

echo "==> Node.js 20+ (Playwright MCP runtime)"
NODE_MAJOR="$(node --version 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true)"
if [ -z "${NODE_MAJOR}" ] || [ "${NODE_MAJOR}" -lt 20 ]; then
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl gnupg >/dev/null
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
  echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
    > /etc/apt/sources.list.d/nodesource.list
  apt-get update -qq
  apt-get install -y -qq nodejs >/dev/null
fi
node -e 'const n=Number(process.versions.node.split(".")[0]); if(n<20) process.exit(1)'

echo "==> claude CLI (self-improvement agent, via claude-agent-sdk)"
command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code >/dev/null

echo "==> pinned Playwright MCP ${PLAYWRIGHT_MCP_VERSION}"
npm install -g "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" >/dev/null
npx --yes "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" --help >/dev/null

echo "==> litellm-proxy.service"
sed "s|__APP_USER__|${APP_USER}|g; s|__APP_DIR__|${APP_DIR}|g; s|__APP_HOME__|${APP_HOME}|g" \
  "${APP_DIR}/deploy/systemd/litellm-proxy.service" > /etc/systemd/system/litellm-proxy.service
for unit in self-improvement-worker.service self-improvement-worker.timer; do
  sed "s|__APP_USER__|${APP_USER}|g; s|__APP_DIR__|${APP_DIR}|g; s|__APP_HOME__|${APP_HOME}|g" \
    "${APP_DIR}/deploy/systemd/${unit}" > "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
systemctl enable --now litellm-proxy.service
systemctl enable --now self-improvement-worker.timer

# Dashboard safe-action sudoers drop-in. Re-synced on every deploy (not just
# fresh setup.sh installs) so an existing VPS picks up newly whitelisted
# commands -- e.g. poller start/stop, added for the dashboard's poller
# pause/resume buttons, which silently failed before this line existed.
echo "==> dashboard sudoers drop-in"
sed "s|__APP_USER__|${APP_USER}|g" "${APP_DIR}/deploy/stekkies-dashboard.sudoers" \
  > /etc/sudoers.d/stekkies-dashboard
chmod 0440 /etc/sudoers.d/stekkies-dashboard
