#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 server (e.g. Hetzner CX22) to run the Stekkies
# responder 24/7: headful Chromium under Xvfb, driven by systemd, with VNC for
# the one-time interactive logins. Idempotent — safe to re-run.
#
# Run as root on the server:   bash deploy/setup.sh
set -euo pipefail

APP_USER="${APP_USER:-deploy}"
APP_HOME="/home/${APP_USER}"
APP_DIR="${APP_HOME}/browser-agent"
REPO_URL="${REPO_URL:-git@github.com:caldaibis/browser-agent.git}"
DISPLAY_NUM="${DISPLAY_NUM:-:99}"

echo "==> [1/7] base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  git curl ca-certificates xvfb x11vnc fonts-liberation fonts-noto-color-emoji \
  nodejs npm \
  >/dev/null
# Node/npx is required for the Playwright MCP (browser automation + file upload)
# that Hermes drives. The MCP server config travels in the copied ~/.hermes.

echo "==> [2/7] app user '${APP_USER}'"
if ! id "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${APP_USER}"
fi

echo "==> [3/7] uv (as ${APP_USER})"
sudo -u "${APP_USER}" bash -lc '
  command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
'

echo "==> [4/7] clone/update repo"
if [ ! -d "${APP_DIR}/.git" ]; then
  echo "    cloning ${REPO_URL} (server SSH key must be a GitHub deploy key)"
  sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
else
  sudo -u "${APP_USER}" git -C "${APP_DIR}" pull --ff-only
fi

echo "==> [5/7] chromium OS deps (root) + browser (as ${APP_USER})"
# install-deps needs root (apt); the browser download lives in the user's cache.
sudo -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && uv sync"
# Run install-deps as root via the venv's playwright (uvx isn't on root's PATH).
"${APP_DIR}/.venv/bin/python" -m playwright install-deps chromium
sudo -u "${APP_USER}" bash -lc "cd '${APP_DIR}' && uv run playwright install chromium"

echo "==> [6/8] systemd units"
for unit in xvfb.service browser-host.service orchestrator.service poller.service \
            vnc.service healthcheck.service healthcheck.timer dashboard.service; do
  sed "s|__APP_USER__|${APP_USER}|g; s|__APP_DIR__|${APP_DIR}|g; s|__DISPLAY__|${DISPLAY_NUM}|g; s|__APP_HOME__|${APP_HOME}|g" \
    "${APP_DIR}/deploy/systemd/${unit}" > "/etc/systemd/system/${unit}"
done
# sudoers drop-in for the dashboard's safe actions
sed "s|__APP_USER__|${APP_USER}|g" "${APP_DIR}/deploy/stekkies-dashboard.sudoers" \
  > /etc/sudoers.d/stekkies-dashboard
chmod 0440 /etc/sudoers.d/stekkies-dashboard
systemctl daemon-reload

echo "==> [7/8] Caddy (reverse proxy: HTTPS + Basic Auth for the dashboard)"
if ! command -v caddy >/dev/null 2>&1; then
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi
# Render the Caddyfile if the dashboard env vars are provided, else leave a note.
if [ -n "${DASHBOARD_DOMAIN:-}" ] && [ -n "${DASHBOARD_USER:-}" ] && [ -n "${DASHBOARD_HASH:-}" ]; then
  sed "s|__DOMAIN__|${DASHBOARD_DOMAIN}|g; s|__USER__|${DASHBOARD_USER}|g; s|__HASH__|${DASHBOARD_HASH}|g" \
    "${APP_DIR}/deploy/Caddyfile.template" > /etc/caddy/Caddyfile
  systemctl restart caddy
else
  echo "    (set DASHBOARD_DOMAIN/USER/HASH and render deploy/Caddyfile.template -> /etc/caddy/Caddyfile)"
fi

echo "==> [8/8] enable Xvfb + browser host + health-check timer + dashboard (NOT orchestrator yet)"
systemctl enable --now xvfb.service
systemctl enable --now browser-host.service
systemctl enable --now healthcheck.timer
systemctl enable --now dashboard.service

cat <<EOF

==> Done. Next, MANUAL steps (see deploy/README.md):
  1. Upload secrets:
       scp state/gmail_client_secret.json state/gmail_token.json \\
           state/sources_credentials.json ${APP_USER}@SERVER:${APP_DIR}/state/
       # create ${APP_DIR}/state/agent.env with: OPENROUTER_API_KEY=sk-or-...
  2. One-time logins via VNC:
       systemctl start vnc.service
       # from your laptop:  ssh -L 5900:localhost:5900 ${APP_USER}@SERVER
       # connect a VNC viewer to localhost:5900, log into Google + Stekkies + sites
       systemctl stop vnc.service
  3. Go live:
       systemctl enable --now orchestrator.service
       systemctl enable --now poller.service   # active site poller (optional)
       journalctl -u orchestrator -u poller -f
EOF
