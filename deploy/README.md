# Deploy: 24/7 on a Hetzner Cloud VM

Always-on responder: headful Chromium under **Xvfb**, **systemd**-managed, with
**VNC** (over SSH tunnel) for the one-time interactive logins, plus a **Caddy**
dashboard (HTTPS + Basic Auth). No Hermes - the agent is `src/browser_agent/`
(DeepSeek + pinned agent-browser MCP). Most steps below are wrapped as `just`
commands.

Box: **Hetzner Cloud CX23** (2 vCPU / 4 GB, EU region — an EU IP reduces
Google/rental-site bot-flagging and is low-latency to the NL sites).
Example server: `stekkies` @ `<your-server-ip>` (nbg1), app user `deploy`.
The `justfile` reads `VPS_HOST`, `VPS_SSH_KEY_PATH`, `VPS_REMOTE_DIR` and
`DASHBOARD_DOMAIN` from your environment — set them to your own values.

## 1. Create the server
```bash
hcloud server create --name stekkies --type cx23 --image ubuntu-24.04 \
  --location nbg1 --ssh-key "your-key"
```

## 2. Repo access (read-only deploy key)
On the server, generate a key and add it to GitHub → repo → Settings → Deploy keys:
```bash
ssh-keygen -t ed25519 -C stekkies -f ~/.ssh/id_ed25519 -N ""
cat ~/.ssh/id_ed25519.pub
```

## 3. Provision
```bash
git clone git@github.com:caldaibis/browser-agent.git
bash browser-agent/deploy/setup.sh
```
Installs uv + Chromium + Xvfb + VNC + **Node 20+**, the pinned
**agent-browser** backend +
**Caddy**; deploys all systemd units; starts `xvfb`, `browser-host`,
`healthcheck.timer`, `dashboard` (but NOT `orchestrator` yet). To render the
Caddyfile, run with `DASHBOARD_DOMAIN`, `DASHBOARD_USER`, `DASHBOARD_HASH` set
(`DASHBOARD_HASH=$(caddy hash-password --plaintext '<pw>')`).

## 4. Upload secrets + documents (gitignored — from your laptop)
```bash
scp state/gmail_client_secret.json state/gmail_token.json \
    state/sources_credentials.json deploy@SERVER:/home/deploy/browser-agent/state/
# Application documents are NOT in git (personal data) — copy them out of band:
scp documents/* deploy@SERVER:/home/deploy/browser-agent/documents/
# DeepSeek key (the apply agent reads it):
printf 'DEEPSEEK_API_KEY=sk-...\n' | ssh deploy@SERVER \
    'cat > /home/deploy/browser-agent/state/agent.env'
```
`just push-creds` / `push-token` / `push-env` do these after the first time.

## 5. One-time interactive logins via VNC
```bash
just vnc          # starts vnc.service + prints the tunnel command
# ssh -L 5900:localhost:5900 deploy@SERVER ; VNC viewer -> localhost:5900
#   sign into Google FIRST (enables SSO), then Stekkies + rental sites.
just vnc-stop
```
Sessions persist in `state/chromium-profile`, so this is one-time (redo only if a
login expires — the health check emails you when the Stekkies session drops).

## 6. Go live
```bash
ssh deploy@SERVER 'sudo systemctl enable --now orchestrator.service'
just logs          # live agent journal
```
The dashboard is already up at `https://<DASHBOARD_DOMAIN>` (Basic Auth).

## Ops (via just, from your laptop)
```bash
just status        # all services at a glance
just logs / just dash-logs / just activity
just deploy        # push -> pull on VPS -> uv sync -> restart agent + dashboard
just pause / just resume          # stop/start the inbox watcher
just credits       # remaining DeepSeek credit + Stekkies login (runs healthcheck)
just restart browser-host         # reattach a clean browser if a session goes stale
```

## CI / CD (GitHub Actions)
- **CI** (`.github/workflows/ci.yml`) runs on every push/PR: `just check`
  (byte-compile + import smoke + render the apply prompt). No browser/secrets.
- **CD** (`.github/workflows/deploy.yml`) auto-deploys on push to `main` *after*
  CI passes — same steps as `just deploy` (ff pull → `uv sync` → restart
  orchestrator + dashboard). One-time setup:
  1. Add a repo **secret** `VPS_SSH_KEY` = a private key whose public half is in
     the VPS `root` user's `~/.ssh/authorized_keys` (your existing
     `~/.ssh/id_ed25519` works; a dedicated deploy key is cleaner).
  2. Add a repo **secret** `VPS_HOST` = the VPS IP/host (used by `deploy.yml`);
     set the matching `VPS_HOST` env var locally for the `justfile`.
  - `just deploy` from your laptop still works as the manual fallback.

## Notes
- The agent applies and **submits** autonomously — there is no dry-run guard.
- `browser-host` keeps the CDP browser on `:9222`; the extractor and apply agent
  attach to it. If logins expire, re-run step 5.
- Keep VNC stopped except during logins (localhost-only + SSH-tunneled).
- Keep DeepSeek credit topped up; the health check
  emails you below the threshold.
